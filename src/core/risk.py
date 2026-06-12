"""Per-order and per-block risk helpers.

Risk in this strategy is the loss the trader accepts if the SL fires
right after the entry fills, expressed in quote-asset units (USDT for
USDT-M Futures). The arithmetic is identical for both directions, but
which price is the "loss leg" flips between BUY and SELL — that flip is
the only reason these helpers exist outside the model itself.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.db.enums import BlockSide


def order_risk_amount(
    *,
    side: BlockSide,
    entry_price: float,
    sl_price: float,
    qty: float,
) -> float:
    """Return the worst-case dollar loss of a single rung.

    For BUY: ``risk = (entry - sl) * qty`` (sl is below entry).
    For SELL: ``risk = (sl - entry) * qty`` (sl is above entry).
    Returns 0 if the inputs are degenerate (matching the convention
    used elsewhere — never raise from a math helper that may run inside
    a notification render).
    """
    if qty <= 0 or entry_price <= 0 or sl_price <= 0:
        return 0.0
    if side == BlockSide.BUY:
        delta = entry_price - sl_price
    else:
        delta = sl_price - entry_price
    return max(0.0, delta * qty)


def total_block_risk(
    *,
    side: BlockSide,
    rungs: Iterable[tuple[float, float, float]],
) -> float:
    """Return the cumulative risk of a block.

    ``rungs`` is any iterable of ``(entry_price, sl_price, qty)``
    triples — usable both before persistence (from a BlockPlan) and
    after (from Order rows). Doesn't account for chain-mode netting
    because, by Variant A, every rung's SL stands on its own.
    """
    return round(
        sum(
            order_risk_amount(
                side=side, entry_price=e, sl_price=s, qty=q
            )
            for e, s, q in rungs
        ),
        6,
    )
