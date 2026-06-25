"""Pinning tests for the /newblock ⬅️ Назад navigation.

The back button is a generic FSM-rewind: every step from SIDE
through CONFIRM exposes a Back, and the handler should walk the
state graph backwards one node per tap. These tests live at the
state-graph level — they don't fire fake aiogram events, they just
verify the lookup table covers every step and points the right
direction.

That's enough to catch the regressions we care about (forgetting
to add a state to the table when extending the FSM, swapping two
arrows). Full integration testing would need an aiogram TestClient
which costs much more setup for little extra coverage.
"""

from __future__ import annotations

import pytest

from futures_bot.bot.handlers import _PREVIOUS_STATE
from futures_bot.bot.keyboards import (
    CB_FSM_BACK,
    CB_MENU_BLOCK,
    CB_SYM_CUSTOM,
    back_only_keyboard,
    confirm_keyboard,
    instrument_picker_keyboard,
    side_keyboard,
)
from futures_bot.bot.states import NewBlockFSM


# ---------------------------------------------------------------------
# State-graph contract
# ---------------------------------------------------------------------

class TestPreviousStateTable:
    """``_PREVIOUS_STATE`` is the authoritative back-link table."""

    def test_every_state_after_symbol_has_a_predecessor(self):
        # SYMBOL is the only step without a predecessor — its
        # picker keyboard's Back goes to the block submenu, not
        # through the FSM rewind.
        states_with_back = [
            NewBlockFSM.SIDE,
            NewBlockFSM.ZERO_PRICE,
            NewBlockFSM.HUNDRED_PRICE,
            NewBlockFSM.BASE_RISK,
            NewBlockFSM.CANCEL_PRICE,
            NewBlockFSM.CONFIRM,
        ]
        for s in states_with_back:
            assert s.state in _PREVIOUS_STATE, (
                f"{s.state} missing from _PREVIOUS_STATE — adding a "
                f"new FSM step without wiring its Back is a UX trap"
            )

    @pytest.mark.parametrize(
        "current,expected_prev",
        [
            (NewBlockFSM.SIDE,          NewBlockFSM.SYMBOL),
            (NewBlockFSM.ZERO_PRICE,    NewBlockFSM.SIDE),
            (NewBlockFSM.HUNDRED_PRICE, NewBlockFSM.ZERO_PRICE),
            (NewBlockFSM.BASE_RISK,     NewBlockFSM.HUNDRED_PRICE),
            (NewBlockFSM.CANCEL_PRICE,  NewBlockFSM.BASE_RISK),
            (NewBlockFSM.CONFIRM,       NewBlockFSM.CANCEL_PRICE),
        ],
    )
    def test_back_link_goes_to_immediate_predecessor(self, current, expected_prev):
        assert _PREVIOUS_STATE[current.state] == expected_prev.state

    def test_symbol_is_not_in_the_table(self):
        # SYMBOL's "back" exits the FSM entirely (returns to the
        # block menu); registering it here would create a no-op or,
        # worse, a self-loop.
        assert NewBlockFSM.SYMBOL.state not in _PREVIOUS_STATE


# ---------------------------------------------------------------------
# Keyboard wiring — every interactive step exposes the Back callback
# ---------------------------------------------------------------------

def _all_callback_data(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class TestBackButtonOnEveryKeyboard:
    """Each keyboard with a forward action also exposes ⬅️ Назад."""

    def test_side_keyboard_has_back(self):
        assert CB_FSM_BACK in _all_callback_data(side_keyboard())

    def test_confirm_keyboard_has_back(self):
        # Confirm still has its ❌ Отмена (clear-FSM) escape hatch,
        # plus the standard Back to re-edit the cancel-price step.
        cbs = _all_callback_data(confirm_keyboard())
        assert CB_FSM_BACK in cbs

    def test_back_only_keyboard_has_only_back(self):
        kb = back_only_keyboard()
        cbs = _all_callback_data(kb)
        assert cbs == [CB_FSM_BACK]
        # Layout sanity — single row, single column.
        assert len(kb.inline_keyboard) == 1
        assert len(kb.inline_keyboard[0]) == 1

    def test_instrument_picker_has_block_menu_back(self):
        # Picker's Back leaves the FSM (goes to the block submenu);
        # it intentionally does NOT use CB_FSM_BACK because there's
        # no predecessor inside the FSM to rewind to.
        cbs = _all_callback_data(instrument_picker_keyboard(["XAUUSD"]))
        assert CB_MENU_BLOCK in cbs
        # And it offers Custom-text fallback regardless.
        assert CB_SYM_CUSTOM in cbs
