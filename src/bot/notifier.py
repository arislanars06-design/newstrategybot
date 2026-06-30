"""Bridges engine notifications to Telegram messages."""

from __future__ import annotations

import asyncio

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
    # A liquidation that triggers a chain restart is a block-level
    # event the trader wants visibility on, on par with WIN/LOSS.
    NotificationType.RUNG_LIQUIDATED,
})


class TelegramNotifier:
    """Stateless adapter — given a Notification, send the rendered text.

    Always sends to the chat that owns the block (so the trader gets
    every event in their personal DM with the bot). When a notification
    channel is configured *and* the event type is in
    :data:`CHANNEL_EVENT_TYPES`, the same message is also delivered to
    that channel as a high-signal block-status feed.

    The chat send and the channel send run **in parallel** via
    ``asyncio.gather``. Earlier the channel send was awaited only after
    the personal chat send completed, which meant a slow Telegram round
    trip on one side delayed the other — the trader noticed this when
    Telegram was rate-limiting the personal chat and the public channel
    fell tens of seconds behind. Each ``_send`` swallows its own
    ``TelegramAPIError`` internally so an unreachable channel can never
    block delivery to the personal chat (and vice-versa).

    Wired into the engine via ``BlockEngine(on_notification=...)``.
    """

    def __init__(self, bot: Bot, channel_id: int | None = None) -> None:
        self._bot = bot
        self._channel_id = channel_id

    async def __call__(self, notification: Notification) -> None:
        text = render_notification(notification)

        sends: list = [self._send(notification.chat_id, text, scope="chat")]
        if (
            self._channel_id is not None
            and notification.type in CHANNEL_EVENT_TYPES
        ):
            sends.append(self._send(self._channel_id, text, scope="channel"))

        # return_exceptions=True is defence in depth: _send already
        # catches TelegramAPIError, but if a future contributor adds a
        # path that raises, gather() must never propagate and bubble
        # back into the engine's _notify try/except.
        await asyncio.gather(*sends, return_exceptions=True)

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
