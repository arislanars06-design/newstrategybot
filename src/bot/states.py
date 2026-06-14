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


class FibBlockFSM(StatesGroup):
    """Bot-driven block creation from Fibonacci levels.

    The trader supplies five values; the bot computes everything else
    (entries, TPs, SLs, per-rung qty) from those plus the leverage
    the trader sets explicitly.

    The cancel price is **not** asked: it is taken automatically from
    the 0% anchor price, which by construction sits strictly outside
    the entry ladder (for BUY blocks the ladder runs downward from
    the 0% level; for SELL blocks it runs upward). This keeps the FSM
    at six prompts and removes a degree of freedom the trader said
    they didn't need.
    """

    SYMBOL = State()
    SIDE = State()
    ZERO_PRICE = State()
    HUNDRED_PRICE = State()
    FIRST_RISK = State()
    LEVERAGE = State()
    CONFIRM = State()


class StatsRangeFSM(StatesGroup):
    """Two-step prompt for ``/stats`` custom date range.

    Both inputs are interpreted as Tashkent local dates (YYYY-MM-DD).
    The handler converts them to UTC for the DB query: SINCE becomes
    00:00 of that local day, UNTIL becomes 23:59:59 of that local day,
    so a same-day range covers the whole day.
    """

    SINCE = State()
    UNTIL = State()
