"""Allow-list authentication middleware for the Telegram bot."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject


class AllowListMiddleware(BaseMiddleware):
    """Drop updates from any user not in the configured allow-list.

    Without this middleware anyone who learns the bot username could
    issue commands that would place real orders.
    """

    def __init__(self, allowed_user_ids: set[int]) -> None:
        self._allowed = set(allowed_user_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id in self._allowed:
            return await handler(event, data)
        # Soft-reject silently for messages, ignore other update types.
        if isinstance(event, Message):
            await event.answer("Access denied.")
        return None
