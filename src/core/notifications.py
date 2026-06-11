"""Notifications emitted by the engine for outside listeners (e.g. Telegram)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NotificationType(StrEnum):
    BLOCK_CREATED = "BLOCK_CREATED"
    ORDER_TRIGGERED = "ORDER_TRIGGERED"
    SL_HIT = "SL_HIT"
    BLOCK_WIN = "BLOCK_WIN"
    BLOCK_LOSS = "BLOCK_LOSS"
    BLOCK_INVALID = "BLOCK_INVALID"
    BLOCK_ERROR = "BLOCK_ERROR"
    BLOCK_MANUAL_CLOSE = "BLOCK_MANUAL_CLOSE"


@dataclass(slots=True)
class Notification:
    """A structured event meant for the outside world (Telegram, logs, ...)."""

    type: NotificationType
    block_id: int
    chat_id: int
    payload: dict[str, Any] = field(default_factory=dict)


# Simple callback contract used by the engine.
NotificationHandler = Callable[[Notification], Awaitable[None]]
