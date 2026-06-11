"""Application configuration loaded from environment variables.

All settings are validated at startup using pydantic-settings. Missing or
malformed values cause an immediate, descriptive failure instead of a
runtime crash deep inside the bot.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Values are loaded (in order of priority):
    1. Real environment variables.
    2. A `.env` file in the project root.
    3. Defaults defined here.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Binance ---
    binance_api_key: str = Field(..., min_length=1)
    binance_api_secret: str = Field(..., min_length=1)
    binance_testnet: bool = True

    # --- Telegram ---
    telegram_bot_token: str = Field(..., min_length=1)
    telegram_allowed_user_ids: str = Field(default="")
    telegram_notify_chat_id: int = Field(...)

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./data/newstrategybot.db"

    # --- Logging ---
    log_level: str = "INFO"
    log_file: str = "./logs/bot.log"

    # --- Engine ---
    order_place_delay_ms: int = 80
    reconcile_interval_sec: int = 30

    # ----- Computed helpers -----

    @property
    def allowed_user_ids(self) -> set[int]:
        """Parse comma-separated Telegram user IDs into a set of ints."""
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
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}")
        return upper


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance.

    Using a singleton avoids re-parsing the environment on every access while
    still allowing tests to override values via environment variables before
    the first call.
    """
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
