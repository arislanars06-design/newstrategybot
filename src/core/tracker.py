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

    # 2. Bucket the candidates.
    entries: list[dict[str, Any]] = []
    tps: list[dict[str, Any]] = []
    sls: list[dict[str, Any]] = []

    for o in candidates:
        if not _matches_position(o, position_side):
            continue
        kind = _classify(o, entry_side=entry_side, exit_side=exit_side)
        if kind == "entry":
            entries.append(o)
        elif kind == "tp":
            tps.append(o)
        elif kind == "sl":
            sls.append(o)

    # 3. Validate counts.
    if len(entries) != expected_rungs:
        return TrackerResult(
            rungs=[],
            warnings=[],
            error=(
                f"Need exactly {expected_rungs} unassigned entry orders for "
                f"{side}, found {len(entries)}. Place 8 entries (or cancel "
                "the extras) and try again."
            ),
        )

    # 4. Sort entries: BUY ladder descends (100, 99, ...), SELL ascends.
    descending = side == BlockSide.BUY
    entries.sort(key=lambda o: float(o["price"]), reverse=descending)

    # 5. Verify all entries share the same quantity (the strategy assumes it).
    qtys = {round(float(o["origQty"]), 10) for o in entries}
    if len(qtys) > 1:
        return TrackerResult(
            rungs=[],
            warnings=[],
            error=(
                "Entry orders have mixed quantities. The strategy expects "
                "all 8 rungs to share the same size. Detected quantities: "
                f"{sorted(qtys)}."
            ),
        )
    entry_qty = float(entries[0]["origQty"])

    # 6. Filter TPs / SLs to the matching quantity, then sort them in the
    #    same order as the entries so the i-th entry pairs with the i-th
    #    TP/SL. This handles the natural "ladder" case (each rung has
    #    the same offset) far better than greedy nearest-price pairing,
    #    which mis-assigns rungs when TP intervals overlap.
    qty_tps = [tp for tp in tps if _qty_close(float(tp.get("origQty", 0)), entry_qty)]
    qty_sls = [sl for sl in sls if _qty_close(float(sl.get("origQty", 0)), entry_qty)]

    if len(qty_tps) < expected_rungs:
        return TrackerResult(
            rungs=[], warnings=[],
            error=(
                f"Need {expected_rungs} TP orders with qty={entry_qty}, "
                f"found {len(qty_tps)}."
            ),
        )
    if len(qty_sls) < expected_rungs:
        return TrackerResult(
            rungs=[], warnings=[],
            error=(
                f"Need {expected_rungs} SL orders with qty={entry_qty}, "
                f"found {len(qty_sls)}."
            ),
        )

    qty_tps.sort(key=_tp_price_of, reverse=descending)
    qty_sls.sort(key=_sl_price_of, reverse=descending)

    # If there are extras at the same qty, they belong to other (yet to
    # be confirmed) blocks — keep only the first 8 in the sorted order.
    paired_tps = qty_tps[:expected_rungs]
    paired_sls = qty_sls[:expected_rungs]

    rungs: list[TrackedRung] = []
    for seq, (entry, tp, sl) in enumerate(
        zip(entries, paired_tps, paired_sls, strict=True), start=1
    ):
        entry_price = float(entry["price"])
        tp_price = _tp_price_of(tp)
        sl_price = _sl_price_of(sl)

        # Validate the side constraint per-rung. If the user placed
        # orders that don't form a coherent ladder, surface the problem
        # early instead of silently mis-pairing.
        if side == BlockSide.BUY:
            if tp_price <= entry_price:
                return TrackerResult(
                    rungs=[], warnings=[],
                    error=(
                        f"BUY rung {seq}: TP {tp_price} is not above entry "
                        f"{entry_price}. Re-check your order layout."
                    ),
                )
            if sl_price >= entry_price:
                return TrackerResult(
                    rungs=[], warnings=[],
                    error=(
                        f"BUY rung {seq}: SL {sl_price} is not below entry "
                        f"{entry_price}."
                    ),
                )
        else:
            if tp_price >= entry_price:
                return TrackerResult(
                    rungs=[], warnings=[],
                    error=(
                        f"SELL rung {seq}: TP {tp_price} is not below entry "
                        f"{entry_price}."
                    ),
                )
            if sl_price <= entry_price:
                return TrackerResult(
                    rungs=[], warnings=[],
                    error=(
                        f"SELL rung {seq}: SL {sl_price} is not above entry "
                        f"{entry_price}."
                    ),
                )

        rungs.append(
            TrackedRung(
                seq=seq,
                entry_price=entry_price,
                tp_price=tp_price,
                sl_price=sl_price,
                qty=entry_qty,
                entry_order_id=str(entry["orderId"]),
                tp_order_id=str(tp["orderId"]),
                sl_order_id=str(sl["orderId"]),
            )
        )

    # 7. Surface warnings about leftover unmatched TP/SL orders.
    warnings: list[str] = []
    leftover_tp = len(qty_tps) - expected_rungs
    leftover_sl = len(qty_sls) - expected_rungs
    if leftover_tp:
        warnings.append(f"{leftover_tp} extra TP order(s) with same qty ignored.")
    if leftover_sl:
        warnings.append(f"{leftover_sl} extra SL order(s) with same qty ignored.")

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
