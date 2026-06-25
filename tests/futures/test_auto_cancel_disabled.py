"""Tests for the auto-cancel-disabled refactor.

The bot no longer asks for a cancel price; ``cancel_price`` flows
through the system as ``None`` and the resulting block's
``cancel_price_active`` flag is False from creation. These tests
pin the contract end-to-end:

* :func:`build_plan` accepts ``cancel_price=None`` without validating
  orientation, and the resulting :class:`BlockPlan` keeps the value
  None for downstream consumers.
* :func:`BlockEngine.create_block` translates that into a Block with
  ``cancel_price=zero_price`` (placeholder, since the column is NOT
  NULL) and ``cancel_price_active=False`` so the engine never tries
  to evaluate the guard.
* :func:`format_plan_preview` renders the cancel-price slot as
  ``не используется`` so the trader sees the explicit "off" state.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from futures_bot.adapters.base import OrderRequest, OrderResult, SymbolInfo
from futures_bot.adapters.mock_adapter import MockAdapter
from futures_bot.bot.formatters import _cancel_price_label, format_plan_preview
from futures_bot.config import Settings
from futures_bot.core.engine import BlockEngine
from futures_bot.db import session_scope, repository
from futures_bot.db.database import close_db, init_db
from futures_bot.db.enums import BlockSide
from futures_bot.strategy.plan import build_plan
from futures_bot.strategy.risk import SymbolSpec


def _settings() -> Settings:
    return Settings(
        mt5_login=1,
        mt5_password="x",
        mt5_server="x",
        telegram_bot_token="x",
        telegram_notify_chat_id=1,
        database_url="sqlite+aiosqlite:///:memory:",
    )


def _xau_spec() -> SymbolSpec:
    """A realistic-enough XAUUSD spec for plan math."""
    return SymbolSpec(
        symbol="XAUUSD",
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        volume_min=0.01,
        volume_max=200.0,
        volume_step=0.01,
    )


# ---------------------------------------------------------------------
# Pure plan layer
# ---------------------------------------------------------------------

class TestBuildPlanCancelOptional:
    """``build_plan`` accepts ``cancel_price=None`` and propagates it."""

    def test_default_omits_cancel_price(self):
        # Without ``cancel_price=`` at all, the kwarg defaults to None.
        plan = build_plan(
            symbol="XAUUSD",
            side=BlockSide.BUY,
            zero_price=2660.0,
            hundred_price=2640.0,
            base_risk_usd=5.0,
            symbol_spec=_xau_spec(),
        )
        assert plan.cancel_price is None

    def test_explicit_none_works(self):
        plan = build_plan(
            symbol="XAUUSD",
            side=BlockSide.BUY,
            zero_price=2660.0,
            hundred_price=2640.0,
            base_risk_usd=5.0,
            cancel_price=None,
            symbol_spec=_xau_spec(),
        )
        assert plan.cancel_price is None

    def test_none_skips_orientation_validation(self):
        # Without the guard, even an "impossible" zero/hundred combo
        # for the side is allowed — the validation isn't there to
        # second-guess the trader, it's there to catch a mis-typed
        # cancel price.
        plan = build_plan(
            symbol="XAUUSD",
            side=BlockSide.BUY,
            zero_price=2660.0,
            hundred_price=2640.0,
            base_risk_usd=5.0,
            cancel_price=None,
            symbol_spec=_xau_spec(),
        )
        assert plan.rungs        # we got rungs; nothing else raised

    def test_explicit_cancel_still_validates(self):
        # Backward-compat check — the API surface for callers that
        # do pass a cancel price hasn't shifted.
        with pytest.raises(ValueError, match="ABOVE"):
            build_plan(
                symbol="XAUUSD",
                side=BlockSide.BUY,
                zero_price=2660.0,
                hundred_price=2640.0,
                base_risk_usd=5.0,
                # Below first entry → invalid for BUY.
                cancel_price=2640.0,
                symbol_spec=_xau_spec(),
            )


# ---------------------------------------------------------------------
# Formatter / label
# ---------------------------------------------------------------------

class TestCancelPriceLabel:
    """``_cancel_price_label`` decides what the preview shows."""

    def test_none_renders_explicit_off_text(self):
        assert _cancel_price_label(None) == "не используется"

    def test_real_value_passes_through_as_string(self):
        assert _cancel_price_label(2665.0) == "2665.0"


class TestPlanPreviewWithNoCancel:
    """Preview must not show a stray numeric placeholder."""

    def test_preview_contains_the_off_phrase(self):
        plan = build_plan(
            symbol="XAUUSD",
            side=BlockSide.BUY,
            zero_price=2660.0,
            hundred_price=2640.0,
            base_risk_usd=5.0,
            symbol_spec=_xau_spec(),
        )
        out = format_plan_preview(plan, typical_spread=0.05)
        assert "не используется" in out
        # Spot-check: the heading still renders and the rung table
        # is intact (no exception threw out part of the message).
        assert "XAUUSD" in out
        assert "BUY" in out


# ---------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    """Fresh in-memory DB per test."""
    import futures_bot.config as config_mod
    config_mod._settings = _settings()
    from futures_bot.db import database as db_mod
    db_mod._engine = None
    db_mod._session_factory = None

    await init_db()
    yield
    await close_db()


class TestCreateBlockWithoutCancel:
    """``BlockEngine.create_block`` handles ``plan.cancel_price = None``."""

    @pytest.mark.asyncio
    async def test_block_starts_with_cancel_disabled(self, db) -> None:
        adapter = MockAdapter()
        adapter.add_symbol(SymbolInfo(
            symbol="XAUUSD",
            digits=2,
            point=0.01,
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            trade_contract_size=100.0,
            volume_min=0.01,
            volume_max=200.0,
            volume_step=0.01,
            trade_stops_level=0,
            spread_typical=0.05,
        ))
        await adapter.connect()
        engine = BlockEngine(settings=_settings(), adapter=adapter)

        plan = build_plan(
            symbol="XAUUSD",
            side=BlockSide.BUY,
            zero_price=2660.0,
            hundred_price=2640.0,
            base_risk_usd=5.0,
            symbol_spec=_xau_spec(),
        )
        block = await engine.create_block(plan, chat_id=1)
        assert block is not None
        assert block.cancel_price_active is False
        # Placeholder for the NOT NULL column — equals zero_price.
        assert block.cancel_price == pytest.approx(plan.zero_price)
