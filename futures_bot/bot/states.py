"""FSM states for the ``/newblock`` flow.

The futures bot has a single creation FSM (no /track or alternative
``/fib`` like the crypto bot — the uniform Fibonacci ladder is the
only flow). The six steps map 1:1 to the trader's discussion:
symbol, side, the two anchors, base risk, cancel price, then confirm.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class NewBlockFSM(StatesGroup):
    """Step-by-step prompts for ``/newblock``.

    Order matches the natural human reading of a block plan: pick
    the instrument and direction first, then the price anchors,
    then the risk knob, then confirm.

    Note: the ``CANCEL_PRICE`` state was removed in the
    auto-cancel-disabled refactor — the bot now skips the cancel-
    price prompt entirely. The state symbol itself is kept as an
    alias of ``CONFIRM`` so old persisted FSM contexts (cached in
    aiogram MemoryStorage) don't crash on resume; new flows never
    visit it.
    """

    SYMBOL = State()
    SIDE = State()
    ZERO_PRICE = State()
    HUNDRED_PRICE = State()
    BASE_RISK = State()
    CONFIRM = State()
