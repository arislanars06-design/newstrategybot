"""Finite-state machine states for the interactive ``/newblock`` flow."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class NewBlockFSM(StatesGroup):
    """Step-by-step prompts the user goes through to compose a block."""

    SYMBOL = State()
    SIDE = State()
    ENTRIES = State()
    TPS = State()
    LAST_SL = State()
    CANCEL_PRICE = State()
    QTY = State()
    CONFIRM = State()


class TrackBlockFSM(StatesGroup):
    """Adopt user-placed orders into a tracked block.

    The bot only needs symbol, side, and cancel price — every other
    detail (entries, TPs, SLs, quantity) is read from Binance.
    """

    SYMBOL = State()
    SIDE = State()
    DISCOVERED = State()
    CANCEL_PRICE = State()
    CONFIRM = State()
