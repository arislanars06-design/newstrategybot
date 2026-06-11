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

    # 4. Group entries / TPs / SLs by quantity. The user is allowed to
    #    pick a different size per rung (different risk per order) so
    #    we cannot assume one global qty. Within each qty bucket we
    #    sort by price and pair index-wise — this keeps ladder offsets
    #    intact while supporting the simple all-same-qty case as a
    #    degenerate one-bucket scenario.
    descending = side == BlockSide.BUY

    qty_buckets: dict[float, dict[str, list[dict[str, Any]]]] = {}

    def _bucket(qty_key: float) -> dict[str, list[dict[str, Any]]]:
        return qty_buckets.setdefault(qty_key, {"entries": [], "tps": [], "sls": []})

    for e in entries:
        _bucket(_qty_key(e.get("origQty", 0)))["entries"].append(e)
    for t in tps:
        _bucket(_qty_key(t.get("origQty", 0)))["tps"].append(t)
    for s in sls:
        _bucket(_qty_key(s.get("origQty", 0)))["sls"].append(s)

    rungs: list[TrackedRung] = []

    # Process buckets in a deterministic order (largest qty first feels
    # natural but is purely cosmetic; final rungs are re-sorted below).
    for qty_key in sorted(qty_buckets.keys(), reverse=True):
        bucket = qty_buckets[qty_key]
        bucket_entries = bucket["entries"]
        if not bucket_entries:
            continue  # qty appears only on a TP/SL — irrelevant by itself.

        sorted_entries = sorted(
            bucket_entries, key=lambda o: float(o["price"]), reverse=descending
        )
        bucket_tps = sorted(bucket["tps"], key=_tp_price_of, reverse=descending)
        bucket_sls = sorted(bucket["sls"], key=_sl_price_of, reverse=descending)

        if len(bucket_tps) < len(sorted_entries):
            return TrackerResult(
                rungs=[], warnings=[],
                error=(
                    f"Quantity bucket {qty_key}: need {len(sorted_entries)} "
                    f"TP order(s) at this size, found {len(bucket_tps)}."
                ),
            )
        if len(bucket_sls) < len(sorted_entries):
            return TrackerResult(
                rungs=[], warnings=[],
                error=(
                    f"Quantity bucket {qty_key}: need {len(sorted_entries)} "
                    f"SL order(s) at this size, found {len(bucket_sls)}."
                ),
            )

        # Pair index-wise; extras at this qty go back to the pool but
        # since we partition by qty there is no other consumer here.
        for entry, tp, sl in zip(
            sorted_entries,
            bucket_tps[: len(sorted_entries)],
            bucket_sls[: len(sorted_entries)],
            strict=True,
        ):
            entry_price = float(entry["price"])
            tp_price = _tp_price_of(tp)
            sl_price = _sl_price_of(sl)

            if side == BlockSide.BUY:
                if tp_price <= entry_price:
                    return TrackerResult(
                        rungs=[], warnings=[],
                        error=(
                            f"BUY rung at {entry_price}: TP {tp_price} must "
                            "be above entry."
                        ),
                    )
                if sl_price >= entry_price:
                    return TrackerResult(
                        rungs=[], warnings=[],
                        error=(
                            f"BUY rung at {entry_price}: SL {sl_price} must "
                            "be below entry."
                        ),
                    )
            else:
                if tp_price >= entry_price:
                    return TrackerResult(
                        rungs=[], warnings=[],
                        error=(
                            f"SELL rung at {entry_price}: TP {tp_price} must "
                            "be below entry."
                        ),
                    )
                if sl_price <= entry_price:
                    return TrackerResult(
                        rungs=[], warnings=[],
                        error=(
                            f"SELL rung at {entry_price}: SL {sl_price} must "
                            "be above entry."
                        ),
                    )

            rungs.append(
                TrackedRung(
                    seq=0,  # renumbered below after we sort by price
                    entry_price=entry_price,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    qty=float(entry["origQty"]),
                    entry_order_id=str(entry["orderId"]),
                    tp_order_id=str(tp["orderId"]),
                    sl_order_id=str(sl["orderId"]),
                )
            )

    if len(rungs) != expected_rungs:
        return TrackerResult(
            rungs=[], warnings=[],
            error=(
                f"Could only pair {len(rungs)} rung(s); expected "
                f"{expected_rungs}. Make sure every entry has a matching "
                "TP and SL with the same quantity."
            ),
        )

    # 5. Sort the final ladder by entry price (descending for BUY,
    #    ascending for SELL) and assign seq numbers 1..N.
    rungs.sort(key=lambda r: r.entry_price, reverse=descending)
    for i, rung in enumerate(rungs, start=1):
        rung.seq = i

    # 6. Surface warnings about leftover unmatched TP/SL orders.
    used_tp_ids = {r.tp_order_id for r in rungs}
    used_sl_ids = {r.sl_order_id for r in rungs}
    leftover_tp = sum(1 for t in tps if str(t["orderId"]) not in used_tp_ids)
    leftover_sl = sum(1 for s in sls if str(s["orderId"]) not in used_sl_ids)
    warnings: list[str] = []
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
