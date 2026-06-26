"""Notification objects exchanged between the engine and the bot.

The engine never imports aiogram. Instead it builds typed
:class:`Notification` objects and pushes them through an
``on_notification`` callback the application wires up at startup. The
Telegram layer then renders each event into HTML and sends it to the
trader's chat and (for high-signal events only) to the public channel.

Decoupling the engine from the transport keeps the engine unit-testable
with no Telegram dependencies: tests collect the notifications into a
list and assert on the sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NotificationType(StrEnum):
    """Distinct event types the engine emits.

    Names mirror the crypto bot's ``src/core/notifications.py`` where
    they overlap; futures-only events (spread-alert, session-blocked)
    use new names.
    """

    BLOCK_CREATED = "BLOCK_CREATED"
    ORDER_FILLED = "ORDER_FILLED"
    SL_HIT = "SL_HIT"
    TP_HIT = "TP_HIT"          # reserved — distinct from BLOCK_WIN for clarity
    BLOCK_WIN = "BLOCK_WIN"
    BLOCK_LOSS = "BLOCK_LOSS"
    BLOCK_INVALID = "BLOCK_INVALID"
    BLOCK_ERROR = "BLOCK_ERROR"
    BLOCK_MANUAL_CLOSE = "BLOCK_MANUAL_CLOSE"
    BLOCK_RECONCILED = "BLOCK_RECONCILED"
    SPREAD_ALERT = "SPREAD_ALERT"
    SESSION_FILTER_BLOCKED = "SESSION_FILTER_BLOCKED"


@dataclass(slots=True, frozen=True)
class Notification:
    """One event from the engine to the bot.

    ``payload`` carries event-specific fields the renderer needs
    (prices, ticket numbers, P&L). Keeping it as a free-form dict
    means we can add fields without touching the engine/bot contract;
    keeping the *type* strict prevents typos from routing the event
    into the wrong renderer.
    """

    type: NotificationType
    block_id: int
    chat_id: int
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------
# Helper builders — these are NOT mandatory; the engine can construct
# Notification objects directly. They exist so call-sites read like
# plain English instead of a dict-shape free-for-all.
# ---------------------------------------------------------------------

def block_created(
    *, block_id: int, chat_id: int, **payload: Any
) -> Notification:
    return Notification(
        type=NotificationType.BLOCK_CREATED,
        block_id=block_id,
        chat_id=chat_id,
        payload=payload,
    )


def order_filled(
    *, block_id: int, chat_id: int, **payload: Any
) -> Notification:
    return Notification(
        type=NotificationType.ORDER_FILLED,
        block_id=block_id,
        chat_id=chat_id,
        payload=payload,
    )


def sl_hit(*, block_id: int, chat_id: int, **payload: Any) -> Notification:
    return Notification(
        type=NotificationType.SL_HIT,
        block_id=block_id,
        chat_id=chat_id,
        payload=payload,
    )


def block_win(*, block_id: int, chat_id: int, **payload: Any) -> Notification:
    return Notification(
        type=NotificationType.BLOCK_WIN,
        block_id=block_id,
        chat_id=chat_id,
        payload=payload,
    )


def block_loss(*, block_id: int, chat_id: int, **payload: Any) -> Notification:
    return Notification(
        type=NotificationType.BLOCK_LOSS,
        block_id=block_id,
        chat_id=chat_id,
        payload=payload,
    )


def block_invalid(
    *, block_id: int, chat_id: int, **payload: Any
) -> Notification:
    return Notification(
        type=NotificationType.BLOCK_INVALID,
        block_id=block_id,
        chat_id=chat_id,
        payload=payload,
    )


def block_error(
    *, block_id: int, chat_id: int, reason: str, **payload: Any
) -> Notification:
    return Notification(
        type=NotificationType.BLOCK_ERROR,
        block_id=block_id,
        chat_id=chat_id,
        payload={"reason": reason, **payload},
    )


def block_reconciled(
    *, block_id: int, chat_id: int, **payload: Any
) -> Notification:
    """Bot startup reconciliation finished for this block.

    Payload carries ``changes`` (a list of human-readable change
    strings the renderer joins into one message), ``new_status``
    (the recomputed block status string), and ``net_pnl`` (None
    when the block is still ACTIVE / CREATED).
    """
    return Notification(
        type=NotificationType.BLOCK_RECONCILED,
        block_id=block_id,
        chat_id=chat_id,
        payload=payload,
    )


def block_manual_close(
    *, block_id: int, chat_id: int, **payload: Any
) -> Notification:
    return Notification(
        type=NotificationType.BLOCK_MANUAL_CLOSE,
        block_id=block_id,
        chat_id=chat_id,
        payload=payload,
    )


def spread_alert(
    *, block_id: int, chat_id: int, **payload: Any
) -> Notification:
    return Notification(
        type=NotificationType.SPREAD_ALERT,
        block_id=block_id,
        chat_id=chat_id,
        payload=payload,
    )
