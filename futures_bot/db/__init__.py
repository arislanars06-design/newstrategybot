"""Database layer for the futures bot.

Re-exports the public surface so call-sites can write
``from futures_bot.db import Block, session_scope`` without knowing
which module owns which symbol.
"""

from futures_bot.db.database import (
    Base,
    close_db,
    get_session,
    init_db,
    session_scope,
)
from futures_bot.db.enums import (
    BlockSide,
    BlockStatus,
    EventType,
    OrderState,
)
from futures_bot.db.models import Block, Event, Order

__all__ = [
    "Base",
    "Block",
    "BlockSide",
    "BlockStatus",
    "Event",
    "EventType",
    "Order",
    "OrderState",
    "close_db",
    "get_session",
    "init_db",
    "session_scope",
]
