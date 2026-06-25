"""Regression tests for the 'reported max risk vs actual loss' bug.

Live testing on EURUSDm (Exness mini) surfaced a mismatch: the bot
showed "Макс. риск: $11.721" at block creation but the block's
6 chained SLs lost a total of $15.921 — about 36% more than
advertised. Root cause: the plan sized lots and computed
``total_real_risk`` against the bare Fibonacci step distance, while
the engine adds an extra ``sl_spread_safety × spread`` buffer to
every SL at fill time. The plan was not aware of that buffer.

These tests pin the fix:

* :func:`build_plan` now accepts ``typical_spread`` and
  ``sl_spread_safety`` and uses ``step + safety × spread`` as the
  effective SL distance for both lot sizing and risk reporting.
* ``BlockPlan.sl_distance`` and ``BlockPlan.total_real_risk()`` are
  computed in that effective space, so the BLOCK_CREATED
  notification matches the SLs the trader will actually see.
* Defaults (no spread args) keep the historic behaviour for every
  existing test path.

The Telegram notification renderer also gets a smaller fix: render
the cancel-price field through ``_cancel_price_label`` so the
``None`` payload becomes the human phrase "не используется" instead
of the literal word "None".
"""

from __future__ import annotations

import pytest

from futures_bot.bot.formatters import render_notification
from futures_bot.core.notifications import (
    NotificationType,
    block_created,
)
from futures_bot.db.enums import BlockSide
from futures_bot.strategy.plan import build_plan
from futures_bot.strategy.risk import SymbolSpec


def _eurusd_spec() -> SymbolSpec:
    """EURUSDm spec lifted from the Exness mini-account live test."""
    return SymbolSpec(
        symbol="EURUSDm",
        trade_tick_size=0.00001,
        trade_tick_value=1.0,        # $1 / tick / 1.0 lot
        volume_min=0.01,
        volume_max=200.0,
        volume_step=0.01,
    )


# ---------------------------------------------------------------------
# Plan layer
# ---------------------------------------------------------------------

class TestEffectiveSlDistance:
    """``build_plan`` sizes against ``step + safety × spread``."""

    def test_typical_spread_defaults_to_zero_preserves_old_lots(self):
        # Backward-compat anchor: every test that doesn't pass the
        # new args gets the same lot sizes as before.
        spec = _eurusd_spec()
        plan_old_default = build_plan(
            symbol="EURUSDm",
            side=BlockSide.SELL,
            zero_price=1.13285,
            hundred_price=1.13551,
            base_risk_usd=0.50,
            symbol_spec=spec,
        )
        plan_zero_spread = build_plan(
            symbol="EURUSDm",
            side=BlockSide.SELL,
            zero_price=1.13285,
            hundred_price=1.13551,
            base_risk_usd=0.50,
            symbol_spec=spec,
            typical_spread=0.0,
            sl_spread_safety=1.5,
        )
        assert [r.lot for r in plan_old_default.rungs] == [
            r.lot for r in plan_zero_spread.rungs
        ]
        assert plan_old_default.sl_distance == plan_zero_spread.sl_distance

    def test_effective_sl_includes_safety_buffer(self):
        spec = _eurusd_spec()
        spread = 0.00008                       # ~ live EURUSDm spread
        safety = 1.5
        plan = build_plan(
            symbol="EURUSDm",
            side=BlockSide.SELL,
            zero_price=1.13285,
            hundred_price=1.13551,
            base_risk_usd=0.50,
            symbol_spec=spec,
            typical_spread=spread,
            sl_spread_safety=safety,
        )
        # Step on the user's range = (hundred-zero) × 0.1273
        # ~ 0.00266 × 0.1273 = 0.0003385.
        # Effective SL = step + safety × spread = 0.0003385 + 0.00012
        # ~ 0.000459.
        assert plan.sl_distance == pytest.approx(
            0.000338 + safety * spread, rel=0.05
        )

    def test_total_real_risk_predicts_chained_sl_loss(self):
        """Reported max risk now matches the sum of every rung's SL loss.

        Before the fix the plan computed real risk against the bare
        Fibonacci step distance while the engine added a
        ``safety × spread`` buffer at fill time — so the bot
        promised X but the chained-SL outcome was X + buffer × total_lot.

        Now both numbers are in the same space. Reported max risk
        equals sum of (lot × effective_sl_distance × value_per_unit)
        which IS the worst-case chained-SL outcome, modulo live-spread
        drift between plan time and fill time.
        """
        spec = _eurusd_spec()
        spread = 0.00008
        safety = 1.5
        plan = build_plan(
            symbol="EURUSDm",
            side=BlockSide.SELL,
            zero_price=1.13285,
            hundred_price=1.13551,
            base_risk_usd=0.50,
            symbol_spec=spec,
            typical_spread=spread,
            sl_spread_safety=safety,
        )
        # Hand-compute the worst-case loss against the same effective
        # SL distance the engine will use. Ratio is the only thing
        # that matters — typing tick_value/tick_size into the test
        # tightens what we're really checking.
        value_per_unit = spec.trade_tick_value / spec.trade_tick_size
        manual = sum(
            r.lot * plan.sl_distance * value_per_unit for r in plan.rungs
        )
        # Plan rounds to 4 decimals; allow that drift.
        assert plan.total_real_risk() == pytest.approx(manual, abs=0.05)

    def test_reported_total_matches_per_rung_sum(self):
        """Sanity: total === Σ per-rung real_risk_usd, no hidden field."""
        spec = _eurusd_spec()
        plan = build_plan(
            symbol="EURUSDm",
            side=BlockSide.SELL,
            zero_price=1.13285,
            hundred_price=1.13551,
            base_risk_usd=0.50,
            symbol_spec=spec,
            typical_spread=0.00008,
            sl_spread_safety=1.5,
        )
        per_rung_sum = sum(r.real_risk_usd for r in plan.rungs)
        assert plan.total_real_risk() == pytest.approx(per_rung_sum, abs=0.01)

    def test_lots_get_smaller_as_safety_increases(self):
        """Bigger safety buffer → bigger effective SL → smaller lots."""
        spec = _eurusd_spec()
        plan_small = build_plan(
            symbol="EURUSDm", side=BlockSide.SELL,
            zero_price=1.13285, hundred_price=1.13551,
            base_risk_usd=2.0, symbol_spec=spec,
            typical_spread=0.00008, sl_spread_safety=0.5,
        )
        plan_big = build_plan(
            symbol="EURUSDm", side=BlockSide.SELL,
            zero_price=1.13285, hundred_price=1.13551,
            base_risk_usd=2.0, symbol_spec=spec,
            typical_spread=0.00008, sl_spread_safety=3.0,
        )
        # Final rung is most sensitive — compare it.
        assert plan_small.rungs[-1].lot >= plan_big.rungs[-1].lot


# ---------------------------------------------------------------------
# Notification renderer
# ---------------------------------------------------------------------

class TestCancelPriceRenderingInBlockCreated:
    """The BLOCK_CREATED notification must not print the word 'None'."""

    def test_none_cancel_renders_as_friendly_phrase(self):
        n = block_created(
            block_id=42,
            chat_id=1,
            symbol="EURUSDm",
            side="SELL",
            orders=6,
            cancel_price=None,
            total_real_risk=15.92,
        )
        out = render_notification(n)
        # The bug: previously the bot wrote 'Цена отмены: None'.
        # The fix surfaces the explicit disabled-state phrase.
        assert "None" not in out
        assert "не используется" in out

    def test_numeric_cancel_passes_through_unchanged(self):
        n = block_created(
            block_id=42,
            chat_id=1,
            symbol="XAUUSD",
            side="BUY",
            orders=6,
            cancel_price=2665.0,
            total_real_risk=15.92,
        )
        out = render_notification(n)
        # Numeric value renders verbatim — no "не используется" line.
        assert "не используется" not in out
        assert "2665" in out
