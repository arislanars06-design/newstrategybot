"""Spread sanity checks for the futures strategy.

The chain rule on which the whole block depends only works while
spread is small relative to the Fibonacci gap. With a 12.73% gap
that means: *as long as spread stays under ~20% of the gap*, the
SL of rung N and the entry of rung N+1 can be made to fire on the
same tick by the spread-aware adjustment in
:mod:`futures_bot.strategy.tp`. Above that ratio, the buffer
required to keep the chain alive eats too much of each rung's
risk envelope and the strategy degrades.

This module enforces three guards:

1. **Static pre-flight** — refuse to *create* a block whose range
   is too small for the current spread (a one-shot check at
   ``/newblock`` time).
2. **Per-order pre-flight** — refuse to *place* an order whose
   spread at the moment of placement is wildly higher than what the
   trader saw on the preview screen.
3. **Runtime monitor** — emit alerts (and optionally pause new
   blocks) when spread on any active block widens past configured
   thresholds. The order-watcher uses this on every fill.

All three return small result objects rather than raising, so
callers can present a friendly message in Telegram instead of
catching exceptions. Spread guard is *not* the place to make
trading decisions on its own — it only reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Inputs to the guard are dimensionless ratios so it doesn't matter
# whether spread is in pips, dollars, or points: the math only cares
# about "spread vs gap" and "spread vs typical-spread".
#
# Defaults track the strategy spec we agreed on:
#  - max 20% of the smallest Fibonacci gap (i.e. the uniform 12.73%)
#  - alerting when spread doubles vs typical
#  - hard refusal when spread quintuples
DEFAULT_MAX_SPREAD_TO_GAP_RATIO: float = 0.20
DEFAULT_ALERT_RATIO: float = 2.0
DEFAULT_BLOCK_RATIO: float = 5.0

# Same uniform gap value computed in :mod:`futures_bot.strategy.fib`.
# Importing it here would create a cycle (fib doesn't depend on
# spread_guard), so we re-declare the constant and a unit test pins
# both definitions together.
GAP_PCT: float = 0.127333


class SpreadSeverity(StrEnum):
    """How bad the current spread is, relative to the typical value."""

    OK = "OK"             # within tolerance
    ALERT = "ALERT"       # widened but not enough to refuse new blocks
    BLOCKED = "BLOCKED"   # too wide; new blocks must wait


# ---------------------------------------------------------------------
# Static pre-flight: is the block range itself big enough?
# ---------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class RangeCheck:
    """Result of the pre-creation range check."""

    ok: bool
    reason: str
    range_size: float
    spread: float
    min_gap: float
    ratio: float                 # spread / min_gap
    recommended_min_range: float


def check_block_range(
    *,
    range_size: float,
    spread: float,
    max_ratio: float = DEFAULT_MAX_SPREAD_TO_GAP_RATIO,
    safety: float = 1.5,
) -> RangeCheck:
    """Return whether ``range_size`` is big enough for ``spread``.

    The smallest Fibonacci gap on the uniform ladder equals
    ``range_size × GAP_PCT``. For the chain to survive the
    spread-aware SL adjustment, that gap must accommodate at least
    ``spread × safety`` of buffer plus some headroom — i.e.

        spread <= max_ratio × (range_size × GAP_PCT)

    which we invert to get the minimum range:

        range_min = spread / (max_ratio × GAP_PCT)

    A ``safety`` multiplier on top compensates for live-spread
    widening; 1.5 is the same default we use for SL adjustments so
    both numbers tell the same story.
    """
    if range_size <= 0:
        return RangeCheck(
            ok=False,
            reason="range_size must be positive",
            range_size=range_size,
            spread=spread,
            min_gap=0.0,
            ratio=float("inf"),
            recommended_min_range=0.0,
        )
    if spread < 0:
        return RangeCheck(
            ok=False,
            reason="spread cannot be negative",
            range_size=range_size,
            spread=spread,
            min_gap=0.0,
            ratio=float("inf"),
            recommended_min_range=0.0,
        )

    min_gap = range_size * GAP_PCT
    ratio = spread / min_gap if min_gap > 0 else float("inf")
    recommended_min = (
        spread / (max_ratio * GAP_PCT) * safety if spread > 0 else 0.0
    )

    if ratio > max_ratio:
        return RangeCheck(
            ok=False,
            reason=(
                f"spread {spread:.5g} is too large for this range "
                f"(uses {ratio:.0%} of the {min_gap:.5g} gap; "
                f"max {max_ratio:.0%}). "
                f"Use range ≥ {recommended_min:.5g}."
            ),
            range_size=range_size,
            spread=spread,
            min_gap=min_gap,
            ratio=ratio,
            recommended_min_range=recommended_min,
        )

    return RangeCheck(
        ok=True,
        reason="OK",
        range_size=range_size,
        spread=spread,
        min_gap=min_gap,
        ratio=ratio,
        recommended_min_range=recommended_min,
    )


# ---------------------------------------------------------------------
# Runtime monitor: did the live spread blow out vs typical?
# ---------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class SpreadReading:
    """Classification of a live spread reading vs typical."""

    severity: SpreadSeverity
    spread: float
    typical_spread: float
    ratio: float                 # spread / typical_spread
    message: str


def classify_spread(
    *,
    spread: float,
    typical_spread: float,
    alert_ratio: float = DEFAULT_ALERT_RATIO,
    block_ratio: float = DEFAULT_BLOCK_RATIO,
) -> SpreadReading:
    """Map a (live, typical) spread pair onto a severity bucket.

    The thresholds are deliberately conservative defaults: at 2×
    typical we warn but keep trading; at 5× we refuse new blocks.
    The trader can override both via the config file once the bot
    has a few weeks of real data.

    Returns ``OK`` on degenerate inputs so logging code can safely
    pass through whatever the broker reports without raising.
    """
    if typical_spread <= 0 or spread < 0:
        return SpreadReading(
            severity=SpreadSeverity.OK,
            spread=spread,
            typical_spread=typical_spread,
            ratio=0.0,
            message="no typical spread available",
        )

    ratio = spread / typical_spread

    if ratio >= block_ratio:
        return SpreadReading(
            severity=SpreadSeverity.BLOCKED,
            spread=spread,
            typical_spread=typical_spread,
            ratio=ratio,
            message=(
                f"spread {spread:.5g} is {ratio:.1f}× the typical "
                f"{typical_spread:.5g}; refusing to open new blocks"
            ),
        )
    if ratio >= alert_ratio:
        return SpreadReading(
            severity=SpreadSeverity.ALERT,
            spread=spread,
            typical_spread=typical_spread,
            ratio=ratio,
            message=(
                f"spread widened to {ratio:.1f}× typical "
                f"({spread:.5g} vs {typical_spread:.5g})"
            ),
        )
    return SpreadReading(
        severity=SpreadSeverity.OK,
        spread=spread,
        typical_spread=typical_spread,
        ratio=ratio,
        message="spread normal",
    )


# ---------------------------------------------------------------------
# Combined helper for the FSM
# ---------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class PreflightReport:
    """Result of the combined pre-flight check used by ``/newblock``."""

    ok: bool
    range_check: RangeCheck
    spread_reading: SpreadReading


def preflight_block(
    *,
    range_size: float,
    spread: float,
    typical_spread: float,
    max_ratio: float = DEFAULT_MAX_SPREAD_TO_GAP_RATIO,
    alert_ratio: float = DEFAULT_ALERT_RATIO,
    block_ratio: float = DEFAULT_BLOCK_RATIO,
    safety: float = 1.5,
) -> PreflightReport:
    """One-call helper bundling both static and runtime checks."""
    rng = check_block_range(
        range_size=range_size,
        spread=spread,
        max_ratio=max_ratio,
        safety=safety,
    )
    spr = classify_spread(
        spread=spread,
        typical_spread=typical_spread,
        alert_ratio=alert_ratio,
        block_ratio=block_ratio,
    )
    return PreflightReport(
        ok=rng.ok and spr.severity != SpreadSeverity.BLOCKED,
        range_check=rng,
        spread_reading=spr,
    )
