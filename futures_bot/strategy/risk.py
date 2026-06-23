"""Risk progression and lot-size arithmetic for the futures strategy.

Three responsibilities live here, all pure math:

1. **Risk progression** — expand the user's base risk into a list of
   six per-rung risks using a geometric multiplier (default 1.5×).
2. **Lot sizing** — turn each rung's dollar risk into a broker-valid
   lot, honouring the symbol's ``volume_min`` / ``volume_max`` /
   ``volume_step`` and the trader's preferred rounding mode.
3. **Cumulative bookkeeping** — sum risk across rungs and report how
   much the rounded lots actually risk vs. the planned amount.

No I/O, no globals, no broker calls. The :class:`SymbolSpec` dataclass
captures the only piece of broker-side metadata we need so call-sites
in tests can fabricate it instead of mocking MT5.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ---------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------

# Geometric multiplier applied to base risk for each subsequent rung.
# Rung_N_risk = base_risk × 1.5 ** (N - 1).
DEFAULT_RISK_MULTIPLIER: float = 1.5

# How to round a raw lot value to the broker's volume_step grid.
# Strict per the trader's decision: always round UP so the strategy
# accuracy stays at or above 100% (never under-risks). Two other modes
# are kept for testing and future configurability.
RoundingMode = str  # "up" | "down" | "nearest"


# ---------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class SymbolSpec:
    """The pieces of MT5 ``symbol_info`` we need for lot arithmetic.

    Every field maps 1:1 to a property MT5 exposes. We snapshot them
    into a small dataclass so the math layer doesn't need to import
    ``MetaTrader5`` (which only installs on Wine) and so tests can
    instantiate it directly.

    ``trade_tick_value`` is the dollar P&L produced by a one-tick
    price move on a one-lot position. Combined with ``trade_tick_size``
    it lets us compute the dollar loss per lot at any SL distance,
    independent of the symbol class (forex, metal, index, …).
    """

    symbol: str
    trade_tick_size: float      # smallest price increment
    trade_tick_value: float     # USD P&L per tick per 1.0 lot
    volume_min: float           # smallest tradable lot (e.g. 0.01)
    volume_max: float           # largest tradable lot
    volume_step: float          # lot increment (usually equals volume_min)


# ---------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class RungSizing:
    """How a single rung was sized.

    Carries both the planned and the actual numbers so the Telegram
    preview can show the trader the exact effect of lot rounding. The
    bot itself only needs ``lot`` and ``real_risk_usd`` to place the
    order and update its books.
    """

    seq: int
    planned_risk_usd: float     # what the progression asked for
    raw_lot: float              # planned_risk / loss_per_lot, unrounded
    lot: float                  # final lot after step+min/max constraints
    real_risk_usd: float        # actual loss if this rung's SL fires
    accuracy_pct: float         # real_risk / planned_risk × 100


# ---------------------------------------------------------------------
# Risk progression
# ---------------------------------------------------------------------

def risk_for_rung(
    base_risk: float,
    rung_seq: int,
    *,
    multiplier: float = DEFAULT_RISK_MULTIPLIER,
) -> float:
    """Return the planned dollar risk for a 1-based rung index.

    ``base_risk × multiplier ** (rung_seq - 1)``. We do not round here:
    the lot calculator handles the rounding once it knows the
    broker's step size, and rounding twice would corrupt the
    progression.
    """
    if rung_seq < 1:
        raise ValueError(f"rung_seq must be >= 1, got {rung_seq}")
    if base_risk <= 0:
        raise ValueError(f"base_risk must be positive, got {base_risk}")
    if multiplier <= 0:
        raise ValueError(f"multiplier must be positive, got {multiplier}")
    return base_risk * (multiplier ** (rung_seq - 1))


def risk_schedule(
    base_risk: float,
    num_rungs: int,
    *,
    multiplier: float = DEFAULT_RISK_MULTIPLIER,
) -> list[float]:
    """Return ``[risk_1, risk_2, ..., risk_N]`` for a whole block."""
    if num_rungs < 1:
        raise ValueError(f"num_rungs must be >= 1, got {num_rungs}")
    return [
        risk_for_rung(base_risk, seq, multiplier=multiplier)
        for seq in range(1, num_rungs + 1)
    ]


def cumulative_risks(per_rung: list[float]) -> list[float]:
    """Running sum of per-rung risks.

    ``cumulative_risks([1, 1.5, 2.25])`` returns ``[1.0, 2.5, 4.75]``.
    Used both by the TP formula (TP_gross = 3 × cumulative) and by
    the worst-case loss display.
    """
    total = 0.0
    out: list[float] = []
    for r in per_rung:
        total += r
        out.append(total)
    return out


# ---------------------------------------------------------------------
# Lot sizing
# ---------------------------------------------------------------------

def loss_per_lot(symbol: SymbolSpec, sl_distance: float) -> float:
    """Dollar loss per 1.0 lot if price moves ``sl_distance`` against you.

    ``sl_distance`` is in price units (e.g. $3.82 for gold,
    0.001273 for a 12.73-pip EURUSD move). Returns 0 for degenerate
    input so the lot calculator can flag the problem instead of
    crashing.
    """
    if sl_distance <= 0 or symbol.trade_tick_size <= 0:
        return 0.0
    ticks = sl_distance / symbol.trade_tick_size
    return ticks * symbol.trade_tick_value


def _round_to_step(value: float, step: float, mode: RoundingMode) -> float:
    """Snap ``value`` to a multiple of ``step`` using the requested mode.

    Done in integer-step space (multiplying by ``1 / step``) to avoid
    floating-point drift when ``step`` is something like 0.01.
    """
    if step <= 0:
        raise ValueError(f"step must be positive, got {step}")
    ratio = value / step
    if mode == "up":
        snapped = math.ceil(ratio)
    elif mode == "down":
        snapped = math.floor(ratio)
    elif mode == "nearest":
        # Python's ``round`` uses banker's rounding for .5 cases;
        # that is acceptable here because the difference is one step
        # in the rare exactly-half case.
        snapped = round(ratio)
    else:
        raise ValueError(
            f"unknown rounding mode {mode!r}; expected 'up' | 'down' | 'nearest'"
        )
    # Round the final product to mitigate floating-point noise like
    # 0.30000000000000004 when step=0.01.
    return round(snapped * step, 8)


def calculate_lot(
    *,
    symbol: SymbolSpec,
    risk_usd: float,
    sl_distance: float,
    mode: RoundingMode = "up",
) -> tuple[float, float]:
    """Compute the broker-valid lot for one rung.

    Returns a ``(lot, raw_lot)`` tuple where ``raw_lot`` is the
    unrounded ideal so the caller can report how much rounding
    happened. ``lot`` is guaranteed to satisfy
    ``volume_min <= lot <= volume_max`` and to land exactly on the
    ``volume_step`` grid.

    Raises :class:`ValueError` only on programmer error (bad
    rounding mode, bad symbol spec). When the raw lot lands *below*
    ``volume_min`` we still return ``volume_min`` rather than zero —
    the higher layer can decide whether to abort the block or accept
    the over-risk on a tiny rung.
    """
    lpl = loss_per_lot(symbol, sl_distance)
    if lpl <= 0:
        # Degenerate spec: report 0 instead of raising. The caller
        # validates this before placing orders.
        return 0.0, 0.0

    raw = risk_usd / lpl
    snapped = _round_to_step(raw, symbol.volume_step, mode)

    # Clip to the broker's bounds.
    if snapped < symbol.volume_min:
        snapped = symbol.volume_min
    if snapped > symbol.volume_max:
        snapped = symbol.volume_max

    return snapped, raw


def size_rungs(
    *,
    symbol: SymbolSpec,
    sl_distance: float,
    base_risk: float,
    num_rungs: int,
    multiplier: float = DEFAULT_RISK_MULTIPLIER,
    mode: RoundingMode = "up",
) -> list[RungSizing]:
    """Produce the full :class:`RungSizing` table for a block.

    The output is one item per rung, in seq order. Each carries the
    planned vs. realised risk and accuracy so the Telegram preview
    can highlight rungs where rounding hurt the strategy the most
    (typically rung 1 on cheap base-risk plans).
    """
    risks = risk_schedule(base_risk, num_rungs, multiplier=multiplier)
    lpl = loss_per_lot(symbol, sl_distance)
    out: list[RungSizing] = []

    for seq, planned in enumerate(risks, start=1):
        lot, raw = calculate_lot(
            symbol=symbol,
            risk_usd=planned,
            sl_distance=sl_distance,
            mode=mode,
        )
        real_risk = lot * lpl
        accuracy = (real_risk / planned * 100.0) if planned > 0 else 0.0
        out.append(
            RungSizing(
                seq=seq,
                planned_risk_usd=round(planned, 4),
                raw_lot=round(raw, 6),
                lot=lot,
                real_risk_usd=round(real_risk, 4),
                accuracy_pct=round(accuracy, 2),
            )
        )

    return out


def min_viable_base_risk(symbol: SymbolSpec, sl_distance: float) -> float:
    """Smallest base-risk that puts rung 1 on a real lot (not just min lot).

    Useful for the Telegram FSM: when the trader enters too small a
    base-risk, the bot can answer "use at least $X or pick a cheaper
    symbol" instead of silently placing six min-lot orders.
    """
    return round(symbol.volume_min * loss_per_lot(symbol, sl_distance), 4)
