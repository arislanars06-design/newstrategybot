"""End-to-end engine tests using the in-memory mock broker.

We drive the engine through full block lifecycles without touching
MT5 or Telegram. The mock adapter (``MockAdapter``) sits in for the
broker; a stub notification callback collects events into a list so
tests can assert on the sequence.

These tests are the canary for the whole strategy stack: if the
strategy math drifts, or the engine forgets a state transition, or
the chain rule changes, one of these tests starts failing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from futures_bot.adapters.base import Tick
from futures_bot.adapters.mock_adapter import MockAdapter
from futures_bot.config import Settings
from futures_bot.core.engine import BlockEngine
from futures_bot.core.notifications import Notification, NotificationType
from futures_bot.db.database import close_db, init_db
from futures_bot.db.enums import BlockStatus, OrderState
from futures_bot.db import session_scope, repository
from futures_bot.db.enums import BlockSide
from futures_bot.strategy.plan import build_plan
from futures_bot.strategy.risk import SymbolSpec


# ---------------------------------------------------------------------
# Test settings — anchored away from any real .env or credentials.
# ---------------------------------------------------------------------

def _test_settings() -> Settings:
    """Build a Settings instance bypassing .env loading.

    Required Pydantic fields are filled with stub values; the engine
    only reads the strategy knobs (sl_spread_safety, tp_multiplier,
    etc.) and a few feature flags.
    """
    return Settings(
        mt5_login=1,
        mt5_password="x",
        mt5_server="x",
        telegram_bot_token="x",
        telegram_notify_chat_id=1,
        database_url="sqlite+aiosqlite:///:memory:",
    )


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    """Reset the in-memory DB before each test.

    We import-and-reset the module-level engine/session factory so a
    test doesn't leak state to the next one. The engine code uses the
    same globals so this is what guarantees isolation.
    """
    # Patch the database URL to in-memory so init_db creates a fresh
    # schema for every test.
    import futures_bot.config as config_mod

    # Force a fresh Settings instance.
    config_mod._settings = _test_settings()
    # And reset the DB module-level globals.
    from futures_bot.db import database as db_mod
    db_mod._engine = None
    db_mod._session_factory = None

    await init_db()
    yield
    await close_db()


@pytest_asyncio.fixture
async def broker():
    adapter = MockAdapter(starting_balance=1_000.0)
    await adapter.connect()
    yield adapter
    await adapter.disconnect()


@pytest_asyncio.fixture
async def engine_and_events(db, broker):
    """Engine plus a list capturing every notification it emits."""
    captured: list[Notification] = []

    async def collector(notification: Notification) -> None:
        captured.append(notification)

    engine = BlockEngine(
        settings=_test_settings(),
        adapter=broker,
        on_notification=collector,
    )
    return engine, captured, broker


@pytest.fixture
def xau_spec() -> SymbolSpec:
    return SymbolSpec(
        symbol="XAUUSD",
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
    )


def _make_xauusd_plan(xau_spec: SymbolSpec):
    return build_plan(
        symbol="XAUUSD",
        side=BlockSide.BUY,
        zero_price=2650.0,
        hundred_price=2620.0,
        base_risk_usd=5.0,
        cancel_price=2655.0,
        symbol_spec=xau_spec,
        lot_rounding="up",
    )


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

class TestMockAdapterAutoSeed:
    """The mock auto-seeds a sensible tick on first ``get_tick`` so
    the Telegram preview doesn't blow up on a fresh process."""

    @pytest.mark.asyncio
    async def test_known_symbol_seeds_default_tick(
        self, engine_and_events, xau_spec
    ) -> None:
        # Fresh MockAdapter (via the fixture chain) has not had any
        # tick fed for EURUSD, yet get_tick must still succeed.
        _engine, _events, broker = engine_and_events

        tick = await broker.get_tick("EURUSD")
        assert tick.symbol == "EURUSD"
        assert tick.bid < tick.ask
        # Spread is roughly the symbol's typical 0.8 pip.
        assert 0.00007 < tick.spread < 0.00009

    @pytest.mark.asyncio
    async def test_unknown_symbol_still_raises(
        self, engine_and_events, xau_spec
    ) -> None:
        _engine, _events, broker = engine_and_events

        with pytest.raises(ValueError) as exc:
            await broker.get_tick("WTFCOIN")
        # Message should hint at the fix so the operator isn't stuck.
        assert "feed_tick" in str(exc.value) or "_DEFAULT" in str(exc.value)


class TestCreateBlock:
    """``engine.create_block`` places the six limits and notifies."""

    @pytest.mark.asyncio
    async def test_creates_active_block_with_six_pending_orders(
        self, engine_and_events, xau_spec
    ) -> None:
        engine, events, broker = engine_and_events

        # Seed an initial tick so the mock has a price.
        await broker.feed_tick(
            Tick(symbol="XAUUSD", bid=2640.30, ask=2640.50, time=_utcnow())
        )

        plan = _make_xauusd_plan(xau_spec)
        block = await engine.create_block(plan, chat_id=42)

        assert block.status == BlockStatus.ACTIVE
        assert len(block.orders) == 6
        # All six orders pending on the broker.
        pendings = await broker.list_pending_orders(symbol="XAUUSD")
        assert len(pendings) == 6
        # One BLOCK_CREATED notification went out.
        types = [n.type for n in events]
        assert types == [NotificationType.BLOCK_CREATED]
        assert events[0].chat_id == 42


class TestCancelPriceInvalid:
    """Cancel-price hit before any fill → block becomes INVALID."""

    @pytest.mark.asyncio
    async def test_cancel_price_triggers_invalid_status(
        self, engine_and_events, xau_spec
    ) -> None:
        engine, events, broker = engine_and_events

        await broker.feed_tick(
            Tick(symbol="XAUUSD", bid=2640.30, ask=2640.50, time=_utcnow())
        )
        plan = _make_xauusd_plan(xau_spec)
        block = await engine.create_block(plan, chat_id=42)

        # Push the ask above the cancel price (2655).
        kill_tick = Tick(
            symbol="XAUUSD", bid=2654.90, ask=2655.10, time=_utcnow()
        )
        await engine.on_tick(kill_tick)

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        assert fresh.status == BlockStatus.INVALID
        # All pendings cancelled.
        for o in fresh.orders:
            assert o.state == OrderState.CANCELLED

        types = [n.type for n in events]
        assert NotificationType.BLOCK_INVALID in types


class TestSingleRungWin:
    """Rung 1 fills, price reverses, TP hits → BLOCK_WIN."""

    @pytest.mark.asyncio
    async def test_first_rung_tp_wins_the_block(
        self, engine_and_events, xau_spec
    ) -> None:
        engine, events, broker = engine_and_events

        # Seed.
        await broker.feed_tick(
            Tick(symbol="XAUUSD", bid=2640.30, ask=2640.50, time=_utcnow())
        )
        plan = _make_xauusd_plan(xau_spec)
        block = await engine.create_block(plan, chat_id=42)

        # Drop price so rung 1 (entry 2631.46) fills. The mock's
        # matching engine fires the fill the moment ASK <= entry.
        fill_tick = Tick(
            symbol="XAUUSD", bid=2631.26, ask=2631.46, time=_utcnow()
        )
        # We need the engine to see the fill ALSO, so its DB stays
        # in sync — the mock_adapter pre-fills any matching pendings
        # but the engine only learns about it on the next on_tick.
        # Trigger fill on the mock first, then drive the engine.
        await broker.feed_tick(fill_tick)
        await engine.on_tick(fill_tick)

        # Now move ASK up to the TP (≈ 2643.12).
        win_tick = Tick(
            symbol="XAUUSD", bid=2643.20, ask=2643.40, time=_utcnow()
        )
        await broker.feed_tick(win_tick)
        await engine.on_tick(win_tick)

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)

        assert fresh.status == BlockStatus.WIN
        # Net PnL is positive.
        assert fresh.net_pnl is not None
        assert fresh.net_pnl > 0
        # At least one rung TP'd.
        tp_count = sum(1 for o in fresh.orders if o.state == OrderState.TP_HIT)
        assert tp_count >= 1
        # Every remaining pending order was cancelled.
        for o in fresh.orders:
            assert o.state in {
                OrderState.TP_HIT,
                OrderState.CANCELLED,
                OrderState.SL_HIT,  # shouldn't happen in this path but allowed
            }

        types = [n.type for n in events]
        assert NotificationType.BLOCK_WIN in types


class TestManualClose:
    """/cancel from Telegram closes pending + open positions."""

    @pytest.mark.asyncio
    async def test_cancel_block_after_one_fill(
        self, engine_and_events, xau_spec
    ) -> None:
        engine, events, broker = engine_and_events

        await broker.feed_tick(
            Tick(symbol="XAUUSD", bid=2640.30, ask=2640.50, time=_utcnow())
        )
        plan = _make_xauusd_plan(xau_spec)
        block = await engine.create_block(plan, chat_id=42)

        # Fill rung 1.
        fill_tick = Tick(
            symbol="XAUUSD", bid=2631.26, ask=2631.46, time=_utcnow()
        )
        await broker.feed_tick(fill_tick)
        await engine.on_tick(fill_tick)

        await engine.cancel_block(block.id)

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        assert fresh.is_terminal
        # No leftover pending or open positions on the broker.
        assert (await broker.list_pending_orders(symbol="XAUUSD")) == []
        assert (await broker.list_open_positions(symbol="XAUUSD")) == []
        types = [n.type for n in events]
        assert NotificationType.BLOCK_MANUAL_CLOSE in types
