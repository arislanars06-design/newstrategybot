"""Centralised logging configuration for the futures bot."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from futures_bot.config import Settings


def configure_logging(settings: Settings) -> None:
    """Set up loguru sinks (console + rotating file)."""
    logger.remove()

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
        diagnose=False,
        enqueue=False,
    )

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
        "Futures bot logging initialised (level={level}, file={file})",
        level=settings.log_level,
        file=str(log_path),
    )
