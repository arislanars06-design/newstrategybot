"""Fibonacci-based block plan generation.

Implements the trader's variant of the strategy where the bot — not
the chart — decides the prices. The trader provides only:

* the symbol and side (BUY / SELL)
* the 0% and 100% anchor prices defining the Fibonacci range
* the first rung's dollar risk
* the leverage (which the bot also pushes to Binance)

The cancel ("price invalid") price is auto-derived from the 0%
anchor — see :func:`fib_leverage` in the bot layer.

The bot computes everything else:

* **6 entry prices** at Fibonacci levels 61.8, 74.53, 87.26, 100,
  112.73 and 125.46 percent of the user-supplied range. The levels
  are uniformly spaced at ~12.73 % apart inside the 0.618..1.382
  band, so every rung has the same SL distance in fraction-of-range
  terms.
* 6 chained stop-losses where each rung's SL equals the next rung's
  entry (chain mechanism). Rung 6's SL sits at level 138.2 % — one
  Fib step beyond the last entry.
* 6 take-profits at a uniform **1:7 risk-to-reward ratio**: each TP
  sits at ``entry + 7 × SL_distance`` (for BUY) or
  ``entry - 7 × SL_distance`` (for SELL).
* Per-rung dollar risk that **doubles** each step (``RISK_MULTIPLIER
  = 2.0``), so the trader enters a single number ("first risk") and
  the bot expands it across the ladder: 1, 2, 4, 8, 16, 32 ×
  first_risk.
* Per-rung margin (and the resulting qty) computed from
  ``risk * 100 / (sl_pct * leverage)`` — exactly the formula the
  trader laid out.

Total ladder size:
  Max planned loss = first_risk × (1 + 2 + 4 + 8 + 16 + 32) = 63×.
  At first_risk=$1 that's $63; at $10 it's $630.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.plan import EXPECTED_ORDERS_PER_BLOCK, BlockPlan, OrderSpec
from src.db.enums import BlockSide

# Standard Fibonacci levels used by the strategy. 8 levels = 1 cancel
# anchor + 6 entries + 1 final SL.
# Entries occupy indices 1..6 (61.8 % through 125.46 %).
# SL of rung N sits at index N+1, so rung 6's SL is at index 7 = 138.2 %.
# The 0.618..1.382 band is divided into 6 equal segments of ~12.73 %
# each, giving every rung the same SL distance as a fraction of the
# user-supplied 0%..100% range.
FIB_LEVELS: list[float] = [
    0.0,
    0.6180,
    0.7453,
    0.8726,
    1.0000,
    1.1273,
    1.2546,
    1.3820,
]

# Risk:reward ratios per rung. Uniform 1:7 across the whole ladder.
TP_RATIOS: list[float] = [7.0, 7.0, 7.0, 7.0, 7.0, 7.0]

# Risk grows by this factor each rung.
RISK_MULTIPLIER: float = 2.0

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
    # Slices are derived from EXPECTED_ORDERS_PER_BLOCK so the formula
    # adapts automatically if the rung count is ever tuned again.
    # Entries take indices 1..N (skipping 0 which is the cancel anchor).
    # SLs take indices 2..N+1 — each rung's SL equals the next rung's
    # entry, and the last rung's SL is one Fib step beyond the last
    # entry. FIB_LEVELS must therefore contain exactly N+2 elements
    # (1 cancel anchor + N entries + 1 final SL).
    n = EXPECTED_ORDERS_PER_BLOCK
    entries = [levels[i] for i in range(1, n + 1)]
    sls = [levels[i] for i in range(2, n + 2)]

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
