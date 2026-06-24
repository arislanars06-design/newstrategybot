"""Futures-bot entry point.

Run with ``python -m futures_bot.main``. Wires up the four pieces:

* Config + logging
* MT5 (or mock) broker adapter
* BlockEngine
* Telegram bot + dispatcher + notifier

Lifecycle is symmetric to the crypto bot's ``src/main.py``: start
everything, await long-running tasks, drain cleanly on signal.
"""

from __future__ import annotations

import asyncio
import signal

from loguru import logger

from futures_bot.adapters.base import BrokerAdapter
from futures_bot.adapters.mock_adapter import MockAdapter
from futures_bot.adapters.mt5_adapter import MT5Adapter
from futures_bot.bot.notifier import TelegramNotifier
from futures_bot.bot.setup import (
    build_bot,
    build_dispatcher,
    install_bot_commands,
)
from futures_bot.config import Settings, get_settings
from futures_bot.core.engine import BlockEngine
from futures_bot.db.database import close_db, init_db
from futures_bot.logging_setup import configure_logging


def _build_adapter(settings: Settings) -> BrokerAdapter:
    """Pick the broker adapter at runtime.

    Setting ``FB_USE_MOCK_ADAPTER=1`` in the .env file (or the process
    environment) swaps in the in-memory mock so the bot can run
    end-to-end without an MT5 container. Useful during the bring-up
    phase before the trader has an Exness demo wired up.

    The flag lives on ``Settings`` so it picks up values from the
    .env file the same way every other ``FB_*`` knob does — reading
    it through ``os.getenv`` would silently miss .env values, which
    is exactly the bug this commit fixes.
    """
    if settings.use_mock_adapter:
        logger.warning(
            "FB_USE_MOCK_ADAPTER set — running with MockAdapter, no real broker!"
        )
        return MockAdapter()
    return MT5Adapter(settings)


async def _runner() -> None:
    settings = get_settings()
    configure_logging(settings)
    logger.info("Futures bot starting up.")

    adapter = _build_adapter(settings)

    bot = build_bot(settings)
    notifier = TelegramNotifier(
        bot=bot,
        channel_id=settings.telegram_notify_channel_id,
    )
    engine = BlockEngine(
        settings=settings,
        adapter=adapter,
        on_notification=notifier,
    )

    await init_db()
    await adapter.connect()
    await install_bot_commands(bot)

    dispatcher = build_dispatcher(
        settings=settings,
        engine=engine,
        adapter=adapter,
    )

    # The dispatcher's polling loop is the long-running task. We
    # arrange for a clean shutdown via Unix signal handlers so the
    # process can be stopped with Ctrl-C or a SIGTERM from systemd
    # without losing in-flight transactions.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows / restricted environments. The bot is intended
            # to run on Linux so this is a best-effort fallback.
            logger.warning("add_signal_handler not available for {sig}", sig=sig)

    async def _stop_when_signalled() -> None:
        await stop.wait()
        logger.info("Shutdown signal received; stopping dispatcher.")
        await dispatcher.stop_polling()

    stopper = asyncio.create_task(_stop_when_signalled())

    try:
        # ``start_polling`` returns when the dispatcher is stopped
        # (either by the signal handler above or by an unhandled
        # internal error). aiogram closes the bot session for us.
        await dispatcher.start_polling(bot)
    finally:
        stopper.cancel()
        await adapter.disconnect()
        await close_db()
        await bot.session.close()
        logger.info("Futures bot shut down cleanly.")


def main() -> None:
    """Synchronous entry point for ``python -m futures_bot.main``."""
    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")


if __name__ == "__main__":
    main()
