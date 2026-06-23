"""High-level block-plan builder for the futures strategy.

This module is the entry point the Telegram FSM and the engine call
when they have the user's five inputs (symbol, side, two anchor
prices, base risk, cancel price) and need a fully-sized,
broker-valid plan back. It pulls together the lower-level helpers
in this package:

* :mod:`futures_bot.strategy.fib` — Fibonacci levels and SL distance.
* :mod:`futures_bot.strategy.risk` — risk progression and lot sizing.

The resulting :class:`BlockPlan` holds everything required to place
the six pending limit orders on the broker. The take-profit prices
are intentionally *not* computed here: they depend on the live
spread at fill time, so the order-watcher computes them just-in-time
from :mod:`futures_bot.strategy.tp` when each rung opens.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from futures_bot.db.enums import BlockSide
from futures_bot.strategy.fib import NUM_RUNGS, FibLevels, compute_levels
from futures_bot.strategy.risk import (
    DEFAULT_RISK_MULTIPLIER,
    RungSizing,
    SymbolSpec,
    cumulative_risks,
    loss_per_lot,
    size_rungs,
)


# ---------------------------------------------------------------------
# Plan output
# ---------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class PlanRung:
    """A single rung in the block plan.

    No take-profit field — TP is computed at fill time when the live
    spread is known. The chain-rule SL price *is* present because we
    need it on the broker the moment we place the entry limit (some
    brokers reject orders that have no protective SL attached).
    """

    seq: int
    entry: float          # limit price for this rung's entry
    sl: float             # chain-rule SL (= next rung's entry, will be
                          # adjusted at fill time once spread is known)
    lot: float            # broker-valid lot (post step+min/max rounding)
    planned_risk_usd: float
    real_risk_usd: float
    accuracy_pct: float


@dataclass(slots=True)
class BlockPlan:
    """Complete plan for one block, ready to be persisted and placed.

    Mutable on purpose (``slots=True`` without ``frozen``) so the
    engine can fill in IDs assigned by the broker without rebuilding
    the dataclass. Lighter than splitting into "plan vs placed
    block" record types, and the planning step itself is short-lived.
    """

    symbol: str
    side: BlockSide

    # Anchors as supplied by the user. Useful in audit logs and for
    # regenerating the plan deterministically from inputs.
    zero_price: float
    hundred_price: float

    base_risk_usd: float

    # Cumulative SL distance (i.e. range × 12.73%). Same for every
    # rung because the ladder is uniform; cached at plan time so the
    # order-watcher and notifier don't recompute it.
    sl_distance: float

    cancel_price: float

    rungs: list[PlanRung] = field(default_factory=list)

    note: str | None = None

    # ---- Derived helpers (cheap, not cached) ----

    def total_planned_risk(self) -> float:
        """Sum of every rung's planned (pre-rounding) risk."""
        return round(sum(r.planned_risk_usd for r in self.rungs), 4)

    def total_real_risk(self) -> float:
        """Sum of every rung's real (post-rounding) risk — worst case."""
        return round(sum(r.real_risk_usd for r in self.rungs), 4)

    def cumulative_real_risk(self) -> list[float]:
        """Running real-risk total — needed by the TP formula."""
        return cumulative_risks([r.real_risk_usd for r in self.rungs])

    def total_lot(self) -> float:
        """Sum of all rung lots — for the margin-check display."""
        return round(sum(r.lot for r in self.rungs), 4)


# ---------------------------------------------------------------------
# Build entry point
# ---------------------------------------------------------------------

def build_plan(
    *,
    symbol: str,
    side: BlockSide,
    zero_price: float,
    hundred_price: float,
    base_risk_usd: float,
    cancel_price: float,
    symbol_spec: SymbolSpec,
    risk_multiplier: float = DEFAULT_RISK_MULTIPLIER,
    lot_rounding: str = "up",
    note: str | None = None,
) -> BlockPlan:
    """Construct a :class:`BlockPlan` from the five trader inputs.

    Validations are done up-front so the FSM can show the error
    before any order leaves the bot. We delegate the heavy lifting
    to :mod:`fib` and :mod:`risk` — this function only orchestrates
    them and packages the result.

    The resulting plan obeys the orientation invariants the engine
    will later double-check before placing orders:

    * BUY block: entries descend, cancel price is *above* every entry.
    * SELL block: entries ascend, cancel price is *below* every entry.
    """
    if base_risk_usd <= 0:
        raise ValueError(
            f"base_risk_usd must be positive, got {base_risk_usd}"
        )
    if cancel_price <= 0:
        raise ValueError(f"cancel_price must be positive, got {cancel_price}")

    levels = compute_levels(zero_price, hundred_price)
    _validate_orientation(side, levels, cancel_price)

    sizings = size_rungs(
        symbol=symbol_spec,
        sl_distance=levels.sl_distance,
        base_risk=base_risk_usd,
        num_rungs=NUM_RUNGS,
        multiplier=risk_multiplier,
        mode=lot_rounding,
    )

    rungs = _assemble_rungs(levels=levels, sizings=sizings)

    return BlockPlan(
        symbol=symbol,
        side=side,
        zero_price=zero_price,
        hundred_price=hundred_price,
        base_risk_usd=base_risk_usd,
        sl_distance=levels.sl_distance,
        cancel_price=cancel_price,
        rungs=rungs,
        note=note,
    )


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------

def _assemble_rungs(
    *,
    levels: FibLevels,
    sizings: list[RungSizing],
) -> list[PlanRung]:
    """Glue Fibonacci prices to risk sizings, one rung at a time.

    The SL stored here is the *unadjusted* chain-rule SL — i.e. the
    next rung's entry, or the 138.2% final SL for the last rung.
    Spread adjustment happens at fill time in the order-watcher, so
    the plan stays deterministic and the persisted DB row remains
    meaningful even if the live spread changes between plan and fill.
    """
    out: list[PlanRung] = []
    for sizing in sizings:
        entry = levels.entry_for_rung(sizing.seq)
        sl = levels.sl_for_rung(sizing.seq)
        out.append(
            PlanRung(
                seq=sizing.seq,
                entry=entry,
                sl=sl,
                lot=sizing.lot,
                planned_risk_usd=sizing.planned_risk_usd,
                real_risk_usd=sizing.real_risk_usd,
                accuracy_pct=sizing.accuracy_pct,
            )
        )
    return out


def _validate_orientation(
    side: BlockSide,
    levels: FibLevels,
    cancel_price: float,
) -> None:
    """Cross-check the trader's anchor choice against the block side.

    We want the bot to fail loudly on "I picked BUY but typed the
    anchors as if I were selling" — a far more common mistake than
    a price typo, and one that the strategy math wouldn't catch on
    its own.
    """
    entries = levels.entries

    if side == BlockSide.BUY:
        # Buying a dip: entries descend, cancel sits above the top.
        if entries[0] <= entries[-1]:
            raise ValueError(
                "BUY block requires zero_price > hundred_price so the "
                "ladder descends (first entry %g, last entry %g)."
                % (entries[0], entries[-1])
            )
        if cancel_price <= entries[0]:
            raise ValueError(
                "BUY block cancel_price (%g) must be ABOVE the first "
                "entry (%g)." % (cancel_price, entries[0])
            )
    elif side == BlockSide.SELL:
        # Selling a rally: entries ascend, cancel sits below.
        if entries[0] >= entries[-1]:
            raise ValueError(
                "SELL block requires zero_price < hundred_price so the "
                "ladder ascends (first entry %g, last entry %g)."
                % (entries[0], entries[-1])
            )
        if cancel_price >= entries[0]:
            raise ValueError(
                "SELL block cancel_price (%g) must be BELOW the first "
                "entry (%g)." % (cancel_price, entries[0])
            )
    else:  # pragma: no cover — StrEnum constrains values
        raise ValueError(f"unknown block side: {side!r}")


def estimate_loss_per_lot_at_sl(
    *,
    symbol_spec: SymbolSpec,
    sl_distance: float,
) -> float:
    """Convenience re-export used by the Telegram preview.

    Some formatters want to show the symbol's "loss per 1.0 lot" line
    without importing :mod:`risk` directly.
    """
    return loss_per_lot(symbol_spec, sl_distance)
