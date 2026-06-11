"""Bridges engine notifications to Telegram messages."""

from __future__ import annotations

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from loguru import logger

from src.bot.formatters import render_notification
from src.core.notifications import Notification, NotificationType


# Events that should also be mirrored to the public/private notification
# channel (when one is configured). The user wants a high-signal feed —
# only block-level outcomes and creation, not per-order play-by-play.
CHANNEL_EVENT_TYPES: frozenset[NotificationType] = frozenset({
    NotificationType.BLOCK_CREATED,
    NotificationType.BLOCK_WIN,
    NotificationType.BLOCK_LOSS,
    NotificationType.BLOCK_INVALID,
    NotificationType.BLOCK_ERROR,
})


class TelegramNotifier:
    """Stateless adapter — given a Notification, send the rendered text.

    Always sends to the chat that owns the block (so the trader gets
    every event in their personal DM with the bot). When a notification
    channel is configured *and* the event type is in
    :data:`CHANNEL_EVENT_TYPES`, the same message is mirrored to that
    channel as a high-signal block-status feed.

    Wired into the engine via ``BlockEngine(on_notification=...)``.
    """

    def __init__(self, bot: Bot, channel_id: int | None = None) -> None:
        self._bot = bot
        self._channel_id = channel_id

    async def __call__(self, notification: Notification) -> None:
        text = render_notification(notification)

        # 1. Personal chat — always.
        await self._send(notification.chat_id, text, scope="chat")

        # 2. Optional channel mirror.
        if (
            self._channel_id is not None
            and notification.type in CHANNEL_EVENT_TYPES
        ):
            await self._send(self._channel_id, text, scope="channel")

    async def _send(self, chat_id: int, text: str, *, scope: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
            )
        except TelegramAPIError as exc:
            # Channel send failures are warning-level, not errors — the
            # personal chat is the primary delivery channel and a
            # mis-configured public channel must never block trading.
            logger.warning(
                "Telegram send failed (scope={scope}, chat={cid}): {err}",
                scope=scope,
                cid=chat_id,
                err=exc,
            )
