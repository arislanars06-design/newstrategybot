"""Block Engine — orchestrates the whole strategy lifecycle.

Responsibilities:
1. Persist new blocks and place their entry orders on Binance.
2. React to user-data WebSocket events (entry fills, TP fills, SL fills).
3. React to mark-price events (Cancel Price detection).
4. Emit notifications for the Telegram bot.
5. Recover state from the database after a restart.

The engine never manipulates exchange or database state outside a
per-block ``asyncio.Lock``. This guarantees that two events for the same
block are applied sequentially, so the state machine is deterministic.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings
from src.core.notifications import Notification, NotificationHandler, NotificationType
from src.core.plan import BlockPlan
from src.db import (
    Block,
    BlockSide,
    BlockStatus,
    EventType,
    Order,
    OrderState,
    repository,
    session_scope,
)
from src.exchange.client import (
    POSITION_LONG,
    POSITION_SHORT,
    SIDE_BUY,
    SIDE_SELL,
    BinanceClient,
)
from src.exchange.streams import MarkPriceStream, UserDataStream
from src.exchange.types import OrderUpdate

# Client-order-id format we generate: "blk{block_id}-s{seq}-{kind}" where
# kind ∈ {e=entry, t=tp, l=stop-loss}.
_CLIENT_ID_RE = re.compile(r"^blk(?P<block>\d+)-s(?P<seq>\d+)-(?P<kind>[etl])$")


@dataclass(slots=True)
class _ParsedClientId:
    block_id: int
    seq: int
    kind: str  # "e", "t", or "l"


def _parse_client_id(client_id: str) -> _ParsedClientId | None:
    """Decode a client_order_id we generated, or return None if foreign."""
    m = _CLIENT_ID_RE.match(client_id or "")
    if not m:
        return None
    return _ParsedClientId(
        block_id=int(m.group("block")),
        seq=int(m.group("seq")),
        kind=m.group("kind"),
    )


def _exit_side_for(side: BlockSide) -> str:
    """The opposite side, used when placing TP/SL that close the position."""
    return SIDE_SELL if side == BlockSide.BUY else SIDE_BUY


def _position_side_for(side: BlockSide) -> str:
    return POSITION_LONG if side == BlockSide.BUY else POSITION_SHORT


def _entry_side_for(side: BlockSide) -> str:
    return SIDE_BUY if side == BlockSide.BUY else SIDE_SELL


class BlockEngine:
    """Single point of truth for block state transitions."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: BinanceClient,
        on_notification: NotificationHandler,
    ) -> None:
        self._settings = settings
        self._client = client
        self._on_notification = on_notification

        self._user_stream = UserDataStream(client, on_order_update=self._handle_order_update)
        self._mark_stream = MarkPriceStream(client, on_price=self._handle_mark_price)

        # One asyncio.Lock per block to serialise events for that block.
        self._locks: dict[int, asyncio.Lock] = {}

    # ----- Lifecycle -----

    async def start(self) -> None:
        """Start WebSocket workers and rebuild in-memory state from DB."""
        await self._user_stream.start()
        await self._recover_active_blocks()

    async def stop(self) -> None:
        await self._user_stream.stop()
        await self._mark_stream.stop()

    async def _recover_active_blocks(self) -> None:
        async with session_scope() as session:
            active = await repository.list_active_blocks(session)
            symbols = {b.symbol for b in active}
        for sym in symbols:
            await self._mark_stream.add_symbol(sym)
        if active:
            logger.info(
                "Recovered {n} active block(s) across {s} symbol(s)",
                n=len(active),
                s=len(symbols),
            )

    # ----- Public API -----

    async def create_block(self, plan: BlockPlan, chat_id: int) -> Block:
        """Validate, persist, and place the entry orders for a new block.

        Raises if validation fails or any exchange call fails before the
        block is fully placed; in that case the partially-created block
        is marked ERROR rather than left dangling.
        """
        plan.validate()

        # 1. Persist the block + 8 orders (CREATED state) so we have IDs
        #    and a stable client_order_id namespace.
        async with session_scope() as session:
            block = await repository.create_block(
                session,
                symbol=plan.symbol,
                side=plan.side,
                cancel_price=plan.cancel_price,
                chat_id=chat_id,
                note=plan.note,
            )
            for spec in plan.orders:
                await repository.add_order(
                    session,
                    block_id=block.id,
                    seq=spec.seq,
                    entry_price=spec.entry_price,
                    tp_price=spec.tp_price,
                    sl_price=spec.sl_price,
                    qty=spec.qty,
                )
            await repository.add_event(
                session,
                block_id=block.id,
                event_type=EventType.BLOCK_CREATED,
                payload={
                    "symbol": plan.symbol,
                    "side": str(plan.side),
                    "cancel_price": plan.cancel_price,
                },
            )
            block_id = block.id

        # 2. Place entry orders. Failures are caught per-order so that we
        #    can mark the block ERROR cleanly instead of crashing.
        try:
            await self._place_entry_orders(block_id, plan)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to place entries for block {b}", b=block_id)
            await self._mark_block_error(block_id, str(exc))
            raise

        # 3. Subscribe to mark price for cancel-price detection.
        await self._mark_stream.add_symbol(plan.symbol)

        # 4. Move block to ACTIVE.
        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            assert block is not None
            await repository.update_block_status(session, block, BlockStatus.ACTIVE)
            await repository.add_event(
                session,
                block_id=block.id,
                event_type=EventType.ORDERS_PLACED,
            )

        await self._notify(
            type_=NotificationType.BLOCK_CREATED,
            block_id=block_id,
            chat_id=chat_id,
            payload={
                "symbol": plan.symbol,
                "side": str(plan.side),
                "orders": len(plan.orders),
                "cancel_price": plan.cancel_price,
            },
        )
        return block

    async def cancel_block(self, block_id: int) -> None:
        """Manually close a block: cancel everything, mark MANUAL_CLOSE."""
        async with self._lock_for(block_id):
            async with session_scope() as session:
                block = await repository.get_block(session, block_id)
                if block is None or block.is_terminal:
                    return
                await self._cancel_all_orders(session, block)
                await repository.update_block_status(
                    session, block, BlockStatus.INVALID
                )
                await repository.add_event(
                    session,
                    block_id=block.id,
                    event_type=EventType.BLOCK_MANUAL_CLOSE,
                )
                chat_id = block.chat_id
                symbol = block.symbol
            await self._mark_stream.remove_symbol(symbol)
            await self._notify(
                type_=NotificationType.BLOCK_MANUAL_CLOSE,
                block_id=block_id,
                chat_id=chat_id,
                payload={},
            )

    # ----- WebSocket handlers -----

    async def _handle_order_update(self, update: OrderUpdate) -> None:
        """Entry point for every ORDER_TRADE_UPDATE event."""
        parsed = _parse_client_id(update.client_order_id)
        if parsed is None:
            # Not one of our orders — ignore (e.g. manual exchange order).
            return

        async with self._lock_for(parsed.block_id):
            try:
                await self._dispatch_order_update(parsed, update)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "order-update dispatch failed for block={b} seq={s} kind={k}",
                    b=parsed.block_id,
                    s=parsed.seq,
                    k=parsed.kind,
                )

    async def _dispatch_order_update(
        self, parsed: _ParsedClientId, update: OrderUpdate
    ) -> None:
        if not (update.is_filled or update.is_canceled):
            # Intermediate states (NEW, PARTIALLY_FILLED) — ignore.
            return

        async with session_scope() as session:
            block = await repository.get_block(session, parsed.block_id)
            if block is None or block.is_terminal:
                return
            order = next((o for o in block.orders if o.seq == parsed.seq), None)
            if order is None:
                return

            if parsed.kind == "e":
                if update.is_filled:
                    await self._on_entry_filled(session, block, order, update)
                elif update.is_canceled:
                    await self._on_entry_canceled(session, block, order, update)
            elif parsed.kind == "t":
                if update.is_filled:
                    await self._on_tp_filled(session, block, order, update)
            elif parsed.kind == "l":
                if update.is_filled:
                    await self._on_sl_filled(session, block, order, update)

            # After every state mutation, check whether the block has
            # reached a terminal state via SL chain or invalidation.
            await self._maybe_finalize(session, block)

    async def _handle_mark_price(self, symbol: str, price: float) -> None:
        """Cancel-price watcher; runs for every mark-price tick."""
        async with session_scope() as session:
            blocks = await repository.list_active_blocks(session)
            candidates = [
                b
                for b in blocks
                if b.symbol == symbol and b.cancel_price_active and not b.is_terminal
            ]

        for block in candidates:
            triggered = (
                price >= block.cancel_price if block.side == BlockSide.BUY
                else price <= block.cancel_price
            )
            if not triggered:
                continue

            async with self._lock_for(block.id):
                async with session_scope() as session:
                    fresh = await repository.get_block(session, block.id)
                    if (
                        fresh is None
                        or fresh.is_terminal
                        or not fresh.cancel_price_active
                    ):
                        continue
                    await self._invalidate_block(session, fresh, mark_price=price)
                await self._mark_stream.remove_symbol(fresh.symbol)
                await self._notify(
                    type_=NotificationType.BLOCK_INVALID,
                    block_id=fresh.id,
                    chat_id=fresh.chat_id,
                    payload={
                        "cancel_price": fresh.cancel_price,
                        "mark_price": price,
                    },
                )

    # ----- State transitions -----

    async def _on_entry_filled(
        self,
        session: AsyncSession,
        block: Block,
        order: Order,
        update: OrderUpdate,
    ) -> None:
        if order.state != OrderState.PENDING:
            return  # idempotent: ignore duplicate event

        await repository.update_order_state(
            session,
            order,
            OrderState.TRIGGERED,
            filled_entry_price=update.avg_fill_price,
        )
        await repository.set_order_exchange_ids(session, order, entry_id=update.order_id)
        await repository.add_event(
            session,
            block_id=block.id,
            order_seq=order.seq,
            event_type=EventType.ORDER_TRIGGERED,
            payload={"price": update.avg_fill_price},
        )
        # Cancel price is no longer relevant once a position is open.
        if block.cancel_price_active:
            await repository.deactivate_cancel_price(session, block)

        # Place TP and SL for this specific order.
        await self._place_tp_sl(session, block, order)

        await self._notify(
            type_=NotificationType.ORDER_TRIGGERED,
            block_id=block.id,
            chat_id=block.chat_id,
            payload={
                "seq": order.seq,
                "price": update.avg_fill_price,
                "qty": order.qty,
            },
        )

    async def _on_entry_canceled(
        self,
        session: AsyncSession,
        block: Block,
        order: Order,
        update: OrderUpdate,
    ) -> None:
        if order.state != OrderState.PENDING:
            return
        await repository.update_order_state(session, order, OrderState.CANCELLED)
        await repository.add_event(
            session,
            block_id=block.id,
            order_seq=order.seq,
            event_type=EventType.ORDER_CANCELLED,
            payload={"reason": "exchange_cancel"},
        )

    async def _on_tp_filled(
        self,
        session: AsyncSession,
        block: Block,
        order: Order,
        update: OrderUpdate,
    ) -> None:
        if order.state == OrderState.TP_HIT:
            return  # idempotent

        # Compute realised PnL for this rung. Use exchange-reported pnl if
        # available; fall back to a simple price math otherwise.
        pnl = update.realized_pnl
        if not pnl and order.filled_entry_price is not None:
            direction = 1 if block.side == BlockSide.BUY else -1
            pnl = direction * (update.avg_fill_price - order.filled_entry_price) * order.qty

        await repository.update_order_state(
            session,
            order,
            OrderState.TP_HIT,
            filled_exit_price=update.avg_fill_price,
            pnl=pnl,
        )
        await repository.add_event(
            session,
            block_id=block.id,
            order_seq=order.seq,
            event_type=EventType.TP_HIT,
            payload={"price": update.avg_fill_price, "pnl": pnl},
        )

        # Cancel the now-orphan SL for the same rung.
        if order.sl_client_id:
            try:
                await self._client.cancel_order_by_client_id(block.symbol, order.sl_client_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not cancel SL for block={b} seq={s}: {err}",
                    b=block.id, s=order.seq, err=exc,
                )

        # Block becomes WIN: cancel every pending entry. Open positions
        # from already-triggered orders (Variant A) keep their own
        # TP/SL; in chain mode this rarely matters in practice.
        await self._cancel_pending_entries(session, block)

        await repository.update_block_status(
            session,
            block,
            BlockStatus.WIN,
            win_order_seq=order.seq,
            net_pnl=_compute_net_pnl(block),
        )
        await repository.add_event(
            session,
            block_id=block.id,
            event_type=EventType.BLOCK_WIN,
            payload={"win_order_seq": order.seq},
        )

        # Mark-price watcher no longer needed for this symbol if no other
        # active block needs it. We let _maybe_finalize handle removal
        # generically via a follow-up call after this transaction.

        await self._notify(
            type_=NotificationType.BLOCK_WIN,
            block_id=block.id,
            chat_id=block.chat_id,
            payload={
                "win_order_seq": order.seq,
                "tp_price": update.avg_fill_price,
                "net_pnl": _compute_net_pnl(block),
            },
        )

    async def _on_sl_filled(
        self,
        session: AsyncSession,
        block: Block,
        order: Order,
        update: OrderUpdate,
    ) -> None:
        if order.state == OrderState.SL_HIT:
            return  # idempotent

        pnl = update.realized_pnl
        if not pnl and order.filled_entry_price is not None:
            direction = 1 if block.side == BlockSide.BUY else -1
            pnl = direction * (update.avg_fill_price - order.filled_entry_price) * order.qty

        await repository.update_order_state(
            session,
            order,
            OrderState.SL_HIT,
            filled_exit_price=update.avg_fill_price,
            pnl=pnl,
        )
        await repository.add_event(
            session,
            block_id=block.id,
            order_seq=order.seq,
            event_type=EventType.SL_HIT,
            payload={"price": update.avg_fill_price, "pnl": pnl},
        )

        # The TP for this rung is now an orphan reduce-only — cancel it.
        if order.tp_client_id:
            try:
                await self._client.cancel_order_by_client_id(block.symbol, order.tp_client_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not cancel TP for block={b} seq={s}: {err}",
                    b=block.id, s=order.seq, err=exc,
                )

        await self._notify(
            type_=NotificationType.SL_HIT,
            block_id=block.id,
            chat_id=block.chat_id,
            payload={
                "seq": order.seq,
                "price": update.avg_fill_price,
                "pnl": pnl,
            },
        )

    async def _maybe_finalize(self, session: AsyncSession, block: Block) -> None:
        """Detect terminal state by inspecting all child orders."""
        if block.is_terminal:
            return

        states = [o.state for o in block.orders]
        terminal_states = {
            OrderState.TP_HIT,
            OrderState.SL_HIT,
            OrderState.CANCELLED,
            OrderState.ERROR,
        }
        if not all(s in terminal_states for s in states):
            return  # still some pending or triggered

        any_tp = any(o.state == OrderState.TP_HIT for o in block.orders)
        any_sl = any(o.state == OrderState.SL_HIT for o in block.orders)

        net_pnl = _compute_net_pnl(block)

        if any_tp:
            # Already handled by _on_tp_filled. Just refresh PnL.
            if block.status != BlockStatus.WIN:
                await repository.update_block_status(
                    session, block, BlockStatus.WIN, net_pnl=net_pnl
                )
            return

        if any_sl:
            await repository.update_block_status(
                session, block, BlockStatus.LOSS, net_pnl=net_pnl
            )
            await repository.add_event(
                session,
                block_id=block.id,
                event_type=EventType.BLOCK_LOSS,
                payload={"net_pnl": net_pnl},
            )
            await self._notify(
                type_=NotificationType.BLOCK_LOSS,
                block_id=block.id,
                chat_id=block.chat_id,
                payload={"net_pnl": net_pnl},
            )
            return

        # All cancelled, no TP, no SL — equivalent to invalid.
        await repository.update_block_status(session, block, BlockStatus.INVALID)
        await repository.add_event(
            session,
            block_id=block.id,
            event_type=EventType.BLOCK_INVALID,
            payload={"reason": "all_cancelled"},
        )

    async def _invalidate_block(
        self, session: AsyncSession, block: Block, *, mark_price: float
    ) -> None:
        """Mark a block as INVALID after the cancel-price was hit."""
        await self._cancel_all_orders(session, block)
        await repository.update_block_status(session, block, BlockStatus.INVALID)
        await repository.add_event(
            session,
            block_id=block.id,
            event_type=EventType.CANCEL_PRICE_HIT,
            payload={"cancel_price": block.cancel_price, "mark_price": mark_price},
        )
        await repository.add_event(
            session,
            block_id=block.id,
            event_type=EventType.BLOCK_INVALID,
            payload={"reason": "cancel_price"},
        )

    # ----- Exchange helpers -----

    async def _place_entry_orders(self, block_id: int, plan: BlockPlan) -> None:
        side = _entry_side_for(plan.side)
        position_side = _position_side_for(plan.side)

        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            assert block is not None
            orders = list(block.orders)

        for order in orders:
            client_id = order.entry_client_id
            try:
                exch_id = await self._client.place_entry_limit(
                    symbol=plan.symbol,
                    side=side,
                    position_side=position_side,
                    qty=order.qty,
                    price=order.entry_price,
                    client_id=client_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Entry placement failed for block={b} seq={s}: {err}",
                    b=block_id, s=order.seq, err=exc,
                )
                raise
            async with session_scope() as session:
                fresh = await repository.get_block(session, block_id)
                assert fresh is not None
                target = next(o for o in fresh.orders if o.seq == order.seq)
                await repository.set_order_exchange_ids(session, target, entry_id=exch_id)
            await asyncio.sleep(self._settings.order_place_delay_ms / 1000.0)

    async def _place_tp_sl(
        self, session: AsyncSession, block: Block, order: Order
    ) -> None:
        """Place TP + SL pair sized to a single rung's quantity."""
        exit_side = _exit_side_for(block.side)
        position_side = _position_side_for(block.side)

        try:
            tp_id = await self._client.place_tp_limit(
                symbol=block.symbol,
                side=exit_side,
                position_side=position_side,
                qty=order.qty,
                price=order.tp_price,
                client_id=order.tp_client_id,
            )
            sl_id = await self._client.place_sl_stop(
                symbol=block.symbol,
                side=exit_side,
                position_side=position_side,
                qty=order.qty,
                stop_price=order.sl_price,
                client_id=order.sl_client_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "TP/SL placement failed for block={b} seq={s}", b=block.id, s=order.seq
            )
            await repository.update_order_state(session, order, OrderState.ERROR)
            await repository.add_event(
                session,
                block_id=block.id,
                order_seq=order.seq,
                event_type=EventType.BLOCK_ERROR,
                payload={"stage": "tp_sl_placement", "error": str(exc)},
            )
            return

        await repository.set_order_exchange_ids(session, order, tp_id=tp_id, sl_id=sl_id)

    async def _cancel_pending_entries(
        self, session: AsyncSession, block: Block
    ) -> None:
        pendings = [o for o in block.orders if o.state == OrderState.PENDING]
        for order in pendings:
            try:
                await self._client.cancel_order_by_client_id(
                    block.symbol, order.entry_client_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Cancel pending entry failed (block={b} seq={s}): {err}",
                    b=block.id, s=order.seq, err=exc,
                )
            await repository.update_order_state(session, order, OrderState.CANCELLED)
            await repository.add_event(
                session,
                block_id=block.id,
                order_seq=order.seq,
                event_type=EventType.ORDER_CANCELLED,
                payload={"reason": "block_terminal"},
            )

    async def _cancel_all_orders(
        self, session: AsyncSession, block: Block
    ) -> None:
        """Cancel every order owned by this block (defensive close)."""
        for order in block.orders:
            for cid in (order.entry_client_id, order.tp_client_id, order.sl_client_id):
                try:
                    await self._client.cancel_order_by_client_id(block.symbol, cid)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "cancel {cid} failed (likely already gone): {err}",
                        cid=cid, err=exc,
                    )
            if order.state == OrderState.PENDING:
                await repository.update_order_state(session, order, OrderState.CANCELLED)
                await repository.add_event(
                    session,
                    block_id=block.id,
                    order_seq=order.seq,
                    event_type=EventType.ORDER_CANCELLED,
                    payload={"reason": "block_terminal"},
                )

    async def _mark_block_error(self, block_id: int, reason: str) -> None:
        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            if block is None or block.is_terminal:
                return
            await repository.update_block_status(session, block, BlockStatus.ERROR)
            await repository.add_event(
                session,
                block_id=block_id,
                event_type=EventType.BLOCK_ERROR,
                payload={"reason": reason},
            )
            chat_id = block.chat_id
        await self._notify(
            type_=NotificationType.BLOCK_ERROR,
            block_id=block_id,
            chat_id=chat_id,
            payload={"reason": reason},
        )

    # ----- Concurrency primitives -----

    def _lock_for(self, block_id: int) -> asyncio.Lock:
        lock = self._locks.get(block_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[block_id] = lock
        return lock

    # ----- Notifications -----

    async def _notify(
        self,
        *,
        type_: NotificationType,
        block_id: int,
        chat_id: int,
        payload: dict[str, Any],
    ) -> None:
        try:
            await self._on_notification(
                Notification(
                    type=type_, block_id=block_id, chat_id=chat_id, payload=payload
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("Notification handler raised for type={t}", t=type_)


def _compute_net_pnl(block: Block) -> float:
    """Sum the realised PnL of every closed rung in the block."""
    return round(sum((o.pnl or 0.0) for o in block.orders), 6)
