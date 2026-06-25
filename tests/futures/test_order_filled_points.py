"""Tests for the SL/TP-in-points suffix on ORDER_FILLED messages.

When the trader sees ``SL: 1.13496023  TP: 1.13342064`` they have
no quick way to tell whether 45 points away is what the chain rule
should produce, or whether something went sideways. Showing the
distance inline as ``(45.5 п)`` saves them subtracting prices in
their head — and surfaces obvious bugs (zero-distance SL, TP on
the wrong side of entry) at a glance.

These tests pin the rendering contract: distance is shown when
present, omitted when absent, and the unit label stays consistent
across instruments with very different point sizes.
"""

from __future__ import annotations

import pytest

from futures_bot.bot.formatters import _points_suffix, render_notification
from futures_bot.core.notifications import NotificationType, order_filled


# ---------------------------------------------------------------------
# Pure-function helper
# ---------------------------------------------------------------------

class TestPointsSuffix:
    """``_points_suffix`` decides what gets appended next to SL/TP prices."""

    def test_none_yields_empty_string(self):
        # Backward compatibility: old payloads without the points
        # fields must still render cleanly, not as 'None п'.
        assert _points_suffix(None) == ""

    def test_zero_distance_is_rendered_not_dropped(self):
        # 0 is a legitimate value (e.g. broker quietly stripped the
        # SL on a degraded connection). Showing it lets the operator
        # notice — a silently empty string would hide the anomaly.
        assert _points_suffix(0.0) == " <i>(0.0 п)</i>"

    @pytest.mark.parametrize(
        "raw,expected_text",
        [
            # EURUSDm: SL distance 0.000455, point 0.00001 → 45.5 п
            (45.489, "45.5"),
            # XAUUSD-style: distance 2.55, point 0.01 → 255 п
            (254.999, "255.0"),
            # Round-half-up at one decimal.
            (45.45, "45.5"),
        ],
    )
    def test_one_decimal_precision(self, raw, expected_text):
        out = _points_suffix(raw)
        assert expected_text in out

    def test_wraps_in_italic_for_subtle_styling(self):
        # The distance is supporting information — italic keeps it
        # visually subordinate to the actual price.
        out = _points_suffix(45.5)
        assert out.startswith(" <i>(")
        assert out.endswith(" п)</i>")


# ---------------------------------------------------------------------
# End-to-end rendering
# ---------------------------------------------------------------------

class TestOrderFilledRenderingWithPoints:
    """Full ORDER_FILLED notification with the new distance fields."""

    def test_eurusd_style_5digit_points(self):
        # Mirror block #4 rung #1 exactly so a regression on the
        # live-test format leaps out of the diff.
        n = order_filled(
            block_id=4, chat_id=1, seq=1,
            entry=1.13450534, sl=1.13496023, tp=1.13342064,
            lot=0.02, spread=0.00008,
            sl_points=45.489, tp_points=108.47,
        )
        out = render_notification(n)
        # SL line carries the distance suffix.
        assert "1.13496023" in out
        assert "(45.5 п)" in out
        # TP line carries its own distance suffix.
        assert "1.13342064" in out
        assert "(108.5 п)" in out
        # Lot + spread still rendered alongside.
        assert "0.02" in out
        assert "Спред" in out

    def test_payload_without_points_renders_unchanged_legacy_shape(self):
        # Older notifications persisted before the engine started
        # passing sl_points/tp_points must still render without the
        # word "None" sneaking into the output.
        n = order_filled(
            block_id=5, chat_id=1, seq=2,
            entry=1.13484, sl=1.13530, tp=1.13310,
            lot=0.03, spread=0.00008,
        )
        out = render_notification(n)
        assert "None" not in out
        assert "(0.0 п)" not in out          # not faked into "0 п"
        # SL/TP prices still present in the message.
        assert "1.1353" in out
        assert "1.1331" in out

    def test_notification_type_unchanged(self):
        # Just guarding against accidental refactors that would
        # change the wire shape of the notification.
        n = order_filled(
            block_id=1, chat_id=1, seq=1,
            entry=1.0, sl=0.99, tp=1.03,
            lot=0.01, spread=0.0001,
            sl_points=10.0, tp_points=30.0,
        )
        assert n.type == NotificationType.ORDER_FILLED
        assert n.block_id == 1
