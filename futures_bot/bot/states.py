"""FSM states for the ``/newblock`` flow.

The futures bot has a single creation FSM (no /track or alternative
``/fib`` like the crypto bot — the uniform Fibonacci ladder is the
only flow). The five steps map 1:1 to the trader's discussion:
symbol, side, the two anchors, base risk, then confirm.

Cancel price is **not** asked: it is taken automatically from the 0%
anchor price, which by construction sits strictly outside the entry
ladder (above for BUY, below for SELL). Hiding it from the FSM
shaves one step and removes the ``> first entry`` / ``< first entry``
edge case the trader can otherwise type wrong.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class NewBlockFSM(StatesGroup):
    """Step-by-step prompts for ``/newblock``.

    Order matches the natural human reading of a block plan: pick the
    instrument and direction first, then the price anchors, then the
    risk knob. Confirm is a yes/no on the rendered plan.
    """

    SYMBOL = State()
    SIDE = State()
    ZERO_PRICE = State()
    HUNDRED_PRICE = State()
    BASE_RISK = State()
    CONFIRM = State()
