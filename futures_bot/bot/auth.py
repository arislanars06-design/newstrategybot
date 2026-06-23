"""Allow-list authentication middleware for the futures Telegram bot.

Identical pattern to ``src/bot/auth.py`` (the crypto bot's middleware).
Kept as a separate file so the two bots can run with different
allow-lists from the same .env (FB_TELEGRAM_ALLOWED_USER_IDS vs the
crypto bot's TELEGRAM_ALLOWED_USER_IDS).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject


class AllowListMiddleware(BaseMiddleware):
    """Drop updates from any user not in the configured allow-list."""

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
        if isinstance(event, Message):
            await event.answer("Доступ запрещён.")
        return None
