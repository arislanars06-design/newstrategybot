"""Helpers for rounding prices and quantities to exchange precision."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def round_to_step(value: Decimal | float | str, step: Decimal) -> Decimal:
    """Round ``value`` down to the nearest multiple of ``step``.

    Rounding *down* is the safe default for both prices and quantities:
    - For buy prices it keeps you slightly cheaper than requested.
    - For sell prices it keeps you slightly cheaper than requested too,
      but the cancel/TP/SL placement uses the user-supplied price as
      authoritative anyway, so the small downward bias is acceptable.
    - For quantities it ensures we never exceed available margin or
      requested size.
    """
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if step <= 0:
        return value
    return (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def format_decimal(value: Decimal, precision: int) -> str:
    """Format a Decimal to a fixed number of decimal places, no exponent."""
    quant = Decimal(10) ** -precision if precision > 0 else Decimal("1")
    return str(value.quantize(quant))
