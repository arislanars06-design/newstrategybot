# BLOCK TRADING SYSTEM — Specification

This document is the authoritative specification for the
`newstrategybot` engine. The trader writes the strategy on the chart;
the bot is an **operational tracker**, not an analyst. Everything below
is implemented in code under `src/` and verified by smoke tests.

---

## 1. Core unit — the BLOCK

```
1 BLOCK = 6 chained limit orders on the same symbol and side
```

Each order carries its own:

- `entry_price`
- `tp_price`  (take-profit)
- `sl_price`  (stop-loss)
- `qty`       (independent — the trader sizes risk per rung)

The block also has one shared parameter:

- `cancel_price` (a.k.a. *price-invalid*) — see §5 INVALID rule.

---

## 2. Chain layout

The trader places the rungs as a price ladder. By convention each
rung's SL equals the next rung's entry, so when the chain rolls down
**at most one position is open at a time** — every SL hand-off opens
the next entry on the same tick.

### BUY block (long)

```
Rung 1:  Entry 100   SL 99   TP X
Rung 2:  Entry  99   SL 98   TP X
Rung 3:  Entry  98   SL 97   TP X
...
Rung 8:  Entry  93   SL 92   TP X   (last SL is "free" — no successor)
```

### SELL block (short) — mirror

```
Rung 1:  Entry 100   SL 101  TP X
Rung 2:  Entry 101   SL 102  TP X
...
Rung 8:  Entry 107   SL 108  TP X
```

The bot does **not** enforce the chain rule — it only validates
ordering and side constraints. A trader who wants gapped or
non-chained ladders is free to place them; the lifecycle below still
applies.

---

## 3. Block creation paths

Three equivalent ways to bring a block into existence. All end with
the same DB rows and watcher subscriptions.

### `/fib` — bot computes the ladder from Fibonacci levels (primary)

This is the trader's preferred workflow and the one the **Yaratish**
menu button drives. The trader supplies five values:

1. Symbol and side (BUY / SELL)
2. The 0% anchor price
3. The 100% anchor price
4. The first rung's risk in USDT
5. The cancel ("price invalid") price

Every other number is derived. The bot:

* Computes 6 entry prices at Fibonacci levels **61.80, 74.53, 87.26,
  100.00, 112.73 and 125.46 percent** of the user-supplied range.
  Levels are uniformly spaced at ~12.73 % apart inside the 61.8 %
  to 138.2 % band, giving every rung an equal SL distance.
* Chains stop-losses so each rung's SL equals the next rung's entry;
  the eighth rung's SL sits at level 138.2% — one Fib step beyond
  the last entry.
* Sizes each rung's USD risk via a geometric **2×** progression from
  the trader's first-rung input. Total max risk across the ladder is
  approximately **49.26 ×** the first-rung risk.
* Sets per-rung TP ratios at **1:5 for rungs 1–3, then 5.64, 6.42,
  6.95, 7.30 and 7.53** for rungs 4–8.
* Reads the current symbol leverage straight from Binance (one less
  question for the trader) and computes per-rung margin via the
  trader's formula: `margin = risk × 100 / (sl_pct × leverage)`.
* Quantity is then `pos_size / entry`, where `pos_size = margin ×
  leverage`.

The preview screen shows the entire ladder plus totals (max risk,
collateral required, notional). On confirmation the engine places
all 18 orders (6 entries + 6 TPs + 6 SLs) in a single batch through
the same `engine.create_block()` path used by `/newblock`.

### `/newblock` — bot places explicit prices

The trader drives an FSM through Telegram (symbol → side → 6 entries
→ 8 TPs → last SL → cancel price → qty). Useful when the trader
wants prices that don't fit a Fibonacci grid. The engine validates
the plan, persists it, and then sends each order to Binance.

### `/track` — bot adopts orders the trader placed manually

The trader places the 6 entries (each with its own TP and SL) directly
on Binance or via TradingView's trading panel, then runs `/track`. The
engine reads the open-orders list, classifies orders into entry / TP
/ SL buckets, pairs them by quantity + price index, and shows the
proposed ladder for confirmation. The trader only types the
`cancel_price`. Already-adopted orders are filtered out so multiple
blocks can coexist on the same symbol. Side is auto-detected from the
unassigned LIMIT entries — manual selection is offered only when the
side is ambiguous (e.g. eight unassigned entries on each side).

---

## 4. Block identity

A block is identified by its database `id` and by the set of Binance
`orderId` values it owns:

```python
Block = {
    block_id: 15,
    order_ids: [101, 102, 103, 104, 105, 106, 107, 108],
    cancel_price: 105_000,
    is_managed: True | False,   # /newblock vs /track
}
```

WebSocket order-update events are routed to the right rung via:

1. `client_order_id` for managed blocks (predictable, set by the bot).
2. `orderId` lookup against `Order.entry_order_id` /
   `tp_order_id` / `sl_order_id` for tracked blocks (the user's UI
   chose the client_id).

---

## 5. Lifecycle

A block is always in exactly one of these states. Transitions are
strict — no state is ever revisited.

| State    | Enter when                                                 |
|----------|-----------------------------------------------------------|
| ACTIVE   | All orders persisted; the block is being watched.          |
| WIN      | **Any single rung's TP fires.** Pending entries cancelled. |
| LOSS     | All 6 rungs ended at SL with no TP hit anywhere.           |
| INVALID  | Mark price hits `cancel_price` *before any entry triggers*. |
| ERROR    | Unrecoverable problem (e.g. failed initial placement).     |

Open-position policy after WIN — **Variant A**: an already-triggered
position keeps running on its own TP/SL. The chain layout means in
practice only one position is open at the moment of WIN, so this
rarely matters; in gap scenarios it is a deliberate trade-off.

`cancel_price` is **deactivated** the first time any rung triggers —
once a position is open, the cancel-price rule no longer applies.

---

## 6. Fees

Limit orders incur **zero fee until they execute**. Cancelled orders
cost nothing. Realised PnL stored on each rung is taken straight from
Binance's `realizedPnl` field on the fill event, which is already net
of fees.

---

## 7. PnL accounting

### Per-order

When TP or SL fires, `Order.pnl` is set from the WebSocket
`realizedPnl` (preferred) or computed from `(exit - entry) * qty *
direction` as a fallback.

### Per-block

Two views:

- **Net PnL (terminal blocks):** `sum(o.pnl for o in block.orders)`,
  persisted on `Block.net_pnl` when the block finalises.
- **Real-time PnL (active blocks):**
  `realised + unrealised`, where
  `unrealised = direction * (mark_price - filled_entry) * qty` summed
  over rungs in `TRIGGERED` state. Mark price is fetched once per
  call. If the mark-price call fails the unrealised number is reported
  as "unavailable" rather than a fake zero.

### Per-rung risk

`order_risk_amount = abs(entry - sl) * qty`. Block risk is the sum
across rungs. Both are shown in `/block <id>` and in the plan/tracker
previews.

---

## 8. Telegram UI

### Commands

```
/menu        — main menu (inline keyboard)
/fib         — Fibonacci-driven block creation (bot computes the ladder)
/newblock    — create a block from explicit prices (bot places orders)
/track       — adopt orders you placed manually
/list        — active blocks (one line each)
/block <id>  — full detail view (with live PnL)
/cancel <id> — manually close a block
/modify <id> <new_cancel_price> — change cancel price of an active block
/stats [w]   — aggregate stats (today | 7 | 30 | 90 | 180 | 365 | all)
/reports [d] — per-day breakdown for the last d days (default 7)
/balance     — wallet USDT balance
/raw <symbol>— diagnostic: dump every open order on a symbol
/help        — list commands
```

### Menu structure

```
📦 Block
   ➕ Yaratish      → /fib  (Fibonacci ladder — primary)
   📋 Aktiv bloklar → /list
   ✋ Bekor qilish  → /cancel <id>
   ✏️ O'zgartirish  → /modify <id> <new_price>
   ⬅️ Orqaga
📊 Statistika
   Today   7d   30d
   3mo     6mo  1y
   All     ⬅️ Orqaga
💰 Balans
```

`/modify` is only valid while the cancel-price rule is still active —
i.e. before any rung has triggered. After the first fill, modifying
the cancel price would be misleading because the rule no longer
applies, so the engine refuses. Attempts on terminal blocks are
rejected with the current status quoted back. New cancel price must
remain on the correct side of the ladder (above all entries for BUY,
below all entries for SELL).

### Active block view (matches the trader's spec)

```
🟡 BLOCK #15
BTCUSDT BUY
Status: ACTIVE
Cancel price: 105000 (active)

Orders:
1/8 SL       entry 100 TP 103 SL 99   qty 0.01  risk 0.01  pnl -0.01
2/8 SL       entry  99 TP 102 SL 98   qty 0.01  risk 0.01  pnl -0.01
3/8 ACTIVE   entry  98 TP 101 SL 97   qty 0.01  risk 0.01
4/8 PENDING  entry  97 TP 100 SL 96   qty 0.01  risk 0.01
...

Block risk (max loss): 0.08
Current PnL: +0.045  (realised -0.02, unrealised +0.065)
Mark price: 98.5  open positions: 1
```

### Statistics

Time-windowed (`Today` / `7d` / `30d` / `All`):

```
📊 Statistics — Last 30 days
Closed blocks: 120
🟢 Wins: 45 (37.5%)
🔴 Losses: 50
⚫ Invalid: 25
🚨 Errors: 0

Net PnL: +320.0 USDT
```

### Reports

`daily_pnl_breakdown(days)` renders a per-day table:

```
📈 Reports — last 7 days
date         W   L   I   E        net
2026-06-11   3   2   1   0      +12.4
2026-06-10   1   3   0   0       -8.1
...

Total PnL: +14.7 USDT
```

---

## 9. Notifications

Every event lands in the trader's personal Telegram chat. A separate
**channel feed** (configured via `TELEGRAM_NOTIFY_CHANNEL_ID`)
receives only block-level outcomes — a high-signal feed without
per-order play-by-play:

| Event           | Personal chat | Channel |
|-----------------|---------------|---------|
| BLOCK_CREATED   | yes           | yes     |
| ORDER_TRIGGERED | yes           | no      |
| SL_HIT          | yes           | no      |
| BLOCK_WIN       | yes           | yes     |
| BLOCK_LOSS      | yes           | yes     |
| BLOCK_INVALID   | yes           | yes     |
| BLOCK_ERROR     | yes           | yes     |

Channel-send failures are warning-level — a misconfigured channel
must never block trading.

---

## 10. The single inviolable rule

> **A block is one lifecycle of 6 orders. The lifecycle ends only on
> TP (→ WIN), all 6 SLs (→ LOSS), or cancel-price hit before any
> trigger (→ INVALID).**

Nothing else (clock time, drawdown, manual whim) can move a block to
a terminal state automatically. The trader can force a close via
`/cancel <id>`; the engine itself follows the rule above without
exception.

---

## 11. What the bot does — and does not — do

| The bot does                                  | The bot does NOT do          |
|-----------------------------------------------|------------------------------|
| Watch order updates via WebSocket             | Analyse charts               |
| Apply the lifecycle rules deterministically   | Generate signals             |
| Cancel pending orders on WIN / INVALID        | Adjust SL or TP automatically |
| Track per-rung and per-block PnL              | Re-enter after a LOSS         |
| Mirror block events to a notification channel | Make any directional decision |
| Recover active blocks after a restart         | Trade outside placed blocks   |

The trader supplies the strategy. The bot supplies the discipline.

---

## 12. Implementation map

| Concern                        | Module                              |
|--------------------------------|-------------------------------------|
| Validated plan + chain helper  | `src/core/plan.py`                  |
| Fibonacci ladder generator     | `src/core/fib.py`                   |
| State machine + WS routing     | `src/core/engine.py`                |
| `/track` discovery + grouping  | `src/core/tracker.py`               |
| Risk math                      | `src/core/risk.py`                  |
| Notification objects + types   | `src/core/notifications.py`         |
| Binance REST + WebSocket       | `src/exchange/`                     |
| ORM + repository               | `src/db/`                           |
| Telegram bot (FSM, menus, fmt) | `src/bot/`                          |
| Entrypoint + signal handling   | `src/main.py`                       |

Anything that diverges from this spec is a bug. Open an issue against
the repo with the rule that was violated and the path the engine took.
