"""Unit tests for the /newblock instrument picker keyboard.

The keyboard is the only piece of UI a trader sees *every* time
they open a new block, so the layout invariants matter:

* Each button carries the symbol's tier emoji as a prefix.
* Tier ordering — A 🥇 first, then B 🥈, C 🥉, D ⚠️, unknown 📊 last.
* Operator-supplied within-tier order is preserved (the operator
  can still nudge "EURJPY before EURAUD" via ``FB_QUICK_SYMBOLS``).
* No section-header rows — the legend lives in the FSM prompt
  message, the keyboard is just buttons.
* Footer ``✏️ Другой / ⬅️ Назад`` always present.

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
    DEFAULT_QUICK_SYMBOLS,
    INSTRUMENT_PICKER_LEGEND,
    _tier_for,
    instrument_picker_keyboard,
)
from futures_bot.config import Settings


def _split(kb):
    """Return ``(symbol_rows, footer_row)`` ignoring trailing footer."""
    return kb.inline_keyboard[:-1], kb.inline_keyboard[-1]


# ---------------------------------------------------------------------
# Tier classifier
# ---------------------------------------------------------------------

class TestTierResolver:
    """``_tier_for`` is the single source of truth for tier emojis."""

    @pytest.mark.parametrize("symbol,tier", [
        ("XAUUSD",  "A"),
        ("GBPJPY",  "A"),
        ("AUDJPY",  "A"),
        ("EURUSD",  "B"),
        ("USDCAD",  "B"),
        ("NZDJPY",  "C"),
        ("USDCHF",  "C"),
        ("EURGBP",  "D"),
        ("CADCHF",  "D"),
    ])
    def test_known_symbols_get_their_tier(self, symbol, tier):
        assert _tier_for(symbol) == tier

    def test_unknown_symbol_falls_to_question_tier(self):
        # Things not in the table (crypto, indices, custom CFDs) get
        # a neutral '?' marker — the keyboard renders that as 📊.
        assert _tier_for("BTCUSD") == "?"
        assert _tier_for("NAS100") == "?"

    def test_lookup_is_case_insensitive(self):
        assert _tier_for("xauusd") == "A"

    def test_suffixed_variant_inherits_parent_tier(self):
        # Exness's mini variants must NOT silently drop to '?'.
        assert _tier_for("XAUUSDm") == "A"
        assert _tier_for("EURUSD.s") == "B"
        assert _tier_for("NZDJPY.cash") == "C"


# ---------------------------------------------------------------------
# Default symbol set
# ---------------------------------------------------------------------

class TestDefaults:
    """The default config covers the full 29-symbol tier roster."""

    def test_default_quick_symbols_has_exactly_29_entries(self):
        # Locking the count here forces a deliberate decision when
        # the tier table grows.
        assert len(DEFAULT_QUICK_SYMBOLS) == 29

    def test_default_starts_with_tier_a(self):
        # First symbol must be a tier-A pick so the picker leads
        # with a recommended option.
        first = DEFAULT_QUICK_SYMBOLS[0]
        assert _tier_for(first) == "A"

    def test_settings_default_matches_default_roster(self):
        s = Settings(
            mt5_login=1, mt5_password="x", mt5_server="x",
            telegram_bot_token="x", telegram_notify_chat_id=1,
        )
        # Order-preserving equality so an operator who reads .env
        # gets exactly the picker layout they saw documented.
        assert s.quick_symbols_list == list(DEFAULT_QUICK_SYMBOLS)


# ---------------------------------------------------------------------
# Keyboard layout
# ---------------------------------------------------------------------

class TestInstrumentPickerKeyboard:
    """Layout invariants of :func:`instrument_picker_keyboard`."""

    def test_button_carries_tier_emoji_prefix(self):
        kb = instrument_picker_keyboard(["XAUUSD"])
        rows, _ = _split(kb)
        assert rows[0][0].text == "🥇 XAUUSD"

    def test_unknown_symbol_gets_neutral_glyph(self):
        kb = instrument_picker_keyboard(["BTCUSD"])
        rows, _ = _split(kb)
        assert rows[0][0].text == "📊 BTCUSD"

    def test_callback_carries_unmodified_symbol(self):
        # The handler routes by the symbol literal in the payload;
        # any tier emoji in the button TEXT must NOT bleed into the
        # callback data.
        kb = instrument_picker_keyboard(["XAUUSD"])
        rows, _ = _split(kb)
        assert rows[0][0].callback_data == f"{CB_SYM_PICK}XAUUSD"

    def test_buttons_render_in_tier_order(self):
        # Mixed input — A, D, B, C, ? — must come out A, B, C, D, ?.
        kb = instrument_picker_keyboard(
            ["EURGBP", "EURUSD", "XAUUSD", "NZDJPY", "BTCUSD"],
            columns=5,
        )
        rows, _ = _split(kb)
        seq = [b.callback_data for b in rows[0]]
        # Symbol order in the row should be tier-rank ascending.
        assert seq == [
            f"{CB_SYM_PICK}XAUUSD",   # A
            f"{CB_SYM_PICK}EURUSD",   # B
            f"{CB_SYM_PICK}NZDJPY",   # C
            f"{CB_SYM_PICK}EURGBP",   # D
            f"{CB_SYM_PICK}BTCUSD",   # ?
        ]

    def test_within_tier_order_preserves_operator_input(self):
        # Both tier-A — the operator's order survives the sort.
        kb = instrument_picker_keyboard(
            ["AUDJPY", "XAUUSD", "GBPJPY"], columns=3,
        )
        rows, _ = _split(kb)
        symbols = [b.text.split(" ", 1)[1] for b in rows[0]]
        assert symbols == ["AUDJPY", "XAUUSD", "GBPJPY"]

    def test_three_column_wrap_is_default(self):
        kb = instrument_picker_keyboard(
            list(DEFAULT_QUICK_SYMBOLS)
        )
        rows, _ = _split(kb)
        # 29 symbols / 3 cols = 9 full rows + 1 partial of 2.
        assert [len(r) for r in rows] == [3] * 9 + [2]

    def test_custom_columns_count_respected(self):
        kb = instrument_picker_keyboard(["XAUUSD", "EURUSD"], columns=2)
        rows, _ = _split(kb)
        assert [len(r) for r in rows] == [2]

    def test_empty_strings_are_dropped(self):
        kb = instrument_picker_keyboard(["", "XAUUSD", "  ", "EURUSD"])
        rows, _ = _split(kb)
        labels = [b.text for r in rows for b in r]
        assert all(label.strip() for label in labels)
        assert sum(len(r) for r in rows) == 2

    def test_no_symbols_still_renders_footer(self):
        # FB_QUICK_SYMBOLS="" hides every quick-pick; the trader can
        # still recover via Custom or back out via Назад.
        kb = instrument_picker_keyboard([])
        rows, footer = _split(kb)
        assert rows == []
        assert footer[0].callback_data == CB_SYM_CUSTOM
        assert footer[1].callback_data == CB_MENU_BLOCK

    def test_footer_is_always_last(self):
        # Whatever the body looks like, the operator-recovery row is
        # the final row of the keyboard.
        kb = instrument_picker_keyboard(list(DEFAULT_QUICK_SYMBOLS))
        last = kb.inline_keyboard[-1]
        assert len(last) == 2
        assert last[0].callback_data == CB_SYM_CUSTOM
        assert last[1].callback_data == CB_MENU_BLOCK


# ---------------------------------------------------------------------
# Legend message
# ---------------------------------------------------------------------

class TestLegend:
    """The legend string is rendered above the keyboard."""

    def test_legend_lists_all_four_tier_emojis(self):
        for emoji in ("🥇", "🥈", "🥉", "⚠️"):
            assert emoji in INSTRUMENT_PICKER_LEGEND


# ---------------------------------------------------------------------
# Settings glue
# ---------------------------------------------------------------------

class TestQuickSymbolsParsing:
    """``Settings.quick_symbols_list`` parses the CSV from ``.env``."""

    def _settings(self, value: str) -> Settings:
        return Settings(
            mt5_login=1,
            mt5_password="x",
            mt5_server="x",
            telegram_bot_token="x",
            telegram_notify_chat_id=1,
            quick_symbols=value,
        )

    def test_csv_is_split_and_trimmed(self):
        s = self._settings(" XAUUSD , EURUSD,GBPUSD ")
        assert s.quick_symbols_list == ["XAUUSD", "EURUSD", "GBPUSD"]

    def test_blank_entries_are_dropped(self):
        s = self._settings("XAUUSD,,EURUSD,")
        assert s.quick_symbols_list == ["XAUUSD", "EURUSD"]

    def test_empty_string_yields_empty_list(self):
        s = self._settings("")
        assert s.quick_symbols_list == []
