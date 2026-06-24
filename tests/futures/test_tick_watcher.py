"""Lifecycle tests for :class:`TickWatcher`.

These tests don't try to validate the full polling-driven engine
behaviour — :file:`test_engine_e2e.py` already exercises ``on_tick``
exhaustively. Instead they guarantee the *plumbing*:

* The watcher fetches ticks for every symbol with an active block.
* The watcher stops calling ``on_tick`` for a symbol once the block
  becomes terminal (e.g. INVALID).
* A failing ``get_tick`` for one symbol doesn't poison the loop for
  others.
* ``stop()`` actually stops the loop, doesn't hang.

The MockAdapter is rich enough to drive all four scenarios — we just
need a thin stub engine that records the ticks it received.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from futures_bot.adapters.base import Tick
from futures_bot.adapters.mock_adapter import MockAdapter
from futures_bot.config import Settings
from futures_bot.core.order_watcher import TickWatcher
from futures_bot.db.database import close_db, init_db


# ---------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------

def _test_settings(*, tick_poll_ms: int = 100) -> Settings:
    """Settings with a small enough polling interval to be test-fast.

    The watcher floor-clamps to 100ms internally, so we pass the
    floor itself — anything smaller would be silently bumped.
    """
    return Settings(
        mt5_login=1,
        mt5_password="x",
        mt5_server="x",
        telegram_bot_token="x",
        telegram_notify_chat_id=1,
        database_url="sqlite+aiosqlite:///:memory:",
        tick_poll_interval_ms=tick_poll_ms,
    )


class _RecordingEngine:
    """Stand-in for :class:`BlockEngine` that just logs ticks seen.

    A real BlockEngine would mutate the DB and call the broker; the
    watcher only cares that ``on_tick`` exists and is awaitable.
    """

    def __init__(self) -> None:
        self.seen: list[Tick] = []

    async def on_tick(self, tick: Tick) -> None:
        self.seen.append(tick)


class _FailingAdapter:
    """Adapter whose ``get_tick`` raises — to test isolation.

    We intentionally inherit no base class; the watcher only uses
    duck-typed methods so this is enough.
    """

    def __init__(self, fail_for: set[str], delegate: MockAdapter) -> None:
        self._fail_for = fail_for
        self._delegate = delegate
        self.fail_calls = 0

    async def get_tick(self, symbol: str) -> Tick:
        if symbol in self._fail_for:
            self.fail_calls += 1
            raise RuntimeError(f"simulated failure for {symbol}")
        return await self._delegate.get_tick(symbol)


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory DB per test, mirroring test_engine_e2e.py."""
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


async def _insert_active_block(symbol: str) -> int:
    """Insert a synthetic ACTIVE block so the watcher picks the symbol up.

    We don't go through engine.create_block here because that would
    require a connected adapter and would over-couple the test to
    the strategy plan. A bare INSERT is enough — the watcher only
    reads ``symbol`` off active rows.
    """
    from futures_bot.db import session_scope
    from futures_bot.db.enums import BlockSide, BlockStatus
    from futures_bot.db.models import Block

    async with session_scope() as session:
        block = Block(
            symbol=symbol,
            side=BlockSide.BUY,
            status=BlockStatus.ACTIVE,
            zero_price=1.0,
            hundred_price=2.0,
            base_risk_usd=1.0,
            cancel_price=3.0,
            sl_distance=1.0,
            cancel_price_active=True,
            chat_id=1,
        )
        session.add(block)
        await session.flush()
        return block.id


async def _mark_block_invalid(block_id: int) -> None:
    """Move a block to a terminal state so the watcher drops it."""
    from futures_bot.db import session_scope
    from futures_bot.db.enums import BlockStatus
    from futures_bot.db import repository

    async with session_scope() as session:
        block = await repository.get_block(session, block_id)
        assert block is not None
        await repository.set_block_status(
            session,
            block,
            BlockStatus.INVALID,
            closed_at=_utcnow(),
            net_pnl=0.0,
        )


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    """Poll-wait for ``predicate()`` to become truthy. Async-friendly.

    Lets us assert on watcher side-effects without hard-coding a
    sleep that's long enough to be flaky on slow CI.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"predicate not satisfied within {timeout}s")


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

class TestLifecycle:
    """Start/stop semantics — no hangs, no leaks."""

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self, db) -> None:
        broker = MockAdapter()
        await broker.connect()
        watcher = TickWatcher(
            settings=_test_settings(),
            adapter=broker,
            engine=_RecordingEngine(),
        )
        # Should return cleanly even though start() was never called.
        await watcher.stop()

    @pytest.mark.asyncio
    async def test_start_twice_is_idempotent(self, db) -> None:
        broker = MockAdapter()
        await broker.connect()
        watcher = TickWatcher(
            settings=_test_settings(),
            adapter=broker,
            engine=_RecordingEngine(),
        )
        watcher.start()
        first_task = watcher._task
        watcher.start()
        assert watcher._task is first_task
        await watcher.stop()


class TestSymbolDispatch:
    """The watcher polls exactly the symbols with active blocks."""

    @pytest.mark.asyncio
    async def test_polls_each_active_symbol(self, db) -> None:
        broker = MockAdapter()
        await broker.connect()
        # Seed ticks so MockAdapter.get_tick has something to return.
        await broker.feed_tick(
            Tick(symbol="XAUUSD", bid=2640.0, ask=2640.2, time=_utcnow())
        )
        await broker.feed_tick(
            Tick(symbol="EURUSD", bid=1.0800, ask=1.08008, time=_utcnow())
        )

        await _insert_active_block("XAUUSD")
        await _insert_active_block("EURUSD")

        engine = _RecordingEngine()
        watcher = TickWatcher(
            settings=_test_settings(),
            adapter=broker,
            engine=engine,
        )
        watcher.start()

        # Wait until we've seen at least one tick per symbol.
        await _wait_for(
            lambda: {t.symbol for t in engine.seen} >= {"XAUUSD", "EURUSD"}
        )
        await watcher.stop()

        seen_symbols = {t.symbol for t in engine.seen}
        assert seen_symbols == {"XAUUSD", "EURUSD"}

    @pytest.mark.asyncio
    async def test_dropped_after_block_goes_terminal(self, db) -> None:
        broker = MockAdapter()
        await broker.connect()
        await broker.feed_tick(
            Tick(symbol="XAUUSD", bid=2640.0, ask=2640.2, time=_utcnow())
        )
        block_id = await _insert_active_block("XAUUSD")

        engine = _RecordingEngine()
        watcher = TickWatcher(
            settings=_test_settings(),
            adapter=broker,
            engine=engine,
        )
        watcher.start()

        # Wait for at least one tick to be delivered.
        await _wait_for(lambda: len(engine.seen) > 0)

        # Mark the block terminal and freeze the count we've seen.
        await _mark_block_invalid(block_id)
        before = len(engine.seen)

        # Give the watcher a few iterations (well above the 100ms
        # polling cadence) to notice and stop polling XAUUSD.
        await asyncio.sleep(0.4)

        # No further ticks should have been delivered, because the
        # only active block was terminated.
        after = len(engine.seen)
        # Allow at most one straggler from a poll in flight when we
        # marked the block — anything more would mean the watcher
        # isn't re-querying active blocks per iteration.
        assert after - before <= 1
        await watcher.stop()


class TestErrorIsolation:
    """A failing get_tick must not starve the other symbols."""

    @pytest.mark.asyncio
    async def test_failing_symbol_isolated_from_healthy_one(
        self, db
    ) -> None:
        delegate = MockAdapter()
        await delegate.connect()
        await delegate.feed_tick(
            Tick(symbol="EURUSD", bid=1.0800, ask=1.08008, time=_utcnow())
        )
        # GBPUSD will be requested but raise — XAUUSD is missing from
        # the mock entirely, so the same error path catches both.
        await _insert_active_block("EURUSD")
        await _insert_active_block("GBPUSD")

        adapter = _FailingAdapter(fail_for={"GBPUSD"}, delegate=delegate)
        engine = _RecordingEngine()
        watcher = TickWatcher(
            settings=_test_settings(),
            adapter=adapter,  # type: ignore[arg-type]
            engine=engine,
        )
        watcher.start()

        # We need: (a) the healthy symbol's tick still gets through,
        # (b) the failing symbol does retry on each iteration.
        await _wait_for(
            lambda: any(t.symbol == "EURUSD" for t in engine.seen)
            and adapter.fail_calls >= 2
        )
        await watcher.stop()

        symbols = {t.symbol for t in engine.seen}
        assert "EURUSD" in symbols
        assert "GBPUSD" not in symbols  # never delivered a tick
        # Engine errors don't crash the loop either — we already
        # verified EURUSD kept being polled past the first GBPUSD
        # failure.
        assert adapter.fail_calls >= 2
