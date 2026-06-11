"""Bot and Dispatcher factories.

Kept separate from ``handlers.py`` so tests can build a dispatcher with
fakes injected, without spinning up the whole application.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from src.bot.auth import AllowListMiddleware
from src.bot.handlers import router
from src.config import Settings
from src.core.engine import BlockEngine
from src.exchange.client import BinanceClient


def build_bot(settings: Settings) -> Bot:
    """Return an aiogram Bot with sensible defaults (HTML parsing)."""
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher(
    *,
    settings: Settings,
    engine: BlockEngine,
    client: BinanceClient,
) -> Dispatcher:
    """Build the dispatcher and inject runtime collaborators."""
    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)

    # Make engine + client visible to every handler via DI.
    dispatcher["engine"] = engine
    dispatcher["client"] = client

    # Auth: allow-list of permitted Telegram user IDs.
    middleware = AllowListMiddleware(settings.allowed_user_ids)
    dispatcher.message.middleware(middleware)
    dispatcher.callback_query.middleware(middleware)

    dispatcher.include_router(router)
    return dispatcher
