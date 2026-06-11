"""Enumerations used across the database layer.

Stored as their string values in the database so they remain readable in
raw SQL and survive renames at the Python level (as long as the value
strings are kept stable).
"""

from __future__ import annotations

from enum import StrEnum


class BlockSide(StrEnum):
    """Direction of every order in a block."""

    BUY = "BUY"   # long: entries below current price, TP above, SL below
    SELL = "SELL"  # short: entries above current price, TP below, SL above


class BlockStatus(StrEnum):
    """Lifecycle of a block."""

    CREATED = "CREATED"   # created in DB, orders not yet placed on exchange
    ACTIVE = "ACTIVE"     # orders placed, watching the market
    WIN = "WIN"           # at least one order reached TP
    LOSS = "LOSS"         # all 8 orders ended at SL
    INVALID = "INVALID"   # cancel-price hit before any order triggered
    ERROR = "ERROR"       # unrecoverable problem; needs human attention


class OrderState(StrEnum):
    """Lifecycle of an individual order inside a block."""

    PENDING = "PENDING"       # entry limit on the order book, not yet filled
    TRIGGERED = "TRIGGERED"   # entry filled; position open with TP/SL alive
    TP_HIT = "TP_HIT"         # closed via take-profit
    SL_HIT = "SL_HIT"         # closed via stop-loss
    CANCELLED = "CANCELLED"   # entry cancelled before triggering
    ERROR = "ERROR"           # placement or close failed


class EventType(StrEnum):
    """Audit-log event types."""

    BLOCK_CREATED = "BLOCK_CREATED"
    ORDERS_PLACED = "ORDERS_PLACED"
    ORDER_TRIGGERED = "ORDER_TRIGGERED"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    BLOCK_WIN = "BLOCK_WIN"
    BLOCK_LOSS = "BLOCK_LOSS"
    BLOCK_INVALID = "BLOCK_INVALID"
    BLOCK_ERROR = "BLOCK_ERROR"
    BLOCK_MANUAL_CLOSE = "BLOCK_MANUAL_CLOSE"
    CANCEL_PRICE_HIT = "CANCEL_PRICE_HIT"
