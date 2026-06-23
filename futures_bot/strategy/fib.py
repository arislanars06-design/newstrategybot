"""Uniform-gap Fibonacci ladder used by the futures strategy.

The crypto bot uses the classic Fibonacci retracement levels
(61.8 / 70.2 / 78.6 / 89.3 / 100 / 111.8 / 123.6 / 130.9). Those gaps
are uneven, so the SL distance — and therefore the spread tolerance —
changes from rung to rung. The futures strategy replaces them with a
**six-rung uniform ladder** anchored to the same outer points the
trader knows from charting:

* ``0%``   — anchor zero (the side of the range the price starts at)
* ``138.2%`` — final stop-loss beyond the last entry

The interval between them is split into seven equal segments of
``76.4 / 6 ≈ 12.73 %`` each:

    0% ── 61.8% ── 74.53% ── 87.26% ── 100% ── 112.73% ── 125.46% ── 138.2%

Indices 1..6 are the six **entry** levels; the SL of each entry sits
at the next level (index 2..7). Because every gap is identical, every
rung has identical SL distance — which makes the lot sizing, the
spread guard, and the chain accounting much easier to reason about.

Module is pure math — no I/O, no globals, no broker calls. Safe to
import from tests and to use inside Telegram message formatters.
"""

from __future__ import annotations

from dataclasses import dataclass

# Number of entry rungs in one block. Six is the contract baked into
# the strategy spec; changing it would invalidate the cumulative-risk
# / TP-multiplier table the trader designed.
NUM_RUNGS: int = 6

# The Fibonacci levels that bookend the ladder. Everything else is
# derived from these two anchors and ``NUM_RUNGS``.
ENTRY_START_PCT: float = 0.618    # first entry sits here
FINAL_SL_PCT: float = 1.382       # last SL sits here

# Computed once so call-sites can read it as a constant. Equals
# (1.382 - 0.618) / 6 = 0.12733... — the famous "12.73 %" gap.
GAP_PCT: float = (FINAL_SL_PCT - ENTRY_START_PCT) / NUM_RUNGS


@dataclass(slots=True, frozen=True)
class FibLevels:
    """Resolved Fibonacci price levels for a single block.

    All eight levels live in one object so call-sites don't need to
    remember which index is which. The order is always
    ``[anchor_zero, entry_1, entry_2, ..., entry_6, final_sl]``.

    For a BUY block the user passes ``zero_price`` ABOVE
    ``hundred_price`` — the ladder descends, you buy each dip.
    For a SELL block ``zero_price`` is BELOW ``hundred_price`` and the
    ladder ascends. The math is identical because we interpolate on
    signed deltas.
    """

    zero_price: float
    hundred_price: float
    prices: tuple[float, ...]  # length NUM_RUNGS + 2 = 8

    @property
    def range_size(self) -> float:
        """Absolute distance between the 0% and 100% anchors."""
        return abs(self.hundred_price - self.zero_price)

    @property
    def signed_range(self) -> float:
        """Signed distance (negative for BUY blocks where the ladder descends)."""
        return self.hundred_price - self.zero_price

    @property
    def sl_distance(self) -> float:
        """Per-rung SL distance — identical for every rung, in price units."""
        return self.range_size * GAP_PCT

    @property
    def entries(self) -> tuple[float, ...]:
        """The six entry prices, rung 1 first."""
        return self.prices[1 : NUM_RUNGS + 1]

    @property
    def final_sl(self) -> float:
        """SL price for the deepest rung — same as the 138.2% level."""
        return self.prices[-1]

    def sl_for_rung(self, rung_seq: int) -> float:
        """Return the SL price for rung ``rung_seq`` (1-based).

        By the chain rule the SL of rung N equals the entry of rung
        N+1. The last rung's SL is the explicit 138.2% level (no next
        entry exists). Indices are guarded with explicit checks
        because off-by-ones here would silently corrupt the ladder.
        """
        if not 1 <= rung_seq <= NUM_RUNGS:
            raise IndexError(
                f"rung_seq must be in 1..{NUM_RUNGS}, got {rung_seq}"
            )
        # prices[0] = anchor 0%
        # prices[1] = rung 1 entry
        # prices[2] = rung 2 entry (= rung 1 SL)  ← shift by 1
        # ...
        # prices[7] = final SL
        return self.prices[rung_seq + 1]

    def entry_for_rung(self, rung_seq: int) -> float:
        """Return the entry price for rung ``rung_seq`` (1-based)."""
        if not 1 <= rung_seq <= NUM_RUNGS:
            raise IndexError(
                f"rung_seq must be in 1..{NUM_RUNGS}, got {rung_seq}"
            )
        return self.prices[rung_seq]


def level_percentages() -> tuple[float, ...]:
    """Return the eight Fibonacci percentages used by the ladder.

    The first item is the explicit 0% anchor, items 1..6 are the six
    entry levels, and the last is the 138.2% final SL. Useful for
    tests, debug formatters, and Telegram previews.

    Returned order:
    ``(0, 61.8, 74.53, 87.27, 100, 112.73, 125.47, 138.2)``.
    """
    return (0.0,) + tuple(
        ENTRY_START_PCT + GAP_PCT * i for i in range(NUM_RUNGS + 1)
    )


def compute_levels(zero_price: float, hundred_price: float) -> FibLevels:
    """Build a :class:`FibLevels` instance from two anchor prices.

    The interpolation is linear in price space. We do not require
    ``zero_price < hundred_price`` — the trader supplies them in the
    direction that makes sense for the block side (BUY vs SELL), and
    the helpers above will return correctly-ordered ladders either
    way.

    Raises :class:`ValueError` on degenerate input so the FSM can
    surface the problem before any order leaves the bot.
    """
    if zero_price <= 0 or hundred_price <= 0:
        raise ValueError("Both anchor prices must be positive.")
    if zero_price == hundred_price:
        raise ValueError("0% and 100% anchor prices must differ.")

    signed_delta = hundred_price - zero_price
    pcts = level_percentages()
    levels = tuple(zero_price + signed_delta * pct for pct in pcts)

    return FibLevels(
        zero_price=zero_price,
        hundred_price=hundred_price,
        prices=levels,
    )
