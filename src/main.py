"""Application entry point.

Wires every component together, starts the Telegram dispatcher and the
exchange/engine background workers, and shuts them down cleanly on
SIGINT / SIGTERM.

Run with:
    python -m src.main
"""

from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

from src.bot.notifier import TelegramNotifier
from src.bot.setup import build_bot, build_dispatcher
from src.config import get_settings
from src.core.engine import BlockEngine
from src.db import close_db, init_db
from src.exchange.client import BinanceClient
from src.logging_setup import configure_logging


async def _amain() -> int:
    settings = get_settings()
    configure_logging(settings)
    logger.info("Starting newstrategybot…")

    if not settings.allowed_user_ids:
        logger.warning(
            "TELEGRAM_ALLOWED_USER_IDS is empty — every Telegram user will be "
            "rejected. Configure at least one user ID in your .env."
        )

    # 1. Database first — every other component may need a session.
    await init_db()

    # 2. Binance REST client + hedge-mode check.
    client = BinanceClient(settings)
    await client.start()
    hedge_ok = await client.ensure_hedge_mode()
    if not hedge_ok:
        logger.error(
            "Could not enable Hedge Mode on Binance Futures. The bot needs "
            "Hedge Mode so BUY and SELL blocks on the same symbol can coexist."
        )
        await client.stop()
        await close_db()
        return 2

    # 3. Telegram bot — Notifier needs the bot instance, the engine needs
    #    the notifier callback, the dispatcher needs the engine and client.
    bot = build_bot(settings)
    notifier = TelegramNotifier(bot, channel_id=settings.telegram_notify_channel_id)
    if settings.telegram_notify_channel_id is not None:
        logger.info(
            "Channel mirroring enabled for block events (channel_id={cid})",
            cid=settings.telegram_notify_channel_id,
        )
    engine = BlockEngine(
        settings=settings,
        client=client,
        on_notification=notifier,
    )
    dispatcher = build_dispatcher(settings=settings, engine=engine, client=client)

    # 4. Start engine (kicks off WebSocket workers, recovers blocks).
    await engine.start()

    # 5. Drive everything until SIGINT/SIGTERM.
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    polling_task = asyncio.create_task(
        dispatcher.start_polling(bot, handle_signals=False),
        name="aiogram-polling",
    )

    logger.info("Bot is running. Send /start in Telegram to begin.")
    try:
        # Either signal received → stop_event set, or polling died on its own.
        wait_stop = asyncio.create_task(stop_event.wait(), name="stop-waiter")
        done, _pending = await asyncio.wait(
            {polling_task, wait_stop}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                logger.error("Task {name} crashed: {err}", name=task.get_name(), err=exc)
    finally:
        await _shutdown(dispatcher, polling_task, engine, client, bot)

    logger.info("Shutdown complete.")
    return 0


async def _shutdown(
    dispatcher,  # type: ignore[no-untyped-def]
    polling_task: asyncio.Task[None],
    engine: BlockEngine,
    client: BinanceClient,
    bot,  # type: ignore[no-untyped-def]
) -> None:
    logger.info("Shutting down…")

    # Stop polling first so no new updates arrive.
    try:
        await dispatcher.stop_polling()
    except Exception as exc:  # noqa: BLE001
        logger.warning("dispatcher.stop_polling raised: {err}", err=exc)

    if not polling_task.done():
        polling_task.cancel()
        try:
            await polling_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # Stop background streams (engine owns both user-data and mark-price).
    try:
        await engine.stop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("engine.stop raised: {err}", err=exc)

    # Close exchange connection and DB.
    try:
        await client.stop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("client.stop raised: {err}", err=exc)

    try:
        await bot.session.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("bot.session.close raised: {err}", err=exc)

    try:
        await close_db()
    except Exception as exc:  # noqa: BLE001
        logger.warning("close_db raised: {err}", err=exc)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Translate SIGINT/SIGTERM into ``stop_event.set()``.

    Falls back to default handling on platforms where add_signal_handler
    is unavailable (e.g. Windows event loop).
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover — Windows
            logger.debug("add_signal_handler not supported for {s}", s=sig)


def run() -> None:
    """Synchronous entry point for the ``newstrategybot`` console script."""
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    run()
