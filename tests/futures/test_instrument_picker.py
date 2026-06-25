"""Unit tests for the /newblock instrument picker keyboard.

The keyboard is the only piece of UI a trader is expected to see
*every* time they open a new block, so the small invariants here
matter:

* Emoji fall-back per symbol (including suffix inheritance).
* Symbols grouped into categories in a stable order.
* Section-header rows present + ignored by handlers.
* Callback data round-trips to the symbol literal.
* Trailing Custom/Back row always present.

These tests deliberately avoid spinning up an aiogram dispatcher.
``InlineKeyboardMarkup`` is a plain Pydantic model so we can read
its structure directly.
"""

from __future__ import annotations

import pytest

from futures_bot.bot.keyboards import (
    CB_MENU_BLOCK,
    CB_SYM_CUSTOM,
    CB_SYM_HEADER,
    CB_SYM_PICK,
    _classify,
    _emoji_for,
    instrument_picker_keyboard,
)
from futures_bot.config import Settings


# ---------------------------------------------------------------------
# Helpers — split the keyboard into its three logical parts.
# ---------------------------------------------------------------------

def _split_rows(kb):
    """Return (headers, symbol_rows, footer_row).

    Section headers are single-button rows whose callback is
    :data:`CB_SYM_HEADER`. Symbol rows are everything between header
    rows. The footer is always the last row.
    """
    rows = kb.inline_keyboard
    footer = rows[-1]
    body = rows[:-1]
    headers = [r for r in body if len(r) == 1 and r[0].callback_data == CB_SYM_HEADER]
    symbol_rows = [
        r for r in body
        if not (len(r) == 1 and r[0].callback_data == CB_SYM_HEADER)
    ]
    return headers, symbol_rows, footer


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
        # The picker may receive mixed-case from a custom-text input;
        # the emoji helper should not care.
        assert _emoji_for("xauusd") == _emoji_for("XAUUSD")

    @pytest.mark.parametrize("variant", ["XAUUSDm", "XAUUSD.s", "XAUUSD_pro"])
    def test_prefix_match_inherits_parent_emoji(self, variant):
        # Exness / IC Markets / Pepperstone all have suffixed variants.
        # Without the prefix fallback those would silently lose the
        # nice emoji.
        assert _emoji_for(variant) == "🥇"

    def test_unknown_symbol_falls_back_to_chart_glyph(self):
        # Anything we haven't mapped should still get a non-empty
        # leading glyph so the button layout stays uniform.
        assert _emoji_for("US500.cash") == "📊"


# ---------------------------------------------------------------------
# Category classifier
# ---------------------------------------------------------------------

class TestClassifier:
    """``_classify`` decides which section a symbol belongs to."""

    @pytest.mark.parametrize("symbol,category", [
        ("XAUUSD", "metals"),
        ("EURUSD", "fx_major"),
        ("USDJPY", "fx_major"),
        ("EURJPY", "fx_cross"),
        ("BTCUSD", "crypto"),
        ("NAS100", "indices"),
    ])
    def test_known_symbols_classified(self, symbol, category):
        assert _classify(symbol) == category

    def test_unknown_falls_back_to_other(self):
        assert _classify("FOOBAR") == "other"

    def test_suffixed_symbols_inherit_parent_category(self):
        # 'XAUUSDm' should be metals via prefix match.
        assert _classify("XAUUSDm") == "metals"
        assert _classify("EURUSDm") == "fx_major"


# ---------------------------------------------------------------------
# Keyboard layout
# ---------------------------------------------------------------------

class TestInstrumentPickerKeyboard:
    """Layout invariants of :func:`instrument_picker_keyboard`."""

    def test_callback_data_round_trips_to_symbol(self):
        kb = instrument_picker_keyboard(["XAUUSD"])
        # Find the first non-header button.
        _, symbol_rows, _ = _split_rows(kb)
        button = symbol_rows[0][0]
        assert button.callback_data == f"{CB_SYM_PICK}XAUUSD"
        # And the data prefix is stable, so the handler can split safely.
        assert button.callback_data is not None
        assert button.callback_data.startswith(CB_SYM_PICK)
        recovered = button.callback_data[len(CB_SYM_PICK):]
        assert recovered == "XAUUSD"

    def test_single_category_renders_one_header(self):
        kb = instrument_picker_keyboard(["XAUUSD", "XAGUSD"])
        headers, symbol_rows, _ = _split_rows(kb)
        assert len(headers) == 1                       # only metals
        # Both metals fit in one row (2 ≤ default 3-column wrap).
        assert sum(len(r) for r in symbol_rows) == 2

    def test_multiple_categories_each_get_a_header(self):
        kb = instrument_picker_keyboard(
            ["XAUUSD", "EURUSD", "BTCUSD"]
        )
        headers, _, _ = _split_rows(kb)
        # Three distinct categories → three section headers.
        assert len(headers) == 3
        labels = [r[0].text for r in headers]
        # Stable order: metals before FX before crypto.
        assert "Металлы" in labels[0]
        assert "Major FX" in labels[1]
        assert "Crypto" in labels[2]

    def test_category_order_is_independent_of_input_order(self):
        # User listed crypto first, but the keyboard puts metals first.
        kb = instrument_picker_keyboard(["BTCUSD", "XAUUSD"])
        headers, _, _ = _split_rows(kb)
        assert "Металлы" in headers[0][0].text
        assert "Crypto" in headers[1][0].text

    def test_within_category_order_preserves_input(self):
        # Both major FX — should appear in the order the operator
        # configured them.
        kb = instrument_picker_keyboard(["USDJPY", "EURUSD", "GBPUSD"])
        _, symbol_rows, _ = _split_rows(kb)
        symbols_in_order = [
            b.text.split()[-1] for r in symbol_rows for b in r
        ]
        assert symbols_in_order == ["USDJPY", "EURUSD", "GBPUSD"]

    def test_grid_wraps_at_three_columns_by_default(self):
        kb = instrument_picker_keyboard(
            ["EURUSD", "GBPUSD", "USDJPY", "USDCHF"]
        )
        _, symbol_rows, _ = _split_rows(kb)
        # 4 majors at 3 cols → 3 + 1.
        assert [len(r) for r in symbol_rows] == [3, 1]

    def test_custom_columns_count_is_respected(self):
        kb = instrument_picker_keyboard(
            ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD"],
            columns=2,
        )
        _, symbol_rows, _ = _split_rows(kb)
        # 5 majors at 2 cols → 2 + 2 + 1.
        assert [len(r) for r in symbol_rows] == [2, 2, 1]

    def test_trailing_row_has_custom_then_back(self):
        kb = instrument_picker_keyboard(["XAUUSD"])
        _, _, footer = _split_rows(kb)
        assert len(footer) == 2
        assert footer[0].callback_data == CB_SYM_CUSTOM
        assert footer[1].callback_data == CB_MENU_BLOCK

    def test_empty_strings_in_input_are_skipped(self):
        kb = instrument_picker_keyboard(["", "XAUUSD", "  ", "EURUSD"])
        _, symbol_rows, _ = _split_rows(kb)
        labels = [b.text for r in symbol_rows for b in r]
        assert all(label.strip() for label in labels)
        # Two non-empty entries → exactly two symbol buttons.
        assert sum(len(r) for r in symbol_rows) == 2

    def test_unknown_symbol_lands_in_other_category(self):
        kb = instrument_picker_keyboard(["XAUUSD", "FOOBAR"])
        headers, _, _ = _split_rows(kb)
        # Two categories: metals + other.
        labels = [r[0].text for r in headers]
        assert any("Прочее" in label for label in labels)

    def test_no_symbols_still_renders_footer(self):
        # Operator may set FB_QUICK_SYMBOLS="" to hide the picker;
        # the keyboard must still surface Custom + Back so the
        # trader can recover.
        kb = instrument_picker_keyboard([])
        headers, symbol_rows, footer = _split_rows(kb)
        assert headers == []
        assert symbol_rows == []
        assert footer[0].callback_data == CB_SYM_CUSTOM
        assert footer[1].callback_data == CB_MENU_BLOCK


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
        defaults = s.quick_symbols_list
        assert "XAUUSD" in defaults
        assert "EURUSD" in defaults
        assert len(defaults) >= 5

    def test_csv_is_split_and_trimmed(self):
        s = self._settings(" XAUUSD , EURUSD,GBPUSD ")
        assert s.quick_symbols_list == ["XAUUSD", "EURUSD", "GBPUSD"]

    def test_blank_entries_are_dropped(self):
        s = self._settings("XAUUSD,,EURUSD,")
        assert s.quick_symbols_list == ["XAUUSD", "EURUSD"]

    def test_empty_string_yields_empty_list(self):
        s = self._settings("")
        assert s.quick_symbols_list == []
