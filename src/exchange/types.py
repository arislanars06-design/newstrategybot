"""Typed value objects exchanged between the connector and the engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(slots=True)
class SymbolFilters:
    """Cached precision rules for a Futures symbol.

    Used to round user-supplied prices and quantities to values the
    exchange will accept.
    """

    symbol: str
    tick_size: Decimal       # PRICE_FILTER.tickSize
    step_size: Decimal       # LOT_SIZE.stepSize
    min_qty: Decimal         # LOT_SIZE.minQty
    min_notional: Decimal    # MIN_NOTIONAL.notional (or 0 if not set)
    price_precision: int     # decimals for price formatting
    quantity_precision: int  # decimals for qty formatting


@dataclass(slots=True)
class OrderUpdate:
    """A single ORDER_TRADE_UPDATE event from the user-data WebSocket.

    The block engine consumes these to drive its state machine.
    """

    symbol: str
    client_order_id: str
    order_id: str
    side: str            # "BUY" / "SELL"
    position_side: str   # "LONG" / "SHORT" / "BOTH"
    order_type: str      # "LIMIT", "STOP_MARKET", ...
    status: str          # "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", ...
    avg_fill_price: float
    executed_qty: float
    realized_pnl: float
    event_time_ms: int

    # Original payload kept around for debug logging / audit trail.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status == "FILLED"

    @property
    def is_canceled(self) -> bool:
        return self.status in {"CANCELED", "EXPIRED", "REJECTED"}
