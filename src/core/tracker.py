"""Discover and group user-placed orders on Binance Futures.

Used by the ``/track`` Telegram flow: the user manually places 8 entries
and pairs each with a TP and SL on Binance (or TradingView). The bot
reads the open-orders list, filters out anything already adopted by an
active block, and proposes a ladder for the user to confirm.

The grouping strategy is deliberately conservative:

* All entries must share the same quantity (this is what the user's
  strategy demands).
* Each entry is paired with **one** TP and **one** SL whose quantity
  matches and whose price sits on the correct side of the entry.
* If pairing is ambiguous (two TPs match the same entry) we pick the
  one closest to the entry — that maps to the user's own order book
  visualisation more naturally.

Binance's "TP/SL" UI checkbox (and TradingView's bracket-order panel)
generates exit orders flagged ``closePosition: true``. Those carry no
quantity of their own — at trigger time the exchange closes whatever
of the position is still open. We treat them as **wildcards**: they
slot into any rung's qty bucket that lacks an exact-qty pair, sorted
by trigger price the same way regular reduce-only orders are.

In addition to ``discover_block`` (caller specifies the side),
``auto_detect_side`` lets the bot infer the side from the
unassigned LIMIT entries on a symbol so the ``/track`` flow can skip
the manual BUY/SELL question whenever the answer is unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.plan import EXPECTED_ORDERS_PER_BLOCK, OrderSpec
from src.db.enums import BlockSide


# ---------- value objects ----------


@dataclass(slots=True)
class TrackedRung:
    """A single proposed rung of a tracked block.

    Carries both the plan-level data (price/qty) and the exchange order
    IDs that the engine will use to match WebSocket events.
    """

    seq: int
    entry_price: float
    tp_price: float
    sl_price: float
    qty: float
    entry_order_id: str
    tp_order_id: str
    sl_order_id: str

    def to_order_spec(self) -> OrderSpec:
        return OrderSpec(
            seq=self.seq,
            entry_price=self.entry_price,
            tp_price=self.tp_price,
            sl_price=self.sl_price,
            qty=self.qty,
        )


@dataclass(slots=True)
class TrackerResult:
    """Outcome of scanning open orders for a candidate block.

    ``rungs`` is the proposed ladder. ``warnings`` lists soft problems
    that don't prevent tracking (e.g. extra orders ignored). ``error``
    is set to a non-empty string when tracking is impossible.
    """

    rungs: list[TrackedRung]
    warnings: list[str]
    error: str | None = None


# ---------- public API ----------


def auto_detect_side(
    open_orders: list[dict[str, Any]],
    *,
    assigned_order_ids: set[str],
    expected_rungs: int = EXPECTED_ORDERS_PER_BLOCK,
) -> tuple[BlockSide | None, str | None]:
    """Infer the block's side from unassigned LIMIT entries on the symbol.

    Returns ``(side, error)``:

    * ``(BlockSide.BUY, None)`` — exactly ``expected_rungs`` BUY entries
      and zero SELL entries are unassigned.
    * ``(BlockSide.SELL, None)`` — symmetrical case.
    * ``(None, error)`` — couldn't decide; the message explains why so
      the Telegram flow can fall back to asking the trader manually.

    Only LIMIT orders are considered (TP/SL exit orders are skipped),
    so the rule is simple: "the side matches the side of the entries
    you placed". Reduce-only and closePosition flags exclude exit
    orders even when they happen to be LIMIT type.
    """
    candidates = [
        o for o in open_orders if str(o.get("orderId", "")) not in assigned_order_ids
    ]

    n_buy = 0
    n_sell = 0
    for o in candidates:
        otype = str(o.get("type", "")).upper()
        if otype != "LIMIT":
            continue
        if _is_reduce_only(o) or _is_close_position(o):
            continue
        side = str(o.get("side", "")).upper()
        if side == "BUY":
            n_buy += 1
        elif side == "SELL":
            n_sell += 1

    if n_buy == expected_rungs and n_sell == 0:
        return BlockSide.BUY, None
    if n_sell == expected_rungs and n_buy == 0:
        return BlockSide.SELL, None
    if n_buy == 0 and n_sell == 0:
        return None, (
            "No unassigned LIMIT entry orders found on this symbol. "
            "Place 8 entries on the chart first, then run /track."
        )
    if n_buy >= expected_rungs and n_sell >= expected_rungs:
        return None, (
            f"Found {n_buy} BUY and {n_sell} SELL entries on this symbol — "
            "please pick a side manually."
        )
    return None, (
        f"Found {n_buy} BUY entry order(s) and {n_sell} SELL entry order(s); "
        f"need exactly {expected_rungs} on one side."
    )


def discover_block(
    open_orders: list[dict[str, Any]],
    *,
    side: BlockSide,
    assigned_order_ids: set[str],
    expected_rungs: int = EXPECTED_ORDERS_PER_BLOCK,
) -> TrackerResult:
    """Filter, classify, and pair the supplied raw Binance open orders.

    Parameters
    ----------
    open_orders:
        Raw payload list from ``BinanceClient.list_open_orders``.
    side:
        ``BlockSide.BUY`` for a long block, ``BlockSide.SELL`` for short.
    assigned_order_ids:
        Set of Binance order IDs that already belong to other active
        blocks; they will be excluded so multiple blocks on the same
        symbol don't fight for the same orders.
    expected_rungs:
        Required number of entries; defaults to 8 to match the user's
        strategy.
    """
    # 1. Filter out orders that already belong to other blocks.
    candidates = [
        o for o in open_orders if str(o.get("orderId", "")) not in assigned_order_ids
    ]

    entry_side = "BUY" if side == BlockSide.BUY else "SELL"
    exit_side = "SELL" if side == BlockSide.BUY else "BUY"
    position_side = "LONG" if side == BlockSide.BUY else "SHORT"

    # 2. Classify candidates. closePosition orders share the same pool
    #    as regular reduce-only TP/SL — they have origQty=0 and only
    #    fire if any of the position is open at trigger time, so they
    #    can stand in for any rung. We track that any closePosition
    #    order was used so the user gets a warning at confirmation
    #    time about the slightly different gap-scenario behaviour.
    entries: list[dict[str, Any]] = []
    tps: list[dict[str, Any]] = []
    sls: list[dict[str, Any]] = []
    close_position_seen = False
    # Diagnostics — every candidate gets a short label so error messages
    # can tell the trader what the bot actually saw on Binance.
    classification_counts: dict[str, int] = {}

    for o in candidates:
        type_label = (
            f"{str(o.get('type','?'))}/"
            f"{str(o.get('side','?'))}/"
            f"ps={str(o.get('positionSide','?'))}/"
            f"reduceOnly={_is_reduce_only(o)}/"
            f"closePos={_is_close_position(o)}"
        )
        classification_counts[type_label] = classification_counts.get(type_label, 0) + 1

        if not _matches_position(o, position_side):
            continue
        kind = _classify(o, entry_side=entry_side, exit_side=exit_side)
        if kind == "entry":
            entries.append(o)
        elif kind == "tp":
            tps.append(o)
            if _is_close_position(o):
                close_position_seen = True
        elif kind == "sl":
            sls.append(o)
            if _is_close_position(o):
                close_position_seen = True

    def _diag_breakdown() -> str:
        """Compact one-line snapshot of what we actually saw on Binance."""
        if not classification_counts:
            return "no orders on this symbol"
        rows = sorted(classification_counts.items(), key=lambda kv: -kv[1])
        return "; ".join(f"{n}x {label}" for label, n in rows)

    # 3. Validate counts.
    if len(entries) != expected_rungs:
        return TrackerResult(
            rungs=[],
            warnings=[],
            error=(
                f"Need exactly {expected_rungs} unassigned entry orders for "
                f"{side}, found {len(entries)}.\n\n"
                f"What the bot saw: {_diag_breakdown()}"
            ),
        )
    if len(tps) < expected_rungs:
        return TrackerResult(
            rungs=[], warnings=[],
            error=(
                f"Need {expected_rungs} TP order(s) on the exit side; "
                f"found {len(tps)}. Each entry needs its own TP — either "
                "as a reduce-only LIMIT/TAKE_PROFIT_MARKET with the entry's "
                "quantity, or with the \"close position\" flag set.\n\n"
                f"What the bot saw: {_diag_breakdown()}"
            ),
        )
    if len(sls) < expected_rungs:
        return TrackerResult(
            rungs=[], warnings=[],
            error=(
                f"Need {expected_rungs} SL order(s) on the exit side; "
                f"found {len(sls)}. Each entry needs its own STOP_MARKET — "
                "either reduce-only with matching quantity, or with the "
                "\"close position\" flag set.\n\n"
                f"What the bot saw: {_diag_breakdown()}"
            ),
        )

    # 4. Sort everything in ladder order — for BUY the highest-priced
    #    entry pairs with the highest TP and SL, and so on. With
    #    consistent ladder layouts this works whether the trader chose
    #    exact-qty reduce-only orders, "close position" orders, or
    #    mixed both. Per-rung qty math always uses the *entry's* qty,
    #    so wildcard TP/SL orders with origQty=0 don't break risk
    #    accounting.
    descending = side == BlockSide.BUY
    entries.sort(key=lambda o: float(o["price"]), reverse=descending)
    tps.sort(key=_tp_price_of, reverse=descending)
    sls.sort(key=_sl_price_of, reverse=descending)

    used_tps = tps[:expected_rungs]
    used_sls = sls[:expected_rungs]

    rungs: list[TrackedRung] = []
    for seq, (entry, tp, sl) in enumerate(
        zip(entries, used_tps, used_sls, strict=True), start=1
    ):
        entry_price = float(entry["price"])
        tp_price = _tp_price_of(tp)
        sl_price = _sl_price_of(sl)

        if side == BlockSide.BUY:
            if tp_price <= entry_price:
                return TrackerResult(
                    rungs=[], warnings=[],
                    error=(
                        f"BUY rung {seq} (entry {entry_price}): TP "
                        f"{tp_price} must be above entry."
                    ),
                )
            if sl_price >= entry_price:
                return TrackerResult(
                    rungs=[], warnings=[],
                    error=(
                        f"BUY rung {seq} (entry {entry_price}): SL "
                        f"{sl_price} must be below entry."
                    ),
                )
        else:
            if tp_price >= entry_price:
                return TrackerResult(
                    rungs=[], warnings=[],
                    error=(
                        f"SELL rung {seq} (entry {entry_price}): TP "
                        f"{tp_price} must be below entry."
                    ),
                )
            if sl_price <= entry_price:
                return TrackerResult(
                    rungs=[], warnings=[],
                    error=(
                        f"SELL rung {seq} (entry {entry_price}): SL "
                        f"{sl_price} must be above entry."
                    ),
                )

        rungs.append(
            TrackedRung(
                seq=seq,
                entry_price=entry_price,
                tp_price=tp_price,
                sl_price=sl_price,
                qty=float(entry["origQty"]),  # always the entry's qty
                entry_order_id=str(entry["orderId"]),
                tp_order_id=str(tp["orderId"]),
                sl_order_id=str(sl["orderId"]),
            )
        )

    # 5. Warnings — closePosition behaviour, plus any leftover orders
    #    we ignored.
    warnings: list[str] = []
    if close_position_seen:
        warnings.append(
            "Some TP/SL orders use \"close position\" mode — at trigger "
            "time they close the whole open position on this side, not "
            "just one rung's quantity. With chain mode (only one rung "
            "open at a time) the effect is identical."
        )
    leftover_tp = len(tps) - expected_rungs
    leftover_sl = len(sls) - expected_rungs
    if leftover_tp:
        warnings.append(f"{leftover_tp} extra TP order(s) ignored.")
    if leftover_sl:
        warnings.append(f"{leftover_sl} extra SL order(s) ignored.")

    return TrackerResult(rungs=rungs, warnings=warnings)


# ---------- internal helpers ----------


def _matches_position(order: dict[str, Any], position_side: str) -> bool:
    """Hedge-mode position side filter (defaults to BOTH for one-way mode)."""
    ps = str(order.get("positionSide", "BOTH"))
    return ps in {position_side, "BOTH"}


def _is_reduce_only(order: dict[str, Any]) -> bool:
    """Binance returns either bool or string; normalise."""
    raw = order.get("reduceOnly")
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() == "true"


def _is_close_position(order: dict[str, Any]) -> bool:
    """Binance's ``closePosition`` flag, normalised across bool / string.

    Set by the UI's "TP/SL" checkbox and by TradingView bracket orders.
    The exchange treats these as "close whatever of the position is
    open at trigger time", which is why we route them through a
    qty-agnostic wildcard pool in :func:`discover_block`.
    """
    raw = order.get("closePosition")
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() == "true"


def _classify(
    order: dict[str, Any], *, entry_side: str, exit_side: str
) -> str | None:
    """Tag an open order as entry / tp / sl / unknown."""
    otype = str(order.get("type", "")).upper()
    side = str(order.get("side", "")).upper()
    reduce_only = _is_reduce_only(order)

    if otype == "LIMIT" and side == entry_side and not reduce_only:
        return "entry"
    if side != exit_side:
        return None
    if otype in {"TAKE_PROFIT_MARKET", "TAKE_PROFIT"}:
        return "tp"
    if otype == "LIMIT" and reduce_only:
        return "tp"
    if otype in {"STOP_MARKET", "STOP"}:
        return "sl"
    return None


def _tp_price_of(order: dict[str, Any]) -> float:
    """Return the user-meaningful TP trigger price."""
    sp = order.get("stopPrice")
    if sp not in (None, "", "0", 0):
        return float(sp)
    return float(order.get("price", 0))


def _sl_price_of(order: dict[str, Any]) -> float:
    """Return the user-meaningful SL trigger price."""
    sp = order.get("stopPrice")
    if sp not in (None, "", "0", 0):
        return float(sp)
    return float(order.get("price", 0))


def _qty_close(a: float, b: float, tol: float = 1e-8) -> bool:
    """Float-tolerant equality for quantities."""
    return abs(a - b) <= tol * max(abs(a), abs(b), 1.0)


def _qty_key(qty: Any) -> float:
    """Bucketing key for grouping orders by quantity.

    Crypto exchanges report quantities with up to 8 decimal places. We
    round at 1e-8 so cosmetic floating-point noise (e.g. 0.0010000001)
    doesn't accidentally split otherwise-identical buckets.
    """
    return round(float(qty), 8)
