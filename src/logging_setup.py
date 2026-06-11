"""Centralised logging configuration using loguru.

Loguru is configured once at application start. Every other module should
just `from loguru import logger` and use it directly; no per-module setup
is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from src.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure loguru sinks based on application settings."""
    logger.remove()

    # Console sink — coloured, human-friendly.
    # NOTE: enqueue=False — the bot is single-process asyncio, so we don't
    # need the multiprocessing queue. enqueue=True breaks when log records
    # carry non-picklable objects (e.g. aiohttp CIMultiDictProxy inside
    # BinanceAPIException), so we avoid it.
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
            "| <level>{level: <8}</level> "
            "| <cyan>{name}:{function}:{line}</cyan> "
            "- <level>{message}</level>"
        ),
        backtrace=True,
        diagnose=False,  # do not leak variable values into logs
        enqueue=False,
    )

    # File sink — rotating, structured for grep / journald.
    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_path,
        level=settings.log_level,
        rotation="20 MB",
        retention="14 days",
        compression="zip",
        encoding="utf-8",
        enqueue=False,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} "
            "| {level: <8} "
            "| {name}:{function}:{line} "
            "- {message}"
        ),
    )

    logger.info(
        "Logging initialised (level={level}, file={file})",
        level=settings.log_level,
        file=str(log_path),
    )
