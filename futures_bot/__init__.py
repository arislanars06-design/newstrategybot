"""Futures trading bot package.

Lives next to the existing ``src`` (crypto bot) package and is completely
independent: its own Telegram bot, its own database file, its own config.
Run with ``python -m futures_bot.main``.

The crypto bot in ``src`` is untouched by anything in this package.
"""

__version__ = "0.1.0"
