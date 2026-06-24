"""Bot and Dispatcher factories.

Mirrors ``src/bot/setup.py``: factories are kept here so tests can
build a dispatcher with stub collaborators without spinning up the
whole application.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from loguru import logger

from futures_bot.adapters.base import BrokerAdapter
from futures_bot.bot.auth import AllowListMiddleware
from futures_bot.bot.handlers import router
from futures_bot.config import Settings
from futures_bot.core.engine import BlockEngine


def build_bot(settings: Settings) -> Bot:
    """Return an aiogram Bot with HTML default parsing."""
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


# Only the two entry-point commands are advertised in Telegram's
# slash-menu autocomplete. /newblock / /list / /block / /cancel /
# /symbols / /balance still work — they just aren't pushed at the
# trader on every chat.
ADVERTISED_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Открыть главное меню"),
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="newblock", description="Создать блок"),
    BotCommand(command="list", description="Активные блоки"),
    BotCommand(command="symbols", description="Список доступных символов"),
    BotCommand(command="balance", description="Баланс счёта"),
]


async def install_bot_commands(bot: Bot) -> None:
    """Register the small command list shown in Telegram autocomplete.

    Best-effort: failures are logged but never block startup.
    """
    try:
        await bot.set_my_commands(ADVERTISED_COMMANDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_my_commands failed: {err}", err=exc)


def build_dispatcher(
    *,
    settings: Settings,
    engine: BlockEngine,
    adapter: BrokerAdapter,
) -> Dispatcher:
    """Build the dispatcher and inject runtime collaborators.

    The engine and adapter become available to every handler through
    ``Dispatcher.workflow_data`` — aiogram resolves them by argument
    name at call time, which keeps each handler signature self-
    documenting.
    """
    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)

    dispatcher["engine"] = engine
    dispatcher["adapter"] = adapter

    middleware = AllowListMiddleware(settings.allowed_user_ids)
    dispatcher.message.middleware(middleware)
    dispatcher.callback_query.middleware(middleware)

    dispatcher.include_router(router)
    return dispatcher
