"""Database engine, session factory, and lifecycle helpers.

Independent from ``src/db/database.py`` so the futures bot can use a
completely separate SQLite file (or PostgreSQL schema) without
risking the crypto bot's data. Same patterns: lazily-initialised
globals, ``init_db`` + ``close_db`` for lifecycle, ``session_scope``
context manager for short-lived transactions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from futures_bot.config import get_settings


class Base(DeclarativeBase):
    """Common declarative base for futures-bot ORM models.

    Distinct from the crypto bot's ``Base`` so the two model
    registries can't accidentally collide when both packages are
    loaded into the same Python process (e.g. during a unified test
    suite).
    """


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    """Create the engine, session factory, and tables.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _engine, _session_factory
    if _engine is not None:
        return

    settings = get_settings()
    url = settings.database_url

    # Make sure SQLite directory exists. Mirrors the crypto bot's
    # behaviour so the futures DB lands next to it under ``data/``.
    if url.startswith("sqlite"):
        path_part = url.split("///", 1)[1]
        if path_part and path_part != ":memory:":
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)

    _engine = create_async_engine(
        url,
        echo=False,
        future=True,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    # Import models so their tables register on ``Base.metadata``
    # before create_all runs.
    from futures_bot.db import models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Futures DB initialised at {url}", url=_redact_url(url))


async def close_db() -> None:
    """Dispose of the engine. Safe to call even if ``init_db`` never ran."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_session() -> AsyncSession:
    """Return a new session bound to the global factory."""
    if _session_factory is None:
        raise RuntimeError("Futures DB not initialised. Call init_db() first.")
    return _session_factory()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session: commit on success, rollback on error."""
    if _session_factory is None:
        raise RuntimeError("Futures DB not initialised. Call init_db() first.")

    session = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def _redact_url(url: str) -> str:
    """Hide credentials from a database URL before logging."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    _creds, host = rest.split("@", 1)
    return f"{scheme}://***@{host}"
