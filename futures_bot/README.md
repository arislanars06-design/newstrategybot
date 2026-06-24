# Futures bot

A self-contained futures-trading bot living next to the existing
crypto bot in `src/`. The crypto bot is **not touched** by anything
in this package — futures bot has its own Telegram bot, its own
SQLite database, its own configuration prefix (`FB_…`), and its own
process. Run with:

```bash
python -m futures_bot.main
```

## What it does

Six-rung uniform Fibonacci ladder per block, ROUND UP lot sizing,
spread-aware SL and TP placement, full chain-rule semantics. See the
[strategy spec](../SPEC.md) for the math; the short version is:

* Levels: `0%, 61.8%, 74.53%, 87.27%, 100%, 112.73%, 125.47%, 138.2%`
  (uniform 12.73% gap between adjacent levels).
* Risk per rung: `base × 1.5 ** (seq − 1)`, cumulative 49.26× base
  if every SL fires.
* `SL_live = next_entry − spread × 1.5` (BUY; mirror for SELL).
* `TP_gross = 3 × cumulative_real_risk` at the moment the rung fills.

## Layout

```
futures_bot/
├── adapters/      Broker interface (base) + MockAdapter + MT5Adapter.
├── bot/           Telegram bot (aiogram 3): handlers, FSM, keyboards,
│                  notifier, allow-list auth, Russian-language formatters.
├── core/          Engine (block lifecycle) + spread/session guards
│                  + notification objects.
├── db/            SQLAlchemy models, repository helpers, enums.
├── strategy/      Pure-math Fibonacci, risk and TP/SL pricing — no
│                  I/O, fully unit-tested.
├── main.py        Process entry point (`python -m futures_bot.main`).
├── config.py      `FB_*` settings via pydantic-settings.
└── logging_setup.py
```

## Configuration

Copy `.env.example` to `<repo-root>/.env` and fill in the marked
values. The futures bot uses the `FB_` prefix so it can share a single
`.env` with the crypto bot without collision.

## Running

### Quick local sanity (no MT5)

```bash
export FB_USE_MOCK_ADAPTER=1
python -m futures_bot.main
```

The bot still expects Telegram credentials in `.env`; everything else
runs against the in-memory mock broker.

### Docker (production)

```bash
cd docker
docker compose up -d
```

The `mt5` service exposes VNC on `127.0.0.1:5900` for the one-shot
Exness login; the `futures-bot` service polls the mt5 RPC over the
compose network.

## Tests

```bash
python -m pytest tests/futures
```

The suite covers the strategy math, the spread/session guards, and an
end-to-end engine run against `MockAdapter` (create block → fill →
TP → WIN).
