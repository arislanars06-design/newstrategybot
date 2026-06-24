"""Telegram-bot layer for the futures bot.

Mirrors the crypto bot's ``src/bot`` package: setup factories,
allow-list middleware, command/FSM handlers, Russian UI formatters,
and a notifier that bridges :class:`Notification` to ``send_message``.
"""

from futures_bot.bot.setup import build_bot, build_dispatcher, install_bot_commands

__all__ = ["build_bot", "build_dispatcher", "install_bot_commands"]
