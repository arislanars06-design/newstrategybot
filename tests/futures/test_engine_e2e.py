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

    @pytest.mark.asyncio
    async def test_double_cancel_tick_does_not_fire_invalid_twice(
        self, engine_and_events, xau_spec
    ) -> None:
        """Two ticks past cancel emit BLOCK_INVALID exactly once.

        Reproduces a live-test issue where the duplicate ⚫ INVALID
        message was hitting the channel twice in quick succession.
        Without the idempotency guard in _handle_cancel_price_hit,
        the second tick re-runs the handler against the now-INVALID
        block and emits another notification.
        """
        engine, events, broker = engine_and_events

        await broker.feed_tick(
            Tick(symbol="XAUUSD", bid=2640.30, ask=2640.50, time=_utcnow())
        )
        plan = _make_xauusd_plan(xau_spec)
        block = await engine.create_block(plan, chat_id=42)

        # First tick: crosses the cancel line.
        kill_tick = Tick(
            symbol="XAUUSD", bid=2654.90, ask=2655.10, time=_utcnow()
        )
        await engine.on_tick(kill_tick)
        # Second tick a beat later, still past cancel.
        kill_tick_2 = Tick(
            symbol="XAUUSD", bid=2655.10, ask=2655.30, time=_utcnow()
        )
        await engine.on_tick(kill_tick_2)

        invalid_count = sum(
            1 for n in events if n.type == NotificationType.BLOCK_INVALID
        )
        assert invalid_count == 1, (
            f"BLOCK_INVALID fired {invalid_count} times — expected 1"
        )

        # And the block is still INVALID (no flip / re-open).
        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        assert fresh.status == BlockStatus.INVALID


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


class TestFullChainLoss:
    """All 6 rungs fill, all 6 SL out → BLOCK_LOSS.

    The user's live block #4 walked this exact path and produced
    the expected 🔴 BLOCK LOSS message. This test pins the path so
    a refactor of the engine's exit logic can't silently break it.
    """

    @pytest.mark.asyncio
    async def test_chain_of_sl_hits_marks_block_loss(
        self, engine_and_events, xau_spec
    ) -> None:
        engine, events, broker = engine_and_events

        # Seed an above-entries tick so the broker has a baseline.
        await broker.feed_tick(
            Tick(symbol="XAUUSD", bid=2640.30, ask=2640.50, time=_utcnow())
        )
        plan = _make_xauusd_plan(xau_spec)
        block = await engine.create_block(plan, chat_id=42)

        # Drive the tick price down past every rung's entry in turn,
        # letting the engine see each fill on its own ``on_tick``.
        sorted_rungs = sorted(plan.rungs, key=lambda r: r.seq)
        last_rung = sorted_rungs[-1]
        for rung in sorted_rungs:
            fill_tick = Tick(
                symbol="XAUUSD",
                bid=rung.entry - 0.30,
                ask=rung.entry,
                time=_utcnow(),
            )
            await broker.feed_tick(fill_tick)
            await engine.on_tick(fill_tick)

        # Drop price well below the last rung's SL so every open
        # position closes at its (chained) SL.
        kill_tick = Tick(
            symbol="XAUUSD",
            bid=last_rung.sl - 5.0,
            ask=last_rung.sl - 4.5,
            time=_utcnow(),
        )
        await broker.feed_tick(kill_tick)
        await engine.on_tick(kill_tick)

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)

        # Engine: block marked LOSS, every order in SL_HIT, net PnL
        # negative.
        assert fresh.status == BlockStatus.LOSS
        assert fresh.net_pnl is not None and fresh.net_pnl < 0
        sl_count = sum(1 for o in fresh.orders if o.state == OrderState.SL_HIT)
        assert sl_count == 6, f"expected 6 SL_HIT orders, got {sl_count}"

        # Notification: BLOCK_LOSS emitted exactly once, addressed
        # to the block's chat, carrying the net PnL payload.
        loss_events = [
            n for n in events if n.type == NotificationType.BLOCK_LOSS
        ]
        assert len(loss_events) == 1
        assert loss_events[0].chat_id == 42
        assert loss_events[0].payload.get("net_pnl") is not None


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
