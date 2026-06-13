"""Fibonacci-based block plan generation.

Implements the trader's variant of the strategy where the bot — not
the chart — decides the prices. The trader provides only:

* the symbol and side (BUY / SELL)
* the 0% and 100% anchor prices defining the Fibonacci range
* the first rung's dollar risk
* the cancel ("price invalid") price

The bot computes everything else:

* 8 entry prices at Fibonacci levels 68.1, 70.2, 78.6, 89.3, 100,
  111.8, 123.6 and 130.9 percent of the user-supplied range.
* 8 chained stop-losses where each rung's SL equals the next rung's
  entry (so the chain rolls down naturally), and rung 8's SL sits at
  level 138.2 % — one Fib step beyond the last entry.
* 8 take-profits sized by per-rung risk:reward ratios. Rungs 1–3 use
  1:5; rungs 4–8 ramp through 5.64 → 5.42 → 6.95 → 7.30 → 7.53.
* Per-rung dollar risk that grows by 1.5× each step, so the trader
  enters a single number ("first risk") and the bot expands it across
  the ladder.
* Per-rung margin (and the resulting qty) computed from
  ``risk * 100 / (sl_pct * leverage)`` — exactly the formula the
  trader laid out.

Leverage is **not** asked from the user: the bot reads it directly
from Binance for the symbol+side via ``BinanceClient.get_leverage``.
That keeps the FSM at five inputs total, matching the trader's spec.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.plan import EXPECTED_ORDERS_PER_BLOCK, BlockPlan, OrderSpec
from src.db.enums import BlockSide

# Standard Fibonacci levels used by the strategy. 10 levels.
# Entries occupy indices 1..8 (68.1 % through 130.9 %).
# SL of rung N sits at index N+1, so rung 8's SL is at index 9 = 138.2 %.
FIB_LEVELS: list[float] = [
    0.0, 0.681, 0.702, 0.786, 0.893, 1.0, 1.118, 1.236, 1.309, 1.382,
]

# Risk:reward ratios per rung, exactly as the trader specified.
TP_RATIOS: list[float] = [5.0, 5.0, 5.0, 5.64, 6.42, 6.95, 7.30, 7.53]

# Risk grows by this factor each rung.
RISK_MULTIPLIER: float = 1.5

# Default leverage when Binance can't be queried (e.g. unit tests).
DEFAULT_LEVERAGE: int = 10


@dataclass(slots=True, frozen=True)
class FibRungComputed:
    """Detailed per-rung breakdown.

    Carried alongside ``BlockPlan`` so the preview message can show the
    trader exactly how every rung was sized — risk in USD, SL distance
    as a percentage, margin, and notional position size. The plan
    itself only needs entry / TP / SL / qty, which is what we hand to
    the engine.
    """

    seq: int
    entry: float
    tp: float
    sl: float
    qty: float
    margin: float    # USDT collateral required for this rung
    pos_size: float  # USDT notional of this rung's position
    risk_usd: float  # max loss in USD if this rung's SL fires
    sl_pct: float    # SL distance from entry as % of entry


def fib_prices(zero_price: float, hundred_price: float) -> list[float]:
    """Return the price at every Fibonacci level.

    Linear interpolation works in either direction: when 100% sits
    above 0% the levels ascend; when below, they descend. The trader
    picks the orientation by choosing which price they label as 0%
    and which as 100%, so the same routine handles BUY and SELL.
    """
    range_size = hundred_price - zero_price
    return [zero_price + range_size * level for level in FIB_LEVELS]


def compute_fib_plan(
    *,
    symbol: str,
    side: BlockSide,
    zero_price: float,
    hundred_price: float,
    first_risk_usd: float,
    leverage: float,
    cancel_price: float,
    note: str | None = None,
) -> tuple[BlockPlan, list[FibRungComputed]]:
    """Build a fully-validated :class:`BlockPlan` from the Fib parameters.

    Raises :class:`ValueError` with a descriptive message on any
    inconsistency (zero leverage, zero range, ladder oriented the
    wrong way for the chosen side, etc.) so the Telegram FSM can
    surface the error without crashing.

    Returns a 2-tuple of ``(plan, rungs)`` where ``rungs`` is the
    detailed per-rung breakdown used by the preview formatter.
    """
    if zero_price <= 0 or hundred_price <= 0:
        raise ValueError("Both 0% and 100% prices must be positive.")
    if zero_price == hundred_price:
        raise ValueError("0% and 100% prices must differ.")
    if first_risk_usd <= 0:
        raise ValueError("First-rung risk must be a positive dollar amount.")
    if leverage <= 0:
        raise ValueError("Leverage must be positive.")

    levels = fib_prices(zero_price, hundred_price)
    entries = [levels[i] for i in range(1, 9)]  # indices 1..8
    sls = [levels[i] for i in range(2, 10)]     # indices 2..9 (each SL = next level)

    # The ladder must run in the direction the side implies. For SELL
    # we sell into a rally → entries ascend. For BUY we buy a dip →
    # entries descend. If the user gave 0% and 100% backwards for
    # their chosen side, fail loudly rather than silently produce a
    # broken plan.
    if side == BlockSide.SELL:
        if entries[0] >= entries[-1]:
            raise ValueError(
                "For a SELL block, set 0% < 100% so the ladder ascends "
                f"(got first entry {entries[0]}, last entry {entries[-1]})."
            )
    else:  # BUY
        if entries[0] <= entries[-1]:
            raise ValueError(
                "For a BUY block, set 0% > 100% so the ladder descends "
                f"(got first entry {entries[0]}, last entry {entries[-1]})."
            )

    orders: list[OrderSpec] = []
    rungs: list[FibRungComputed] = []
    risk = first_risk_usd

    for seq, (entry, sl) in enumerate(zip(entries, sls, strict=True), start=1):
        # SL distance as a percentage of the entry price. Always positive.
        if side == BlockSide.SELL:
            sl_pct = (sl - entry) / entry * 100.0
        else:
            sl_pct = (entry - sl) / entry * 100.0
        if sl_pct <= 0:
            # This is a sanity guard: the orientation check above
            # should have caught the user error already.
            raise ValueError(
                f"Rung {seq}: SL% computed as {sl_pct:.4f} (must be > 0). "
                "Check that 0% / 100% are oriented correctly."
            )

        # The trader's formula, verbatim.
        margin = risk * 100.0 / (sl_pct * leverage)
        pos_size = margin * leverage  # notional in USDT
        qty = pos_size / entry

        # TP is the per-rung ratio applied to the SL distance.
        risk_per_share = abs(sl - entry)
        reward_per_share = TP_RATIOS[seq - 1] * risk_per_share
        tp = (
            entry - reward_per_share
            if side == BlockSide.SELL
            else entry + reward_per_share
        )

        orders.append(
            OrderSpec(
                seq=seq,
                entry_price=entry,
                tp_price=tp,
                sl_price=sl,
                qty=qty,
            )
        )
        rungs.append(
            FibRungComputed(
                seq=seq,
                entry=entry,
                tp=tp,
                sl=sl,
                qty=qty,
                margin=margin,
                pos_size=pos_size,
                risk_usd=risk,
                sl_pct=sl_pct,
            )
        )
        risk *= RISK_MULTIPLIER

    if len(orders) != EXPECTED_ORDERS_PER_BLOCK:
        # Defensive — should be impossible given the level constants.
        raise ValueError(
            f"Internal error: produced {len(orders)} orders, expected "
            f"{EXPECTED_ORDERS_PER_BLOCK}."
        )

    plan = BlockPlan(
        symbol=symbol,
        side=side,
        cancel_price=cancel_price,
        orders=orders,
        note=note,
    )
    return plan, rungs


def total_max_risk(rungs: list[FibRungComputed]) -> float:
    """Sum every rung's USD risk — the worst-case loss if every SL fires."""
    return round(sum(r.risk_usd for r in rungs), 4)


def total_margin(rungs: list[FibRungComputed]) -> float:
    """Sum every rung's USDT margin — the collateral the trader needs."""
    return round(sum(r.margin for r in rungs), 4)
