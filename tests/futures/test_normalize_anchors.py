"""Tests for the anchor auto-swap helper.

``normalize_anchors`` is the pure function the Telegram handler calls
before :func:`build_plan` so the most common user mistake — typing
the two anchor prices in the order they appeared on the chart rather
than the order the strategy requires — doesn't blow up with a
validation error. The handler shows a notice when a swap occurred.

These tests pin the behaviour to its small contract: swap iff the
ordering is wrong for the chosen side, otherwise pass through.
"""

from __future__ import annotations

import pytest

from futures_bot.db.enums import BlockSide
from futures_bot.strategy.plan import normalize_anchors


class TestBuyOrientation:
    """BUY expects zero_price > hundred_price (entries descend)."""

    def test_correct_order_passes_through_unchanged(self):
        new_zero, new_hundred, swapped = normalize_anchors(
            BlockSide.BUY, zero_price=2660.0, hundred_price=2640.0,
        )
        assert (new_zero, new_hundred) == (2660.0, 2640.0)
        assert swapped is False

    def test_wrong_order_is_swapped(self):
        # Trader typed the low first by mistake.
        new_zero, new_hundred, swapped = normalize_anchors(
            BlockSide.BUY, zero_price=2640.0, hundred_price=2660.0,
        )
        assert (new_zero, new_hundred) == (2660.0, 2640.0)
        assert swapped is True


class TestSellOrientation:
    """SELL expects zero_price < hundred_price (entries ascend)."""

    def test_correct_order_passes_through_unchanged(self):
        new_zero, new_hundred, swapped = normalize_anchors(
            BlockSide.SELL, zero_price=2640.0, hundred_price=2660.0,
        )
        assert (new_zero, new_hundred) == (2640.0, 2660.0)
        assert swapped is False

    def test_wrong_order_is_swapped(self):
        new_zero, new_hundred, swapped = normalize_anchors(
            BlockSide.SELL, zero_price=2660.0, hundred_price=2640.0,
        )
        assert (new_zero, new_hundred) == (2640.0, 2660.0)
        assert swapped is True


class TestEdgeCases:
    """Equal prices are left to downstream validation."""

    @pytest.mark.parametrize("side", [BlockSide.BUY, BlockSide.SELL])
    def test_equal_prices_pass_through_without_swap(self, side):
        # A genuinely bad input (zero == hundred) should reach
        # build_plan, which has the right error message for it. The
        # auto-swap step shouldn't silently mask it.
        new_zero, new_hundred, swapped = normalize_anchors(side, 100.0, 100.0)
        assert (new_zero, new_hundred) == (100.0, 100.0)
        assert swapped is False
