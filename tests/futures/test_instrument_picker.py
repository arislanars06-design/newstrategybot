"""Unit tests for the /newblock instrument picker keyboard.

The keyboard is the only piece of UI a trader is expected to see
*every* time they open a new block, so the small invariants here
matter: emoji fall-back, partial-row layout, callback-data round-trip
to the symbol literal, and the trailing Custom/Back row.

These tests deliberately avoid spinning up an aiogram dispatcher.
``InlineKeyboardMarkup`` is a plain Pydantic model so we can read
its structure directly.
"""

from __future__ import annotations

import pytest

from futures_bot.bot.keyboards import (
    CB_MENU_BLOCK,
    CB_SYM_CUSTOM,
    CB_SYM_PICK,
    _emoji_for,
    instrument_picker_keyboard,
)
from futures_bot.config import Settings


# ---------------------------------------------------------------------
# Emoji resolver
# ---------------------------------------------------------------------

class TestEmojiResolver:
    """``_emoji_for`` is the only place where symbol names map to glyphs."""

    @pytest.mark.parametrize("symbol,emoji", [
        ("XAUUSD", "🥇"),
        ("XAGUSD", "🥈"),
        ("EURUSD", "💶"),
        ("BTCUSD", "₿"),
    ])
    def test_known_majors_get_their_emoji(self, symbol, emoji):
        assert _emoji_for(symbol) == emoji

    def test_lookup_is_case_insensitive(self):
        # The picker uppercases on input but the broker may surface
        # mixed-case in alerts; `_emoji_for` should not care.
        assert _emoji_for("xauusd") == _emoji_for("XAUUSD")

    @pytest.mark.parametrize("variant", ["XAUUSDm", "XAUUSD.s", "XAUUSD_pro"])
    def test_prefix_match_inherits_parent_emoji(self, variant):
        # Exness / IC Markets / Pepperstone all have suffixed variants
        # for mini, raw, ECN etc. Without the prefix fallback those
        # would silently lose their nice emoji.
        assert _emoji_for(variant) == "🥇"

    def test_unknown_symbol_falls_back_to_chart_glyph(self):
        # Indices, exotic FX, custom CFDs — anything we haven't
        # explicitly mapped should still get a non-empty leading glyph
        # so the button layout stays uniform.
        assert _emoji_for("NAS100") == "📊"
        assert _emoji_for("US500.cash") == "📊"


# ---------------------------------------------------------------------
# Keyboard layout
# ---------------------------------------------------------------------

class TestInstrumentPickerKeyboard:
    """Layout invariants of :func:`instrument_picker_keyboard`."""

    def test_callback_data_round_trips_to_symbol(self):
        kb = instrument_picker_keyboard(["XAUUSD"])
        button = kb.inline_keyboard[0][0]
        assert button.callback_data == f"{CB_SYM_PICK}XAUUSD"
        # And the data prefix is stable, so the handler can split safely.
        assert button.callback_data is not None
        assert button.callback_data.startswith(CB_SYM_PICK)
        recovered = button.callback_data[len(CB_SYM_PICK):]
        assert recovered == "XAUUSD"

    def test_grid_wraps_at_three_columns_by_default(self):
        kb = instrument_picker_keyboard(
            ["A1", "B2", "C3", "D4", "E5", "F6", "G7"]
        )
        # 7 symbols → 3 + 3 + 1 + the trailing Custom/Back row.
        symbol_rows = kb.inline_keyboard[:-1]
        assert [len(r) for r in symbol_rows] == [3, 3, 1]

    def test_partial_row_is_preserved(self):
        # Without preservation, a 4-symbol list would render only the
        # first row, hiding the last symbol from the trader.
        kb = instrument_picker_keyboard(["A", "B", "C", "D"])
        symbol_rows = kb.inline_keyboard[:-1]
        # 4 → 3 + 1
        assert [len(r) for r in symbol_rows] == [3, 1]
        assert symbol_rows[1][0].callback_data == f"{CB_SYM_PICK}D"

    def test_custom_columns_count_is_respected(self):
        kb = instrument_picker_keyboard(
            ["A", "B", "C", "D", "E"], columns=2
        )
        # 5 symbols at 2 cols → 2 + 2 + 1 + Custom row.
        symbol_rows = kb.inline_keyboard[:-1]
        assert [len(r) for r in symbol_rows] == [2, 2, 1]

    def test_trailing_row_has_custom_then_back(self):
        kb = instrument_picker_keyboard(["XAUUSD"])
        last_row = kb.inline_keyboard[-1]
        assert len(last_row) == 2
        assert last_row[0].callback_data == CB_SYM_CUSTOM
        assert last_row[1].callback_data == CB_MENU_BLOCK

    def test_empty_strings_in_input_are_skipped(self):
        # Operators sometimes leave a trailing comma or extra space in
        # their .env; the keyboard shouldn't render an unclickable
        # blank-text button.
        kb = instrument_picker_keyboard(["", "XAUUSD", "  ", "EURUSD"])
        symbol_rows = kb.inline_keyboard[:-1]
        labels = [b.text for r in symbol_rows for b in r]
        assert all(label.strip() for label in labels)
        assert any("XAUUSD" in label for label in labels)
        assert any("EURUSD" in label for label in labels)
        # Two non-empty entries → exactly two symbol buttons.
        assert sum(len(r) for r in symbol_rows) == 2


# ---------------------------------------------------------------------
# Settings glue
# ---------------------------------------------------------------------

class TestQuickSymbolsParsing:
    """``Settings.quick_symbols_list`` is the only consumer of .env."""

    def _settings(self, value: str) -> Settings:
        return Settings(
            mt5_login=1,
            mt5_password="x",
            mt5_server="x",
            telegram_bot_token="x",
            telegram_notify_chat_id=1,
            quick_symbols=value,
        )

    def test_default_covers_the_common_majors(self):
        s = Settings(
            mt5_login=1, mt5_password="x", mt5_server="x",
            telegram_bot_token="x", telegram_notify_chat_id=1,
        )
        # Don't pin to an exact list — operators may tune it. Just
        # check the most-traded instruments survived defaults so a
        # green-field deploy doesn't show a one-symbol picker.
        defaults = s.quick_symbols_list
        assert "XAUUSD" in defaults
        assert "EURUSD" in defaults
        assert len(defaults) >= 5

    def test_csv_is_split_and_trimmed(self):
        s = self._settings(" XAUUSD , EURUSD,GBPUSD ")
        assert s.quick_symbols_list == ["XAUUSD", "EURUSD", "GBPUSD"]

    def test_blank_entries_are_dropped(self):
        # Forgiving parser: a stray comma in .env shouldn't surface as
        # an empty button.
        s = self._settings("XAUUSD,,EURUSD,")
        assert s.quick_symbols_list == ["XAUUSD", "EURUSD"]

    def test_empty_string_yields_empty_list(self):
        # Operator can hide the picker by setting FB_QUICK_SYMBOLS=""
        # → handler will render only the Custom + Back row.
        s = self._settings("")
        assert s.quick_symbols_list == []
