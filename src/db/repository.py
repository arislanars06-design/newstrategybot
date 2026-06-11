"""Repository functions: focused, side-effect-only helpers around the ORM.

Functions take an ``AsyncSession`` and never commit themselves — the
caller is responsible for the transaction boundary (typically via the
``session_scope`` context manager). This keeps higher-level workflows
atomic across multiple operations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.enums import BlockSide, BlockStatus, EventType, OrderState
from src.db.models import Block, Event, Order


# =============================================================================
# Block CRUD
# =============================================================================


async def create_block(
    session: AsyncSession,
    *,
    symbol: str,
    side: BlockSide,
    cancel_price: float,
    chat_id: int,
    note: str | None = None,
) -> Block:
    """Create a new Block in CREATED status with no orders attached yet."""
    block = Block(
        symbol=symbol,
        side=side,
        status=BlockStatus.CREATED,
        cancel_price=cancel_price,
        cancel_price_active=True,
        chat_id=chat_id,
        note=note,
    )
    session.add(block)
    await session.flush()  # populate block.id
    return block


async def get_block(session: AsyncSession, block_id: int) -> Block | None:
    """Load a block with its orders eagerly fetched."""
    stmt = (
        select(Block)
        .where(Block.id == block_id)
        .options(selectinload(Block.orders))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_active_blocks(session: AsyncSession) -> Sequence[Block]:
    """Return all blocks in non-terminal states (CREATED or ACTIVE)."""
    stmt = (
        select(Block)
        .where(Block.status.in_([BlockStatus.CREATED, BlockStatus.ACTIVE]))
        .options(selectinload(Block.orders))
        .order_by(Block.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_blocks(
    session: AsyncSession,
    *,
    limit: int = 50,
    statuses: Sequence[BlockStatus] | None = None,
) -> Sequence[Block]:
    """Return blocks filtered by optional status, newest first."""
    stmt = select(Block).options(selectinload(Block.orders))
    if statuses:
        stmt = stmt.where(Block.status.in_(list(statuses)))
    stmt = stmt.order_by(Block.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def update_block_status(
    session: AsyncSession,
    block: Block,
    status: BlockStatus,
    *,
    win_order_seq: int | None = None,
    net_pnl: float | None = None,
) -> None:
    """Move a block to a new status and stamp closed_at if terminal."""
    block.status = status
    if win_order_seq is not None:
        block.win_order_seq = win_order_seq
    if net_pnl is not None:
        block.net_pnl = net_pnl
    if block.is_terminal and block.closed_at is None:
        block.closed_at = datetime.now(tz=timezone.utc)


async def deactivate_cancel_price(session: AsyncSession, block: Block) -> None:
    """Disable the cancel-price watcher for this block.

    Called the first time any order in the block triggers, since the
    cancel-price rule explicitly only applies before any fill.
    """
    block.cancel_price_active = False


# =============================================================================
# Order CRUD
# =============================================================================


async def add_order(
    session: AsyncSession,
    *,
    block_id: int,
    seq: int,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    qty: float,
) -> Order:
    """Attach a new order row to a block. State starts as PENDING."""
    order = Order(
        block_id=block_id,
        seq=seq,
        entry_price=entry_price,
        tp_price=tp_price,
        sl_price=sl_price,
        qty=qty,
        entry_client_id=f"blk{block_id}-s{seq}-e",
        tp_client_id=f"blk{block_id}-s{seq}-t",
        sl_client_id=f"blk{block_id}-s{seq}-l",
    )
    session.add(order)
    await session.flush()
    return order


async def get_order_by_client_id(
    session: AsyncSession, client_id: str
) -> Order | None:
    """Look up an order by any of its three client IDs.

    Used by the WebSocket handler to map an exchange event back to one of
    our orders.
    """
    stmt = select(Order).where(
        (Order.entry_client_id == client_id)
        | (Order.tp_client_id == client_id)
        | (Order.sl_client_id == client_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_pending_orders_for_block(
    session: AsyncSession, block_id: int
) -> Sequence[Order]:
    """Return orders still in PENDING state (entry not yet filled)."""
    stmt = (
        select(Order)
        .where(Order.block_id == block_id)
        .where(Order.state == OrderState.PENDING)
        .order_by(Order.seq)
    )
    return list((await session.execute(stmt)).scalars().all())


async def update_order_state(
    session: AsyncSession,
    order: Order,
    state: OrderState,
    *,
    filled_entry_price: float | None = None,
    filled_exit_price: float | None = None,
    pnl: float | None = None,
) -> None:
    """Mutate the order's state and execution fields, stamping timestamps."""
    now = datetime.now(tz=timezone.utc)
    order.state = state
    if filled_entry_price is not None:
        order.filled_entry_price = filled_entry_price
        if order.triggered_at is None:
            order.triggered_at = now
    if filled_exit_price is not None:
        order.filled_exit_price = filled_exit_price
    if pnl is not None:
        order.pnl = pnl
    if state in {
        OrderState.TP_HIT,
        OrderState.SL_HIT,
        OrderState.CANCELLED,
        OrderState.ERROR,
    }:
        order.closed_at = now


async def set_order_exchange_ids(
    session: AsyncSession,
    order: Order,
    *,
    entry_id: str | None = None,
    tp_id: str | None = None,
    sl_id: str | None = None,
) -> None:
    """Persist exchange-assigned order IDs once they are returned by Binance."""
    if entry_id is not None:
        order.entry_order_id = entry_id
    if tp_id is not None:
        order.tp_order_id = tp_id
    if sl_id is not None:
        order.sl_order_id = sl_id


# =============================================================================
# Events
# =============================================================================


async def add_event(
    session: AsyncSession,
    *,
    block_id: int,
    event_type: EventType,
    order_seq: int | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    """Append an event to the audit log."""
    event = Event(
        block_id=block_id,
        order_seq=order_seq,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    return event


# =============================================================================
# Aggregates / stats
# =============================================================================


async def aggregate_stats(session: AsyncSession) -> dict[str, Any]:
    """Compute simple aggregate statistics across all closed blocks."""
    stmt = select(Block).where(
        Block.status.in_(
            [BlockStatus.WIN, BlockStatus.LOSS, BlockStatus.INVALID, BlockStatus.ERROR]
        )
    )
    closed_blocks = list((await session.execute(stmt)).scalars().all())
    total = len(closed_blocks)
    wins = sum(1 for b in closed_blocks if b.status == BlockStatus.WIN)
    losses = sum(1 for b in closed_blocks if b.status == BlockStatus.LOSS)
    invalids = sum(1 for b in closed_blocks if b.status == BlockStatus.INVALID)
    errors = sum(1 for b in closed_blocks if b.status == BlockStatus.ERROR)
    net_pnl = sum((b.net_pnl or 0.0) for b in closed_blocks)

    win_rate = (wins / total * 100.0) if total else 0.0
    return {
        "total_closed": total,
        "wins": wins,
        "losses": losses,
        "invalids": invalids,
        "errors": errors,
        "win_rate_pct": round(win_rate, 2),
        "net_pnl": round(net_pnl, 4),
    }
