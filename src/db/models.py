"""SQLAlchemy ORM models for blocks, orders, and audit events."""

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

from src.db.database import Base
from src.db.enums import BlockSide, BlockStatus, EventType, OrderState


def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Centralised so tests can monkey-patch a single function.
    """
    return datetime.now(tz=timezone.utc)


class Block(Base):
    """A group of 6 chained limit orders that share lifecycle and a Cancel Price."""

    __tablename__ = "blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[BlockSide] = mapped_column(String(8), nullable=False)
    status: Mapped[BlockStatus] = mapped_column(
        String(16), nullable=False, default=BlockStatus.CREATED, index=True
    )

    cancel_price: Mapped[float] = mapped_column(Float, nullable=False)
    # Becomes False as soon as the first order is triggered. Reduces work
    # for the price watcher and prevents the cancel-price rule from firing
    # once a position has been opened (per design decision).
    cancel_price_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Telegram chat that should receive notifications for this block.
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # True when the bot placed the orders itself (/newblock); False when
    # the orders were placed by the user manually and the bot only tracks
    # them (/track). Affects how we cancel orders later (by client_id vs
    # by exchange order_id).
    is_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Optional human-readable note from the user.
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # If WIN: which order (seq 1..8) reached TP. NULL otherwise.
    win_order_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Realised + unrealised P&L in quote asset (USDT). Populated when the
    # block reaches a terminal state and all positions are closed.
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    orders: Mapped[list[Order]] = relationship(
        "Order",
        back_populates="block",
        cascade="all, delete-orphan",
        order_by="Order.seq",
    )
    events: Mapped[list[Event]] = relationship(
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

    def __repr__(self) -> str:
        return (
            f"<Block id={self.id} symbol={self.symbol} side={self.side} "
            f"status={self.status}>"
        )


class Order(Base):
    """A single rung in the block ladder: an entry limit plus its TP and SL."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    block_id: Mapped[int] = mapped_column(
        ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Position within the chain (1..8). Order N's SL price equals order N+1's
    # entry price by design, ensuring at most one position is open at a time.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- Plan ---
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    tp_price: Mapped[float] = mapped_column(Float, nullable=False)
    sl_price: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)

    # --- Exchange identifiers ---
    # Binance order IDs (assigned by the exchange after a successful POST).
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tp_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sl_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Client order IDs we generate for idempotency. Format: "blk{block_id}-s{seq}-{kind}".
    entry_client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    tp_client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    sl_client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    state: Mapped[OrderState] = mapped_column(
        String(16), nullable=False, default=OrderState.PENDING, index=True
    )

    # --- Execution data ---
    filled_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    block: Mapped[Block] = relationship("Block", back_populates="orders")

    __table_args__ = (
        UniqueConstraint("block_id", "seq", name="uq_orders_block_seq"),
        Index("ix_orders_block_state", "block_id", "state"),
    )

    def __repr__(self) -> str:
        return (
            f"<Order block_id={self.block_id} seq={self.seq} "
            f"entry={self.entry_price} state={self.state}>"
        )


class Event(Base):
    """Append-only audit log. Useful for forensic debugging and stats."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    block_id: Mapped[int] = mapped_column(
        ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Optional pointer to a specific order within the block.
    order_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)

    event_type: Mapped[EventType] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    block: Mapped[Block] = relationship("Block", back_populates="events")

    def __repr__(self) -> str:
        return f"<Event block_id={self.block_id} type={self.event_type}>"
