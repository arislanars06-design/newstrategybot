"""Bridges engine notifications to Telegram messages."""

from __future__ import annotations

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from loguru import logger

from src.bot.formatters import render_notification
from src.core.notifications import Notification


class TelegramNotifier:
    """Stateless adapter — given a Notification, send the rendered text.

    Wired into the engine via ``BlockEngine(on_notification=...)``.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def __call__(self, notification: Notification) -> None:
        text = render_notification(notification)
        try:
            await self._bot.send_message(
                chat_id=notification.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Telegram send failed (chat={cid}): {err}",
                cid=notification.chat_id,
                err=exc,
            )
