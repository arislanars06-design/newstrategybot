"""SQLAlchemy ORM models for the futures bot.

Schema mirrors the crypto bot's at a high level (Block / Order /
Event triple) but the column set is adapted to MT5 conventions:

* Orders carry a single MT5 ticket per stage (entry / SL / TP). MT5
  attaches SL/TP to a position rather than to separate orders, so
  ``sl_ticket`` / ``tp_ticket`` are usually the position ticket
  itself, repeated for symmetry with the crypto schema.
* Block stores ``zero_price``, ``hundred_price``, ``base_risk_usd``,
  and ``sl_distance`` so any plan can be re-derived deterministically
  from the persisted inputs.
* No ``win_order_seq``-style sentinel here; the same information is
  available via ``orders[*].state == TP_HIT``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from futures_bot.db.database import Base
from futures_bot.db.enums import BlockSide, BlockStatus, EventType, OrderState


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp.

    Centralised so tests can monkey-patch a single function.
    """
    return datetime.now(tz=timezone.utc)


class Block(Base):
    """A group of 6 chained limit orders that share a lifecycle."""

    __tablename__ = "blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- Identity ---
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[BlockSide] = mapped_column(String(8), nullable=False)
    status: Mapped[BlockStatus] = mapped_column(
        String(16), nullable=False, default=BlockStatus.CREATED, index=True
    )

    # --- Inputs (snapshot of what the trader supplied) ---
    zero_price: Mapped[float] = mapped_column(Float, nullable=False)
    hundred_price: Mapped[float] = mapped_column(Float, nullable=False)
    base_risk_usd: Mapped[float] = mapped_column(Float, nullable=False)
    cancel_price: Mapped[float] = mapped_column(Float, nullable=False)

    # --- Derived once at plan time, frozen on the block ---
    sl_distance: Mapped[float] = mapped_column(Float, nullable=False)

    # Cancel-price guard: becomes False as soon as the first rung
    # fills. After the first fill we are managing positions, not
    # waiting on a setup, so the cancel-price rule no longer applies.
    cancel_price_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    # Telegram chat that should receive notifications for this block.
    # BIG int so we can store negative channel/group IDs.
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Filled when the block reaches a terminal state.
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    orders: Mapped[list["Order"]] = relationship(
        "Order",
        back_populates="block",
        cascade="all, delete-orphan",
        order_by="Order.seq",
    )
    events: Mapped[list["Event"]] = relationship(
        "Event",
        back_populates="block",
        cascade="all, delete-orphan",
        order_by="Event.created_at",
    )

    __table_args__ = (
        Index("ix_blocks_status_symbol", "status", "symbol"),
    )

    @property
    def is_terminal(self) -> bool:
        """True if the block has reached a final state."""
        return self.status in {
            BlockStatus.WIN,
            BlockStatus.LOSS,
            BlockStatus.INVALID,
            BlockStatus.ERROR,
        }

    def __repr__(self) -> str:  # pragma: no cover — repr only
        return (
            f"<Block id={self.id} symbol={self.symbol} side={self.side} "
            f"status={self.status}>"
        )


class Order(Base):
    """One rung of a block: entry limit + position-level SL/TP."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    block_id: Mapped[int] = mapped_column(
        ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False, index=True
    )

    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..6

    # --- Plan (set at block creation) ---
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)

    # SL price at plan time = next rung's entry (chain rule).
    # Gets *adjusted* at fill time by ``spread × safety`` and stored
    # in ``sl_price_live``. We keep both so the audit log is complete.
    sl_price_plan: Mapped[float] = mapped_column(Float, nullable=False)
    sl_price_live: Mapped[float | None] = mapped_column(Float, nullable=True)

    # TP is computed at fill time from cumulative real risk; null
    # until the order opens.
    tp_price_live: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Lot computed at plan time with ROUND UP. Static for the
    # lifetime of the order; broker won't let us change it after a
    # fill without closing/reopening.
    lot: Mapped[float] = mapped_column(Float, nullable=False)

    # Planned vs realised risk in USD — useful for the preview and
    # for reporting after the fact.
    planned_risk_usd: Mapped[float] = mapped_column(Float, nullable=False)
    real_risk_usd: Mapped[float] = mapped_column(Float, nullable=False)

    # --- Broker handles ---
    # MT5 returns a 64-bit integer ticket for every order/position;
    # store as string for portability.
    entry_ticket: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    position_ticket: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    # Idempotency tag we attach to MT5 orders via ``magic`` /
    # ``comment``; format: "fb-blk{block_id}-s{seq}".
    client_tag: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    state: Mapped[OrderState] = mapped_column(
        String(16), nullable=False, default=OrderState.PENDING, index=True
    )

    # --- Execution data ---
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    filled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    block: Mapped[Block] = relationship("Block", back_populates="orders")

    __table_args__ = (
        UniqueConstraint("block_id", "seq", name="uq_orders_block_seq"),
        Index("ix_orders_block_state", "block_id", "state"),
    )

    def __repr__(self) -> str:  # pragma: no cover — repr only
        return (
            f"<Order block_id={self.block_id} seq={self.seq} "
            f"entry={self.entry_price} state={self.state}>"
        )


class Event(Base):
    """Append-only audit log for forensic debugging and stats."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    block_id: Mapped[int] = mapped_column(
        ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)

    event_type: Mapped[EventType] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    block: Mapped[Block] = relationship("Block", back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover — repr only
        return f"<Event block_id={self.block_id} type={self.event_type}>"
