"""Futures bot configuration.

Independent from ``src/config.py`` so the crypto bot keeps working
without modification. Variables live under their own prefix (``FB_``)
so they can coexist with the crypto bot's variables in the same .env
file without collision.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Strongly-typed configuration for the futures bot."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="FB_",   # FB_ = "futures bot"
        case_sensitive=False,
        extra="ignore",
    )

    # --- MT5 / Broker ---
    # mt5linux RPC server (running inside the Wine container) listens on
    # this host:port. From outside Docker it is 127.0.0.1:18001; from
    # inside the compose network it is "mt5:8001".
    mt5_host: str = "127.0.0.1"
    mt5_port: int = 8001

    # Trading account credentials and server name shown by the broker.
    # Server names for Exness look like "Exness-MT5Trial7" (demo) or
    # "Exness-MT5Real8" (real). Bot logs in to MT5 once, then talks to
    # it over RPC for the lifetime of the process.
    mt5_login: int = Field(..., gt=0)
    mt5_password: str = Field(..., min_length=1)
    mt5_server: str = Field(..., min_length=1)

    # --- Telegram ---
    # Separate bot + separate channel from the crypto bot so neither
    # the chat history nor the signal channel get mixed.
    telegram_bot_token: str = Field(..., min_length=1)
    telegram_allowed_user_ids: str = Field(default="")
    telegram_notify_chat_id: int = Field(...)
    telegram_notify_channel_id: int | None = None

    # --- Database ---
    # Separate SQLite file so the crypto DB is never touched.
    database_url: str = "sqlite+aiosqlite:///./data/futures_bot.db"

    # --- Logging ---
    log_level: str = "INFO"
    log_file: str = "./logs/futures_bot.log"

    # --- Strategy defaults ---
    # All editable from .env so the trader can iterate without
    # touching code.

    # Per-rung lot rounding when the calculated lot does not land
    # exactly on the broker's volume_step. "up" preserves the planned
    # risk:reward; "down" stays safer; "nearest" splits the difference.
    lot_rounding: str = "up"

    # Multiplier applied to the typical spread when adjusting each
    # rung's SL deeper, so the chain stays unbroken when prices move
    # at MT5 quote-quote granularity. 1.5 covers normal-session jitter
    # without burning more than a fraction of a rung's risk budget.
    sl_spread_safety: float = 1.5

    # The "3 ×" in TP_gross = TP_MULTIPLIER × cumulative_risk.
    # Changing this re-shapes the whole pay-off table; keep at 3 unless
    # backtests say otherwise.
    tp_multiplier: float = 3.0

    # 1.5 ^ rung-index growth in dollar risk per rung.
    risk_progression: float = 1.5

    # Refuse to open a new block if cumulative open-block risk would
    # exceed this fraction of equity. 0.05 = 5%.
    max_daily_loss_pct: float = 5.0

    # --- Engine ---
    # Polling interval (ms) for MT5 ticks when WebSocket-style streams
    # are not available. Keep low enough to catch fast fills, high
    # enough not to hammer the RPC.
    tick_poll_interval_ms: int = 500

    # --- Computed helpers ---

    @property
    def allowed_user_ids(self) -> set[int]:
        if not self.telegram_allowed_user_ids.strip():
            return set()
        return {
            int(part.strip())
            for part in self.telegram_allowed_user_ids.split(",")
            if part.strip()
        }

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"FB_LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}")
        return upper

    @field_validator("lot_rounding")
    @classmethod
    def _validate_lot_rounding(cls, value: str) -> str:
        allowed = {"up", "down", "nearest"}
        lower = value.lower()
        if lower not in allowed:
            raise ValueError(
                f"FB_LOT_ROUNDING must be one of {sorted(allowed)}, got {value!r}"
            )
        return lower


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached :class:`Settings` instance."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
