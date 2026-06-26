"""Startup state-reconciliation tests for :class:`BlockEngine`.

When the bot wakes up after a crash, SIGTERM, or VPS reboot, its DB
view of the world is by definition stale: orders may have filled,
positions may have SL'd or TP'd, and pending limits may have been
yanked off the broker. ``engine.reconcile_open_blocks()`` is the
single entry point that walks every non-terminal block, asks the
broker what its true state is, and patches the DB before the tick
watcher starts pumping fresh quotes.

These tests pin every branch of that reconciliation logic against
the :class:`MockAdapter`. The mock exposes ``record_position_close``
and lets us mutate ``_pending`` / ``_positions`` directly so we can
stage the "bot was offline while X happened" scenarios cleanly.

Scenarios covered (one TestClass per cluster of related branches):

* ``TestQuietRestart`` — broker matches DB, no notification.
* ``TestPendingTransitions`` — pending orders that filled or were
  cancelled while the bot was down.
* ``TestPositionExits`` — open positions that left the broker via
  SL / TP / manual close while the bot was down.
* ``TestEdgeCases`` — missing close-history, no open blocks, one
  consolidated notification per block.
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
from futures_bot.db import repository, session_scope
from futures_bot.db.database import close_db, init_db
from futures_bot.db.enums import BlockSide, BlockStatus, OrderState
from futures_bot.strategy.plan import build_plan
from futures_bot.strategy.risk import SymbolSpec


# ---------------------------------------------------------------------
# Test settings + fixtures — copied verbatim from test_engine_e2e.py
# so the reconciliation suite stays runnable in isolation. Duplication
# is cheaper than a shared conftest here because the two suites live
# side-by-side and a single change to one stays local.
# ---------------------------------------------------------------------

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
    import futures_bot.config as config_mod

    config_mod._settings = _test_settings()
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
# Helpers shared across the test classes.
# ---------------------------------------------------------------------

async def _create_baseline_block(engine, broker, xau_spec, chat_id: int = 42):
    """Create a fully active block and clear the BLOCK_CREATED noise.

    Returns the block we just created. The test typically grabs its
    ID, then asserts on reconcile-time notifications without having
    to filter out the creation event.
    """
    await broker.feed_tick(
        Tick(symbol="XAUUSD", bid=2640.30, ask=2640.50, time=_utcnow())
    )
    plan = _make_xauusd_plan(xau_spec)
    return await engine.create_block(plan, chat_id=chat_id)


def _reconcile_events(events: list[Notification]) -> list[Notification]:
    """Slice out just the BLOCK_RECONCILED notifications."""
    return [e for e in events if e.type == NotificationType.BLOCK_RECONCILED]


# =====================================================================
# 1. Quiet restart — nothing changed on the broker.
# =====================================================================

class TestQuietRestart:
    """Broker state matches the DB: reconcile is a no-op."""

    @pytest.mark.asyncio
    async def test_no_change_when_all_orders_still_pending(
        self, engine_and_events, xau_spec
    ) -> None:
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)
        events.clear()

        await engine.reconcile_open_blocks()

        # No notifications — the block's state was already consistent.
        assert _reconcile_events(events) == []

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        assert fresh.status == BlockStatus.ACTIVE
        for order in fresh.orders:
            assert order.state == OrderState.PENDING


# =====================================================================
# 2. Pending → something while bot was down.
# =====================================================================

class TestPendingTransitions:
    """Pending orders that filled or were dropped during downtime."""

    @pytest.mark.asyncio
    async def test_pending_that_filled_becomes_open(
        self, engine_and_events, xau_spec
    ) -> None:
        """A pending limit that filled into a position is recovered."""
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)
        events.clear()

        # Simulate: rung #1 filled while the bot was offline. Move
        # the pending into the open-positions ledger by hand.
        first = next(o for o in block.orders if o.seq == 1)
        ticket = first.entry_ticket
        assert ticket
        pending = broker._pending.pop(ticket)
        OpenPosCls = _placeholder_position()
        broker._positions[ticket] = OpenPosCls(
            ticket=ticket,
            request=pending.request,
            fill_price=first.entry_price,
            sl=None,
            tp=None,
        )

        await engine.reconcile_open_blocks()

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        recovered = next(o for o in fresh.orders if o.seq == 1)
        assert recovered.state == OrderState.OPEN
        assert recovered.position_ticket == ticket

        # One BLOCK_RECONCILED notification, no synthetic ORDER_FILLED.
        recon = _reconcile_events(events)
        assert len(recon) == 1
        assert any("fill recovered" in c for c in recon[0].payload["changes"])

    @pytest.mark.asyncio
    async def test_pending_that_vanished_becomes_cancelled(
        self, engine_and_events, xau_spec
    ) -> None:
        """A pending that's no longer on the broker is marked cancelled."""
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)
        events.clear()

        # Drop one pending from the broker entirely (no position
        # created either — pure cancellation).
        first = next(o for o in block.orders if o.seq == 1)
        broker._pending.pop(first.entry_ticket)

        await engine.reconcile_open_blocks()

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        recovered = next(o for o in fresh.orders if o.seq == 1)
        assert recovered.state == OrderState.CANCELLED
        # Other rungs untouched.
        for other in fresh.orders:
            if other.seq != 1:
                assert other.state == OrderState.PENDING

        recon = _reconcile_events(events)
        assert len(recon) == 1
        assert any("cancelled" in c for c in recon[0].payload["changes"])

    @pytest.mark.asyncio
    async def test_all_pendings_vanished_block_finalises_to_invalid(
        self, engine_and_events, xau_spec
    ) -> None:
        """All pendings dropped → block resolves to INVALID by zero PnL."""
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)
        events.clear()

        # Wipe every pending order off the broker.
        broker._pending.clear()

        await engine.reconcile_open_blocks()

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        assert fresh.status == BlockStatus.INVALID
        for order in fresh.orders:
            assert order.state == OrderState.CANCELLED
        assert fresh.net_pnl == 0.0

        recon = _reconcile_events(events)
        assert len(recon) == 1
        assert recon[0].payload["new_status"] == "INVALID"


# =====================================================================
# 3. Open positions that exited the broker while we were down.
# =====================================================================

class TestPositionExits:
    """Positions that left the broker via SL / TP / manual close."""

    async def _fill_rung_then_offline_close(
        self,
        broker,
        order,
        *,
        close_reason: str,
        close_price: float,
        profit: float,
    ):
        """Move ``order`` from PENDING through OPEN, then strip the
        position from the broker and record a closing deal so
        reconciliation has something to attribute.
        """
        ticket = order.entry_ticket
        assert ticket
        # First simulate the fill that happened while we were online:
        # move pending → open. We update the engine's DB state via
        # repository so reconciliation sees the order in OPEN.
        async with session_scope() as session:
            fresh = await repository.get_order_by_entry_ticket(
                session, ticket
            )
            assert fresh is not None
            await repository.mark_order_filled(
                session,
                fresh,
                fill_price=order.entry_price,
                fill_spread=0.0,
                sl_live=order.sl_price_plan,
                tp_live=order.entry_price + 5.0,
                position_ticket=ticket,
                filled_at=_utcnow(),
            )
        # Now mutate the mock to forget the position and remember it
        # as a closed deal in history.
        pending = broker._pending.pop(ticket, None)
        if pending is not None:
            # In our flow the order already filled — drop the
            # leftover pending entry.
            pass
        broker._positions.pop(ticket, None)
        broker.record_position_close(
            ticket=ticket,
            close_price=close_price,
            close_time=_utcnow(),
            profit=profit,
            reason=close_reason,
        )

    @pytest.mark.asyncio
    async def test_open_position_sl_recovered_as_sl_hit(
        self, engine_and_events, xau_spec
    ) -> None:
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)
        first = next(o for o in block.orders if o.seq == 1)
        await self._fill_rung_then_offline_close(
            broker,
            first,
            close_reason="SL",
            close_price=first.sl_price_plan,
            profit=-4.93,
        )
        events.clear()

        await engine.reconcile_open_blocks()

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        recovered = next(o for o in fresh.orders if o.seq == 1)
        assert recovered.state == OrderState.SL_HIT
        assert recovered.pnl_usd == pytest.approx(-4.93)

        recon = _reconcile_events(events)
        assert len(recon) == 1
        assert any("SL" in c for c in recon[0].payload["changes"])

    @pytest.mark.asyncio
    async def test_open_position_tp_recovers_and_wins_block(
        self, engine_and_events, xau_spec
    ) -> None:
        """A position that TP'd during downtime → block goes WIN, pendings cancel."""
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)
        first = next(o for o in block.orders if o.seq == 1)
        await self._fill_rung_then_offline_close(
            broker,
            first,
            close_reason="TP",
            close_price=first.entry_price + 5.0,
            profit=14.79,
        )
        events.clear()

        await engine.reconcile_open_blocks()

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)

        # Block won via the reconciliation path.
        assert fresh.status == BlockStatus.WIN
        win_rung = next(o for o in fresh.orders if o.seq == 1)
        assert win_rung.state == OrderState.TP_HIT
        # The other 5 rungs were cancelled by finalise.
        for o in fresh.orders:
            if o.seq != 1:
                assert o.state == OrderState.CANCELLED

        # One BLOCK_RECONCILED — and BLOCK_WIN (since _finalise fires).
        types = [n.type for n in events]
        assert NotificationType.BLOCK_RECONCILED in types
        assert NotificationType.BLOCK_WIN in types

    @pytest.mark.asyncio
    async def test_open_position_manual_close_keeps_pnl(
        self, engine_and_events, xau_spec
    ) -> None:
        """Manual close (third-party tool) → CANCELLED state with PnL preserved."""
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)
        first = next(o for o in block.orders if o.seq == 1)
        await self._fill_rung_then_offline_close(
            broker,
            first,
            close_reason="MANUAL",
            close_price=first.entry_price + 0.50,
            profit=1.23,
        )
        events.clear()

        await engine.reconcile_open_blocks()

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        recovered = next(o for o in fresh.orders if o.seq == 1)
        assert recovered.state == OrderState.CANCELLED
        # The broker-reported P&L survives onto the row even though
        # state is CANCELLED (we don't have a dedicated "manual"
        # enum value).
        assert recovered.pnl_usd == pytest.approx(1.23)


# =====================================================================
# 4. Edge cases.
# =====================================================================

class TestEdgeCases:
    """Empty block list, no broker history, single-notification guarantee."""

    @pytest.mark.asyncio
    async def test_no_open_blocks_returns_silently(
        self, engine_and_events, xau_spec
    ) -> None:
        engine, events, _ = engine_and_events

        await engine.reconcile_open_blocks()

        assert _reconcile_events(events) == []

    @pytest.mark.asyncio
    async def test_missing_close_history_marks_cancelled_with_zero_pnl(
        self, engine_and_events, xau_spec
    ) -> None:
        """Position vanished, no closing deal in history → CANCELLED + 0 P&L."""
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)
        first = next(o for o in block.orders if o.seq == 1)

        # Move the order to OPEN via repository, then drop both the
        # position AND any closing deal from the broker.
        async with session_scope() as session:
            fresh_order = await repository.get_order_by_entry_ticket(
                session, first.entry_ticket
            )
            assert fresh_order is not None
            await repository.mark_order_filled(
                session,
                fresh_order,
                fill_price=first.entry_price,
                fill_spread=0.0,
                sl_live=first.sl_price_plan,
                tp_live=first.entry_price + 5.0,
                position_ticket=first.entry_ticket,
                filled_at=_utcnow(),
            )
        broker._pending.pop(first.entry_ticket, None)
        broker._positions.pop(first.entry_ticket, None)
        # Intentionally do NOT call record_position_close.
        events.clear()

        await engine.reconcile_open_blocks()

        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
        recovered = next(o for o in fresh.orders if o.seq == 1)
        assert recovered.state == OrderState.CANCELLED
        assert recovered.pnl_usd == pytest.approx(0.0)

        recon = _reconcile_events(events)
        assert len(recon) == 1
        assert any("no broker history" in c for c in recon[0].payload["changes"])

    @pytest.mark.asyncio
    async def test_one_consolidated_notification_per_block(
        self, engine_and_events, xau_spec
    ) -> None:
        """Many simultaneous changes → exactly one BLOCK_RECONCILED."""
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)

        # Stage three different kinds of change in one go:
        #  - rung #1: pending vanished (cancelled)
        #  - rung #2: pending → position (fill recovered)
        #  - rung #3: pending vanished (cancelled)
        rung1 = next(o for o in block.orders if o.seq == 1)
        rung2 = next(o for o in block.orders if o.seq == 2)
        rung3 = next(o for o in block.orders if o.seq == 3)
        broker._pending.pop(rung1.entry_ticket, None)
        pending2 = broker._pending.pop(rung2.entry_ticket)
        broker._positions[rung2.entry_ticket] = _placeholder_position()(
            ticket=rung2.entry_ticket,
            request=pending2.request,
            fill_price=rung2.entry_price,
            sl=None,
            tp=None,
        )
        broker._pending.pop(rung3.entry_ticket, None)
        events.clear()

        await engine.reconcile_open_blocks()

        recon = _reconcile_events(events)
        assert len(recon) == 1, (
            "BLOCK_RECONCILED must be emitted exactly once per block, "
            f"got {len(recon)}"
        )
        # And it carries three change strings — one per affected rung.
        assert len(recon[0].payload["changes"]) == 3

    @pytest.mark.asyncio
    async def test_terminal_block_is_skipped(
        self, engine_and_events, xau_spec
    ) -> None:
        """Already-terminal blocks aren't re-reconciled."""
        engine, events, broker = engine_and_events
        block = await _create_baseline_block(engine, broker, xau_spec)

        # Force the block to a terminal status by hand so it's not
        # in list_active_blocks anymore.
        async with session_scope() as session:
            fresh = await repository.get_block(session, block.id)
            assert fresh is not None
            await repository.set_block_status(
                session,
                fresh,
                BlockStatus.WIN,
                closed_at=_utcnow(),
                net_pnl=12.34,
            )
        events.clear()

        await engine.reconcile_open_blocks()

        assert _reconcile_events(events) == []


# ---------------------------------------------------------------------
# Internal: synthesise a mock-shaped _OpenPosition without naming it.
# ---------------------------------------------------------------------

def _placeholder_position():
    """Return the mock's private ``_OpenPosition`` dataclass type.

    The mock keeps ``_OpenPosition`` module-private, but the tests
    need to construct one when we hand-craft a "this position came
    online while bot was offline" state. Reaching in by name keeps
    the test honest about what part of the mock it's depending on
    (and breaks loudly if the symbol moves).
    """
    from futures_bot.adapters import mock_adapter as _m
    return _m._OpenPosition          # noqa: WPS437
