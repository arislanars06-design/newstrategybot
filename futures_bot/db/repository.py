"""Query helpers for the futures-bot DB layer.

All cross-table reads / atomic writes live here so the engine and the
Telegram handlers don't sprinkle SQL across the codebase. Mirrors the
crypto bot's ``src/db/repository.py`` style: every function takes an
:class:`AsyncSession` as its first argument and never commits — that
is the caller's job via ``session_scope``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from futures_bot.db.enums import BlockStatus, EventType, OrderState
from futures_bot.db.models import Block, Event, Order


# ---------------------------------------------------------------------
# Block reads
# ---------------------------------------------------------------------

async def get_block(session: AsyncSession, block_id: int) -> Block | None:
    """Return one block with eager-loaded orders and events.

    ``selectinload`` triggers a single follow-up query per relationship
    so the caller can render the block in one round-trip without
    triggering lazy loads from outside the session.
    """
    stmt = (
        select(Block)
        .where(Block.id == block_id)
        .options(selectinload(Block.orders), selectinload(Block.events))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_active_blocks(session: AsyncSession) -> Sequence[Block]:
    """All non-terminal blocks, ordered by creation (oldest first)."""
    stmt = (
        select(Block)
        .where(Block.status.in_([BlockStatus.CREATED, BlockStatus.ACTIVE]))
        .options(selectinload(Block.orders))
        .order_by(Block.created_at)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_blocks_by_symbol(
    session: AsyncSession, symbol: str
) -> Sequence[Block]:
    stmt = (
        select(Block)
        .where(Block.symbol == symbol)
        .options(selectinload(Block.orders))
        .order_by(Block.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


# ---------------------------------------------------------------------
# Order reads
# ---------------------------------------------------------------------

async def get_order_by_client_tag(
    session: AsyncSession, tag: str
) -> Order | None:
    """Look up an order by the idempotency tag we wrote into MT5 comment.

    Used during state-reconciliation on bot restart: we re-list MT5
    orders/positions and match them by ``client_tag`` to recover the
    in-memory state without trusting MT5 to remember our intent.
    """
    stmt = select(Order).where(Order.client_tag == tag)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_order_by_entry_ticket(
    session: AsyncSession, ticket: str
) -> Order | None:
    stmt = select(Order).where(Order.entry_ticket == ticket)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_order_by_position_ticket(
    session: AsyncSession, ticket: str
) -> Order | None:
    stmt = select(Order).where(Order.position_ticket == ticket)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------

async def append_event(
    session: AsyncSession,
    *,
    block_id: int,
    event_type: EventType,
    order_seq: int | None = None,
    payload: dict | None = None,
) -> Event:
    """Append an audit-log row. Caller flushes / commits."""
    event = Event(
        block_id=block_id,
        event_type=event_type,
        order_seq=order_seq,
        payload=payload,
    )
    session.add(event)
    return event


async def block_events(
    session: AsyncSession, block_id: int
) -> Sequence[Event]:
    stmt = (
        select(Event)
        .where(Event.block_id == block_id)
        .order_by(Event.created_at)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


# ---------------------------------------------------------------------
# Block status transitions
# ---------------------------------------------------------------------

async def set_block_status(
    session: AsyncSession,
    block: Block,
    status: BlockStatus,
    *,
    closed_at: datetime | None = None,
    net_pnl: float | None = None,
) -> None:
    """Mutate the block in place; caller commits."""
    block.status = status
    if status in {
        BlockStatus.WIN,
        BlockStatus.LOSS,
        BlockStatus.INVALID,
        BlockStatus.ERROR,
    }:
        block.closed_at = closed_at
        if net_pnl is not None:
            block.net_pnl = net_pnl


async def disable_cancel_price(session: AsyncSession, block: Block) -> None:
    """Mark the cancel-price guard as no longer applicable.

    The engine calls this on the first rung fill: once we own a
    position, the "cancel before any fill" rule doesn't apply
    anymore. Stored as a boolean so /list and /block can show the
    right hint.
    """
    block.cancel_price_active = False


# ---------------------------------------------------------------------
# Order status transitions
# ---------------------------------------------------------------------

async def mark_order_filled(
    session: AsyncSession,
    order: Order,
    *,
    fill_price: float,
    fill_spread: float,
    sl_live: float,
    tp_live: float,
    position_ticket: str,
    filled_at: datetime,
) -> None:
    order.state = OrderState.OPEN
    order.fill_price = fill_price
    order.fill_spread = fill_spread
    order.sl_price_live = sl_live
    order.tp_price_live = tp_live
    order.position_ticket = position_ticket
    order.filled_at = filled_at


async def mark_order_closed(
    session: AsyncSession,
    order: Order,
    *,
    state: OrderState,
    close_price: float,
    pnl_usd: float,
    commission_usd: float | None,
    closed_at: datetime,
) -> None:
    order.state = state
    order.close_price = close_price
    order.pnl_usd = pnl_usd
    order.commission_usd = commission_usd
    order.closed_at = closed_at


async def mark_order_cancelled(
    session: AsyncSession,
    order: Order,
    *,
    closed_at: datetime,
) -> None:
    order.state = OrderState.CANCELLED
    order.closed_at = closed_at
