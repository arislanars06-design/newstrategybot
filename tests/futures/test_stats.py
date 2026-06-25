"""Tests for :func:`repository.compute_stats` + :func:`formatters.format_stats`.

The stats screen is the only place the trader sees historical
performance, so the aggregation has to be correct on every block
state combination and the formatter has to render zero-block /
all-wins / mixed-outcome cases cleanly.

The aggregation is pure-Python — we test it directly against a
freshly-built in-memory SQLite DB. No engine, no adapter, no FSM:
the goal here is to lock down the contract between blocks → stats
→ Telegram-ready string.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from futures_bot.bot.formatters import format_stats
from futures_bot.config import Settings
from futures_bot.db import repository, session_scope
from futures_bot.db.database import close_db, init_db
from futures_bot.db.enums import BlockSide, BlockStatus
from futures_bot.db.models import Block


def _test_settings() -> Settings:
    return Settings(
        mt5_login=1,
        mt5_password="x",
        mt5_server="x",
        telegram_bot_token="x",
        telegram_notify_chat_id=1,
        database_url="sqlite+aiosqlite:///:memory:",
    )


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory DB per test, mirroring other futures tests."""
    import futures_bot.config as config_mod
    config_mod._settings = _test_settings()
    from futures_bot.db import database as db_mod
    db_mod._engine = None
    db_mod._session_factory = None

    await init_db()
    yield
    await close_db()


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


async def _add_block(
    *,
    status: BlockStatus,
    symbol: str = "XAUUSD",
    net_pnl: float | None = None,
    closed_at: datetime | None = None,
) -> int:
    """Insert a synthetic block. Returns the id so tests can reference it."""
    async with session_scope() as session:
        b = Block(
            symbol=symbol,
            side=BlockSide.BUY,
            status=status,
            zero_price=2660.0,
            hundred_price=2640.0,
            base_risk_usd=5.0,
            cancel_price=2665.0,
            sl_distance=2.55,
            cancel_price_active=False,
            chat_id=1,
            net_pnl=net_pnl,
            closed_at=closed_at,
        )
        session.add(b)
        await session.flush()
        return b.id


# ---------------------------------------------------------------------
# compute_stats
# ---------------------------------------------------------------------

class TestComputeStats:
    """Aggregation against synthetic block rows."""

    @pytest.mark.asyncio
    async def test_empty_db_yields_zero_counts(self, db) -> None:
        async with session_scope() as session:
            stats = await repository.compute_stats(session)
        assert stats.total == 0
        assert stats.active == 0
        assert stats.wins == 0
        assert stats.losses == 0
        assert stats.total_pnl == 0.0
        assert stats.win_rate is None
        assert stats.pnl_last_7d == 0.0

    @pytest.mark.asyncio
    async def test_active_blocks_counted_separately(self, db) -> None:
        await _add_block(status=BlockStatus.ACTIVE)
        await _add_block(status=BlockStatus.CREATED)
        async with session_scope() as session:
            stats = await repository.compute_stats(session)
        # Both pre-terminal statuses count as 'active' in the summary.
        assert stats.active == 2
        assert stats.wins == 0
        assert stats.losses == 0
        # No terminal blocks → no win rate.
        assert stats.win_rate is None

    @pytest.mark.asyncio
    async def test_win_rate_uses_only_decisive_outcomes(self, db) -> None:
        # INVALID / ERROR blocks shouldn't pull win-rate down — they're
        # not "decisive" outcomes for the strategy, just no-trades.
        now = _utcnow()
        await _add_block(status=BlockStatus.WIN, net_pnl=10.0, closed_at=now)
        await _add_block(status=BlockStatus.WIN, net_pnl=10.0, closed_at=now)
        await _add_block(status=BlockStatus.LOSS, net_pnl=-5.0, closed_at=now)
        await _add_block(status=BlockStatus.INVALID, closed_at=now)
        await _add_block(status=BlockStatus.ERROR, closed_at=now)
        async with session_scope() as session:
            stats = await repository.compute_stats(session)
        assert stats.wins == 2
        assert stats.losses == 1
        assert stats.invalid == 1
        assert stats.errored == 1
        # 2 / (2 + 1) = 0.6666...
        assert stats.win_rate is not None
        assert abs(stats.win_rate - 2 / 3) < 1e-4

    @pytest.mark.asyncio
    async def test_total_pnl_sums_terminal_blocks_only(self, db) -> None:
        # Active blocks have null net_pnl and must not contribute.
        await _add_block(status=BlockStatus.ACTIVE, net_pnl=None)
        await _add_block(status=BlockStatus.WIN, net_pnl=20.0, closed_at=_utcnow())
        await _add_block(status=BlockStatus.LOSS, net_pnl=-7.5, closed_at=_utcnow())
        async with session_scope() as session:
            stats = await repository.compute_stats(session)
        assert stats.total_pnl == pytest.approx(12.5)

    @pytest.mark.asyncio
    async def test_pnl_last_7d_filters_by_closed_at(self, db) -> None:
        # The 7-day window matters more than the total because the
        # trader uses it to spot recent runs of red/green.
        now = _utcnow()
        recent = now - timedelta(days=2)
        old = now - timedelta(days=14)
        await _add_block(status=BlockStatus.WIN, net_pnl=5.0, closed_at=recent)
        await _add_block(status=BlockStatus.WIN, net_pnl=100.0, closed_at=old)
        async with session_scope() as session:
            stats = await repository.compute_stats(session, now=now)
        assert stats.total_pnl == pytest.approx(105.0)
        assert stats.pnl_last_7d == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_top_symbol_ranking(self, db) -> None:
        now = _utcnow()
        # XAUUSD: 3 blocks, EURUSD: 1.
        for _ in range(3):
            await _add_block(status=BlockStatus.WIN, net_pnl=2.0, closed_at=now, symbol="XAUUSD")
        await _add_block(status=BlockStatus.WIN, net_pnl=10.0, closed_at=now, symbol="EURUSD")
        async with session_scope() as session:
            stats = await repository.compute_stats(session)
        assert stats.by_symbol_top[0][0] == "XAUUSD"
        assert stats.by_symbol_top[0][1] == 3        # block count
        # EURUSD second by count.
        assert stats.by_symbol_top[1][0] == "EURUSD"
        assert stats.by_symbol_top[1][1] == 1


# ---------------------------------------------------------------------
# format_stats
# ---------------------------------------------------------------------

class TestFormatStats:
    """Output strings — focus on shape, not pixel-perfect content."""

    @pytest.mark.asyncio
    async def test_empty_state_is_a_short_friendly_message(self, db) -> None:
        async with session_scope() as session:
            stats = await repository.compute_stats(session)
        out = format_stats(stats)
        # Empty-state should NOT include zero rows for every status —
        # that would be intimidating on first launch.
        assert "Создать" in out
        assert "0" not in out.split("\n")[0]      # no zero in heading

    @pytest.mark.asyncio
    async def test_populated_render_includes_pnl_and_win_rate(self, db) -> None:
        now = _utcnow()
        await _add_block(status=BlockStatus.WIN, net_pnl=10.0, closed_at=now)
        await _add_block(status=BlockStatus.LOSS, net_pnl=-3.5, closed_at=now)
        async with session_scope() as session:
            stats = await repository.compute_stats(session)
        out = format_stats(stats)
        # Spot-check the most-important numbers are in the output.
        assert "Win rate" in out
        assert "+6.5" in out or "6.5" in out      # 10 - 3.5
        assert "Выигрыши" in out
        assert "Убытки" in out

    @pytest.mark.asyncio
    async def test_clean_state_hides_invalid_and_error_rows(self, db) -> None:
        # When there are no INVALID/ERROR blocks, those rows shouldn't
        # appear — keeps the message compact in the happy path.
        await _add_block(status=BlockStatus.WIN, net_pnl=10.0, closed_at=_utcnow())
        async with session_scope() as session:
            stats = await repository.compute_stats(session)
        out = format_stats(stats)
        assert "INVALID" not in out
        assert "Ошибки" not in out
