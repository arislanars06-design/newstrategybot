"""Telegram bot: aiogram setup, command handlers, FSM, notifier."""

from src.bot.notifier import TelegramNotifier
from src.bot.setup import build_bot, build_dispatcher

__all__ = [
    "TelegramNotifier",
    "build_bot",
    "build_dispatcher",
]
