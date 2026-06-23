"""Broker adapters.

Each adapter implements the same interface (``BrokerAdapter``) so the
engine can swap between MT5 (real / demo) and a mock implementation
without changing strategy code. The MT5 adapter is the production
target; the mock adapter is used by unit tests and by the trader
during early development before they have an Exness demo account.
"""

from futures_bot.adapters.base import (
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    Position,
    SymbolInfo,
    Tick,
)

__all__ = [
    "BrokerAdapter",
    "OrderRequest",
    "OrderResult",
    "Position",
    "SymbolInfo",
    "Tick",
]
