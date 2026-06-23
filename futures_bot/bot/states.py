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

    Order matches the natural human reading of a block plan: pick the
    instrument and direction first, then the price anchors, then the
    risk knob, then the safety cancel. Confirm is a yes/no on the
    rendered plan.
    """

    SYMBOL = State()
    SIDE = State()
    ZERO_PRICE = State()
    HUNDRED_PRICE = State()
    BASE_RISK = State()
    CANCEL_PRICE = State()
    CONFIRM = State()
