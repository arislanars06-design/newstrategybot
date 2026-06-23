"""Take-profit and spread-aware stop-loss pricing.

The trader's TP rule is delightfully simple:

    TP_gross[N] = TP_MULTIPLIER × cumulative_real_risk[N]

where ``cumulative_real_risk`` includes the *actual* (post-rounding)
risk of rungs 1..N. Using the realised numbers — not the planned
ones — keeps the strategy honest: when rounding made rung 1 risk
$7.64 instead of $5.00, the TP must clear $22.92 (= 3 × 7.64) for
rung 1 to fully repay that lump.

The same module also handles the chain-rule SL adjustment: BUY orders
fill on the ASK, SL fires on the BID, so naively setting the SL of
rung N to ``next_entry`` leaves a one-spread dead zone where rung N
has closed but rung N+1 has not yet opened. The fix is to push the SL
``spread × safety_multiplier`` deeper, so the BID hitting the SL is
the same instant the ASK falls to the next entry. Mirror for SELL.

This module is pure math; it never touches MT5, the DB, or Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass

from futures_bot.db.enums import BlockSide

# Default fallback when no config is available (mainly for tests).
# The runtime value is read from ``FB_TP_MULTIPLIER`` and passed in
# explicitly by callers.
DEFAULT_TP_MULTIPLIER: float = 3.0


@dataclass(slots=True, frozen=True)
class FillContext:
    """The fill-time information we need to place SL and TP.

    Snapped from MT5 at the exact moment ``order N`` triggers.
    Bundled into one object so the order-watcher can hand it to both
    ``compute_sl_price`` and ``compute_tp_price`` without forgetting
    a field. Spread is the live bid/ask gap, not the broker's
    advertised typical spread.
    """

    entry_price: float        # the actual price at which the order filled
    spread: float             # ask - bid at fill time
    side: BlockSide


def compute_sl_price(
    *,
    next_entry: float,
    fill: FillContext,
    safety_multiplier: float,
) -> float:
    """SL price that keeps the chain unbroken.

    Why subtract ``spread × safety``? When rung N's SL fires
    (BID = sl), rung N+1's limit fills (ASK = next_entry). For both
    to happen at the same moment we need

        next_entry - sl = spread

    plus a safety multiplier so news-induced spread widening doesn't
    snap the chain. The multiplier defaults to 1.5; the trader can
    tune it via ``FB_SL_SPREAD_SAFETY``.

    For SELL the geometry mirrors: the chain rolls up, so the SL
    sits ABOVE next_entry by the same buffer.
    """
    if safety_multiplier <= 0:
        raise ValueError(
            f"safety_multiplier must be positive, got {safety_multiplier}"
        )
    if fill.spread < 0:
        # Crossed market — shouldn't happen on a real broker but
        # defend against bad data anyway.
        raise ValueError(f"spread must be >= 0, got {fill.spread}")

    buffer = fill.spread * safety_multiplier
    if fill.side == BlockSide.BUY:
        return round(next_entry - buffer, 8)
    return round(next_entry + buffer, 8)


def compute_tp_price(
    *,
    fill: FillContext,
    lot: float,
    cumulative_real_risk_usd: float,
    usd_per_price_unit: float,
    tp_multiplier: float = DEFAULT_TP_MULTIPLIER,
) -> float:
    """Take-profit price for the just-filled rung.

    ``usd_per_price_unit`` is the position's P&L for a 1.0-unit move
    in price space — i.e. ``lot × (tick_value / tick_size)``. Asking
    the caller to compute it once keeps this function symbol-agnostic
    and the symbol metadata out of the math layer.

    The arithmetic:

    1. Required gross win in USD: ``tp_multiplier × cumulative_real_risk``.
    2. Price distance that yields that win: ``tp_gross / usd_per_price_unit``.
    3. Spread compensation: BUY positions close on the BID, so add
       one spread to the TP to recover what we paid crossing the
       spread on entry. SELL subtracts.

    Spread compensation is applied here (not at SL) because winning
    trades pay the spread; losing trades have already had the spread
    folded into the chain-rule adjustment in :func:`compute_sl_price`.
    """
    if lot <= 0:
        raise ValueError(f"lot must be positive, got {lot}")
    if usd_per_price_unit <= 0:
        raise ValueError(
            f"usd_per_price_unit must be positive, got {usd_per_price_unit}"
        )
    if cumulative_real_risk_usd <= 0:
        raise ValueError(
            f"cumulative_real_risk_usd must be positive, "
            f"got {cumulative_real_risk_usd}"
        )
    if tp_multiplier <= 0:
        raise ValueError(f"tp_multiplier must be positive, got {tp_multiplier}")
    if fill.spread < 0:
        raise ValueError(f"spread must be >= 0, got {fill.spread}")

    tp_gross_usd = tp_multiplier * cumulative_real_risk_usd
    tp_distance = tp_gross_usd / usd_per_price_unit

    if fill.side == BlockSide.BUY:
        return round(fill.entry_price + tp_distance + fill.spread, 8)
    return round(fill.entry_price - tp_distance - fill.spread, 8)


def usd_per_price_unit_from_lot(
    *,
    lot: float,
    trade_tick_size: float,
    trade_tick_value: float,
) -> float:
    """Dollar P&L per 1.0-unit price move at the given lot size.

    Helper so :func:`compute_tp_price` callers don't have to inline
    the arithmetic at every call-site. Returns 0 on degenerate
    inputs — matches the rest of the math module's "never raise from
    a render-time helper" convention.
    """
    if lot <= 0 or trade_tick_size <= 0 or trade_tick_value <= 0:
        return 0.0
    return lot * trade_tick_value / trade_tick_size
