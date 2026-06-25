"""Tests for the stats-window picker keyboard.

Three concerns:

* Every window we support is reachable (no orphan callback strings).
* The Back row points at the main menu, not at a non-existent state.
* The window label helper degrades gracefully on unexpected values.
"""

from __future__ import annotations

import pytest

from futures_bot.bot.formatters import _window_label
from futures_bot.bot.keyboards import (
    CB_MENU_BACK,
    CB_STATS_1Y,
    CB_STATS_30D,
    CB_STATS_3MO,
    CB_STATS_6MO,
    CB_STATS_7D,
    CB_STATS_ALL,
    CB_STATS_TODAY,
    stats_window_keyboard,
)


class TestStatsWindowKeyboard:
    """Static shape — no FSM, no DB, just the buttons."""

    def test_every_window_callback_is_present(self):
        kb = stats_window_keyboard()
        all_callbacks = [
            b.callback_data for row in kb.inline_keyboard for b in row
        ]
        for cb in (
            CB_STATS_TODAY, CB_STATS_7D, CB_STATS_30D,
            CB_STATS_3MO, CB_STATS_6MO, CB_STATS_1Y,
            CB_STATS_ALL,
        ):
            assert cb in all_callbacks, f"window button {cb!r} missing"

    def test_back_button_returns_to_main_menu(self):
        kb = stats_window_keyboard()
        last_row = kb.inline_keyboard[-1]
        # Back is always a full-width row at the bottom — both for
        # visual breathing room and so a fat-finger doesn't ping it
        # while reaching for a window.
        assert len(last_row) == 1
        assert last_row[0].callback_data == CB_MENU_BACK

    def test_callback_payload_carries_window_size_in_days(self):
        # The handler parses the integer after the colon to derive
        # the days arg. Anchor the convention so a refactor doesn't
        # silently re-arrange it.
        assert CB_STATS_TODAY == "stats:1"
        assert CB_STATS_7D == "stats:7"
        assert CB_STATS_ALL == "stats:0"     # 0 means all-time


class TestWindowLabel:
    """``_window_label`` translates days into Russian for the heading."""

    @pytest.mark.parametrize("days,expected", [
        (None, "за всё время"),
        (1, "за сегодня"),
        (7, "за 7 дней"),
        (30, "за 30 дней"),
        (90, "за 3 месяца"),
        (365, "за 1 год"),
    ])
    def test_known_windows_have_human_labels(self, days, expected):
        assert _window_label(days) == expected

    def test_unknown_window_falls_back_to_generic(self):
        # 14-day window isn't in the picker but the formatter still
        # has to render something reasonable.
        assert "14" in _window_label(14)
