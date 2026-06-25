"""Query helpers for the futures-bot DB layer.

All cross-table reads / atomic writes live here so the engine and the
Telegram handlers don't sprinkle SQL across the codebase. Mirrors the
crypto bot's ``src/db/repository.py`` style: every function takes an
:class:`AsyncSession` as its first argument and never commits — that
is the caller's job via ``session_scope``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------
# Stats aggregation
# ---------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class StatsSummary:
    """Aggregated per-block statistics for the Telegram stats view.

    All fields are immutable so they can be safely passed across the
    formatter boundary without anyone mutating them in-flight. The
    counts are integers; PnLs are USD (or whatever currency the
    account is denominated in) rounded to two decimal places.
    """

    total: int
    active: int
    wins: int
    losses: int
    invalid: int
    errored: int
    total_pnl: float
    win_rate: float | None     # None when no decisive (WIN/LOSS) blocks
    pnl_last_7d: float
    by_symbol_top: tuple[tuple[str, int, float], ...] = field(default_factory=tuple)


async def compute_stats(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    top_symbol_limit: int = 5,
) -> StatsSummary:
    """Aggregate block counts and PnL into a :class:`StatsSummary`.

    Done in Python rather than SQL because the table is intentionally
    small (a discretionary trader rarely hits triple-digit blocks/day),
    and Python-side aggregation is easier to read than three or four
    parallel SQL grouping clauses. If the table grows past low-tens-
    of-thousands of rows we can revisit.

    Args:
        session: open async session (read-only is fine).
        now: override "current time" for testability; defaults to UTC now.
        top_symbol_limit: how many symbols to surface in the breakdown.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    week_ago = now - timedelta(days=7)

    stmt = select(Block)
    result = await session.execute(stmt)
    blocks = list(result.scalars().all())

    total = len(blocks)
    active = sum(
        1 for b in blocks
        if b.status in (BlockStatus.CREATED, BlockStatus.ACTIVE)
    )
    wins = sum(1 for b in blocks if b.status == BlockStatus.WIN)
    losses = sum(1 for b in blocks if b.status == BlockStatus.LOSS)
    invalid = sum(1 for b in blocks if b.status == BlockStatus.INVALID)
    errored = sum(1 for b in blocks if b.status == BlockStatus.ERROR)

    # PnL totals only count terminal blocks — active blocks have no
    # net_pnl yet, and an in-flight unrealised number would mislead
    # the trader at a glance.
    total_pnl = 0.0
    pnl_last_7d = 0.0
    for b in blocks:
        if not b.is_terminal:
            continue
        net = b.net_pnl or 0.0
        total_pnl += net
        if b.closed_at is not None:
            closed_aware = b.closed_at
            if closed_aware.tzinfo is None:
                closed_aware = closed_aware.replace(tzinfo=timezone.utc)
            if closed_aware >= week_ago:
                pnl_last_7d += net

    decided = wins + losses
    win_rate = (wins / decided) if decided > 0 else None

    # By-symbol breakdown. Track both block count and PnL per symbol;
    # sort by count so the most-used pair surfaces first.
    by_symbol: dict[str, list[float]] = {}
    for b in blocks:
        agg = by_symbol.setdefault(b.symbol, [0, 0.0])
        agg[0] += 1                                  # block count
        if b.is_terminal:
            agg[1] += float(b.net_pnl or 0.0)        # cumulative pnl
    top = sorted(
        ((sym, int(c), float(p)) for sym, (c, p) in by_symbol.items()),
        key=lambda t: (t[1], abs(t[2])),
        reverse=True,
    )[:top_symbol_limit]

    return StatsSummary(
        total=total,
        active=active,
        wins=wins,
        losses=losses,
        invalid=invalid,
        errored=errored,
        total_pnl=round(total_pnl, 2),
        win_rate=round(win_rate, 4) if win_rate is not None else None,
        pnl_last_7d=round(pnl_last_7d, 2),
        by_symbol_top=tuple(top),
    )
