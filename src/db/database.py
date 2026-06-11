"""Database engine, session factory, and lifecycle helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.config import get_settings


class Base(DeclarativeBase):
    """Common declarative base for all ORM models."""


# Lazily initialised globals. Created in ``init_db`` and torn down in
# ``close_db`` so tests can swap them via dependency injection if needed.
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    """Create the SQLAlchemy engine, session factory, and tables.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _engine, _session_factory
    if _engine is not None:
        return

    settings = get_settings()
    url = settings.database_url

    # Make sure SQLite file directory exists before SQLAlchemy tries to open it.
    if url.startswith("sqlite"):
        # URL format: sqlite+aiosqlite:///./data/newstrategybot.db
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

    # Import models here so SQLAlchemy registers them on Base.metadata
    # before create_all is called. Avoids a chicken-and-egg import cycle.
    from src.db import models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_lightweight_migrations(conn)

    logger.info("Database initialised at {url}", url=_redact_url(url))


async def _run_lightweight_migrations(conn: AsyncConnection) -> None:
    """Idempotent ALTER TABLE patches for SQLite databases created before
    a column was added. Safe to call on every startup.

    For PostgreSQL we expect Alembic to be used in real deployments, so
    this helper is a no-op there.
    """
    dialect = conn.dialect.name
    if dialect != "sqlite":
        return

    # blocks.is_managed — added when /track was introduced.
    result = await conn.execute(text("PRAGMA table_info(blocks)"))
    cols = {row[1] for row in result.fetchall()}
    if "is_managed" not in cols:
        await conn.execute(
            text("ALTER TABLE blocks ADD COLUMN is_managed BOOLEAN NOT NULL DEFAULT 1")
        )
        logger.info("Migration: added blocks.is_managed column")


async def close_db() -> None:
    """Dispose of the engine. Safe to call even if ``init_db`` never ran."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_session() -> AsyncSession:
    """Return a new session bound to the global factory.

    The caller owns the session lifecycle. Prefer ``session_scope`` for
    short-lived transactional blocks.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _session_factory()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a transactional session scope.

    Commits on clean exit, rolls back on any exception, always closes.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")

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
    creds, host = rest.split("@", 1)
    return f"{scheme}://***@{host}"
