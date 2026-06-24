"""Bridge from engine notifications to Telegram delivery.

Same shape as ``src/bot/notifier.py``: a stateless callable, wired
into the engine via ``BlockEngine(on_notification=...)``. Sends to
the chat that owns the block, and (for high-signal events only) also
to the configured notification channel.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from loguru import logger

from futures_bot.bot.formatters import render_notification
from futures_bot.core.notifications import Notification, NotificationType


# Events the trader wants on the public/private signal channel. Same
# choice as the crypto bot — block-level outcomes only, not every
# rung fill. Per-rung fills would flood the channel.
CHANNEL_EVENT_TYPES: frozenset[NotificationType] = frozenset({
    NotificationType.BLOCK_CREATED,
    NotificationType.BLOCK_WIN,
    NotificationType.BLOCK_LOSS,
    NotificationType.BLOCK_INVALID,
    NotificationType.BLOCK_ERROR,
})


class TelegramNotifier:
    """Adapter between :class:`Notification` and ``Bot.send_message``."""

    def __init__(self, bot: Bot, channel_id: int | None = None) -> None:
        self._bot = bot
        self._channel_id = channel_id

    async def __call__(self, notification: Notification) -> None:
        text = render_notification(notification)

        # Personal chat is always tried. Channel is tried in parallel
        # only when the notification type is in the allow-list; that
        # way a slow channel send never delays the trader's own DM.
        tasks = [self._send(notification.chat_id, text, scope="chat")]
        if (
            self._channel_id is not None
            and notification.type in CHANNEL_EVENT_TYPES
        ):
            tasks.append(self._send(self._channel_id, text, scope="channel"))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _send(self, chat_id: int, text: str, *, scope: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
            )
        except TelegramAPIError as exc:
            # Warn — never raise. The engine treats notification
            # delivery as best-effort.
            logger.warning(
                "Telegram send failed (scope={scope}, chat={cid}): {err}",
                scope=scope,
                cid=chat_id,
                err=exc,
            )
