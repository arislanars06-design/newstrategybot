"""Enumerations used across the futures-bot database layer.

Mirrors the crypto bot's ``src/db/enums.py`` (same StrEnum-as-string
pattern, same names where they apply) so anyone familiar with the
crypto bot can read this without surprise. Differences are deliberate:

* No ``BLOCK_MODIFIED`` / ``CANCEL_PRICE_HIT`` — the futures bot
  doesn't expose ``/modify`` yet, and cancel-price handling is part
  of the engine, not user input.
* ``OrderState`` lacks a ``TRIGGERED`` step because limit orders on
  MT5 either are filled (open position) or pending; there is no
  intermediate "triggered but not filled" state we need to model.
"""

from __future__ import annotations

from enum import StrEnum


class BlockSide(StrEnum):
    """Direction of every order in a block."""

    BUY = "BUY"     # long ladder: entries descend, TP above, SL below
    SELL = "SELL"   # short ladder: entries ascend, TP below, SL above


class BlockStatus(StrEnum):
    """Lifecycle of a block."""

    CREATED = "CREATED"     # planned + persisted, orders not yet on broker
    ACTIVE = "ACTIVE"       # at least one limit lives on the broker
    WIN = "WIN"             # any rung hit TP
    LOSS = "LOSS"           # final rung's SL fired
    INVALID = "INVALID"     # cancel price hit before any fill
    ERROR = "ERROR"         # unrecoverable: see Event payload


class OrderState(StrEnum):
    """Lifecycle of an individual order inside a block."""

    PENDING = "PENDING"        # limit order live on the broker, not filled
    OPEN = "OPEN"              # filled -> position open with SL/TP attached
    TP_HIT = "TP_HIT"          # closed via take-profit
    SL_HIT = "SL_HIT"          # closed via stop-loss
    CANCELLED = "CANCELLED"    # cancelled before filling
    ERROR = "ERROR"            # placement or close failed


class EventType(StrEnum):
    """Audit-log event types."""

    BLOCK_CREATED = "BLOCK_CREATED"
    ORDERS_PLACED = "ORDERS_PLACED"
    ORDER_FILLED = "ORDER_FILLED"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    BLOCK_WIN = "BLOCK_WIN"
    BLOCK_LOSS = "BLOCK_LOSS"
    BLOCK_INVALID = "BLOCK_INVALID"
    BLOCK_ERROR = "BLOCK_ERROR"
    BLOCK_MANUAL_CLOSE = "BLOCK_MANUAL_CLOSE"
    SPREAD_ALERT = "SPREAD_ALERT"
    SESSION_FILTER_BLOCKED = "SESSION_FILTER_BLOCKED"
