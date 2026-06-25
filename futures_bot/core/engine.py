"""Block lifecycle engine.

Coordinates the strategy module, the broker adapter, the database,
and the notification stream. The Telegram bot layer talks to this
engine through a small public API; the engine talks to MT5 only
through :class:`BrokerAdapter` so tests can run end-to-end against
:class:`MockAdapter`.

State machine for a single block:

    CREATED ──place_orders──► ACTIVE
       │                        │
       │ cancel_price hit       ├──first fill──► position open
       └──INVALID                │
                                 ├──any TP hit──► WIN
                                 ├──all SLs hit──► LOSS
                                 └──/cancel───────► MANUAL_CLOSE → WIN/LOSS based on PnL

Calling code uses three primary entry points:

* :meth:`create_block` — persist the plan, place the six limit
  orders, and start watching them.
* :meth:`on_tick`     — fed by the order-watcher; checks for fills,
  TP/SL hits, and cancel-price events on a single symbol.
* :meth:`cancel_block` — manual close: cancel pending orders, close
  any open positions, mark terminal.

This first cut keeps everything in one file so the test suite has
one place to mock. We can split it later if it grows past ~600 lines.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from futures_bot.adapters.base import (
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    SymbolInfo,
    Tick,
)
from futures_bot.config import Settings
from futures_bot.core import notifications as notify
from futures_bot.db import repository, session_scope
from futures_bot.db.enums import (
    BlockSide,
    BlockStatus,
    EventType,
    OrderState,
)
from futures_bot.db.models import Block, Order
from futures_bot.strategy.fib import GAP_PCT
from futures_bot.strategy.plan import BlockPlan, PlanRung
from futures_bot.strategy.risk import SymbolSpec, cumulative_risks, loss_per_lot
from futures_bot.strategy.tp import (
    FillContext,
    compute_sl_price,
    compute_tp_price,
    usd_per_price_unit_from_lot,
)


# Type alias for the notification sink. The engine calls
# ``await on_notification(Notification)`` from inside its async methods;
# the bot wires this up at startup with :class:`TelegramNotifier`.
NotificationCallback = Callable[[notify.Notification], Awaitable[None]]


@dataclass(slots=True)
class _PlacedOrder:
    """Pairs a plan rung with the broker ticket it created."""

    rung: PlanRung
    ticket: str
    client_tag: str


class BlockEngine:
    """Orchestrates the futures block strategy.

    Thread-safety: the engine assumes a single asyncio event loop. All
    DB writes happen inside ``async with session_scope()`` blocks so
    each invariant transition is atomic. The adapter takes care of
    serialising MT5 requests internally.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        adapter: BrokerAdapter,
        on_notification: NotificationCallback | None = None,
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._on_notification: NotificationCallback | None = on_notification

    # ===================================================================
    # Public API: block creation
    # ===================================================================

    async def create_block(
        self,
        plan: BlockPlan,
        *,
        chat_id: int,
    ) -> Block:
        """Persist the plan, place the six limit orders, return the block.

        Failure semantics:
        * If persistence fails — we raise; nothing was sent to the
          broker yet so there is nothing to undo.
        * If broker placement fails partway — we cancel whatever was
          placed, mark the block ``ERROR``, and re-raise so the
          Telegram handler can surface the error to the trader.

        The engine never logs in to MT5 inside this method; the caller
        must ensure ``adapter.connect()`` has succeeded before getting
        here.
        """
        if not await self._adapter.is_connected():
            raise RuntimeError("broker adapter is not connected")

        # 1. Insert the Block + 6 Orders rows in one transaction.
        async with session_scope() as session:
            block = Block(
                symbol=plan.symbol,
                side=plan.side,
                status=BlockStatus.CREATED,
                zero_price=plan.zero_price,
                hundred_price=plan.hundred_price,
                base_risk_usd=plan.base_risk_usd,
                # Store ``zero_price`` as a placeholder when the
                # trader opted out of the cancel-price guard. The
                # column is NOT NULL in the DB; we'd add a schema
                # migration to make it nullable, but the engine
                # respects ``cancel_price_active`` as the on/off
                # switch so a placeholder value is harmless.
                cancel_price=(
                    plan.cancel_price
                    if plan.cancel_price is not None
                    else plan.zero_price
                ),
                sl_distance=plan.sl_distance,
                # The guard starts active only when the trader chose
                # a cancel price; otherwise it's off from creation.
                cancel_price_active=plan.cancel_price is not None,
                chat_id=chat_id,
                note=plan.note,
            )
            session.add(block)
            await session.flush()  # populate block.id

            for rung in plan.rungs:
                tag = _make_client_tag(block.id, rung.seq)
                order = Order(
                    block_id=block.id,
                    seq=rung.seq,
                    entry_price=rung.entry,
                    sl_price_plan=rung.sl,
                    lot=rung.lot,
                    planned_risk_usd=rung.planned_risk_usd,
                    real_risk_usd=rung.real_risk_usd,
                    client_tag=tag,
                    state=OrderState.PENDING,
                )
                session.add(order)

            await repository.append_event(
                session,
                block_id=block.id,
                event_type=EventType.BLOCK_CREATED,
                payload={
                    "symbol": plan.symbol,
                    "side": str(plan.side),
                    "base_risk_usd": plan.base_risk_usd,
                    "total_planned_risk": plan.total_planned_risk(),
                    "total_real_risk": plan.total_real_risk(),
                    "cancel_price": plan.cancel_price,
                },
            )
            block_id = block.id

        # 2. Place the limit orders on the broker. This is OUTSIDE the
        #    DB transaction so a slow broker doesn't hold a write lock.
        placements: list[_PlacedOrder] = []
        try:
            for rung in plan.rungs:
                tag = _make_client_tag(block_id, rung.seq)
                req = OrderRequest(
                    symbol=plan.symbol,
                    side=plan.side,
                    order_type="LIMIT",
                    price=rung.entry,
                    lot=rung.lot,
                    # SL/TP attached AFTER fill (see on_tick). Some
                    # brokers prefer SL on the limit itself, but for
                    # MT5 + our chain rule we want spread-aware SL
                    # which only makes sense after we know the fill
                    # spread.
                    sl=None,
                    tp=None,
                    client_tag=tag,
                    magic=block_id,
                )
                res = await self._adapter.place_order(req)
                if not res.ok:
                    raise RuntimeError(
                        f"place_order failed for rung {rung.seq}: "
                        f"{res.error_code} {res.error_message}"
                    )
                placements.append(
                    _PlacedOrder(rung=rung, ticket=res.ticket or "", client_tag=tag)
                )
        except Exception:
            # Roll back any placements that succeeded before the failure.
            await self._rollback_placements(placements)
            await self._mark_block_error(block_id, reason="placement failed")
            raise

        # 3. Update the DB with the broker tickets and mark ACTIVE.
        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            assert block is not None
            ticket_by_seq = {p.rung.seq: p.ticket for p in placements}
            for o in block.orders:
                o.entry_ticket = ticket_by_seq[o.seq]
            block.status = BlockStatus.ACTIVE
            await repository.append_event(
                session,
                block_id=block.id,
                event_type=EventType.ORDERS_PLACED,
                payload={"tickets": ticket_by_seq},
            )

        await self._notify(
            notify.block_created(
                block_id=block_id,
                chat_id=chat_id,
                symbol=plan.symbol,
                side=str(plan.side),
                orders=len(plan.rungs),
                cancel_price=plan.cancel_price,
                base_risk=plan.base_risk_usd,
                total_real_risk=plan.total_real_risk(),
            )
        )

        async with session_scope() as session:
            return await repository.get_block(session, block_id)  # type: ignore[return-value]

    # ===================================================================
    # Public API: tick-driven lifecycle
    # ===================================================================

    async def on_tick(self, tick: Tick) -> None:
        """Process every active block on ``tick.symbol``.

        The order-watcher calls this on every fresh tick. We:

        1. Check cancel-price hits on CREATED/ACTIVE blocks whose
           cancel-price guard is still active. If hit → INVALID,
           cancel pending orders.
        2. Reconcile fills: any pending order whose entry price was
           crossed by this tick is treated as filled. We compute SL
           and TP at this exact spread, modify the position on the
           broker, and mark the order OPEN.
        3. Reconcile TP/SL hits on open positions.
        4. If any rung TP-hits → block becomes WIN; cancel remaining
           pending orders.
        5. If all rungs SL-hit → block becomes LOSS.

        The reconciliation order matters: we must check fills BEFORE
        checking SL/TP, because a single tick may both fill a limit
        and immediately trigger its SL (rare but real on news ticks).
        """
        async with session_scope() as session:
            blocks = await self._load_active_blocks_for_symbol(
                session, symbol=tick.symbol
            )

            for block in blocks:
                if block.status not in {
                    BlockStatus.CREATED,
                    BlockStatus.ACTIVE,
                }:
                    continue

                # 1. Cancel-price guard.
                if (
                    block.cancel_price_active
                    and self._cancel_price_hit(block, tick)
                ):
                    await self._handle_cancel_price_hit(session, block, tick)
                    continue

                # 2-3-4-5. Fill / TP / SL / state transitions.
                await self._process_fills(session, block, tick)
                await self._process_exits(session, block, tick)
                await self._finalise_block_state(session, block)

    # ===================================================================
    # Public API: manual close
    # ===================================================================

    async def cancel_block(self, block_id: int) -> None:
        """Manually close a block — cancel pendings, close opens.

        The Telegram /cancel handler calls this. We do NOT mark the
        block as a WIN or LOSS here; we mark it ``BLOCK_MANUAL_CLOSE``
        via a status of ``WIN`` if net PnL is positive, ``LOSS`` if
        negative, ``INVALID`` if zero. This way reporting code can
        treat manual closes the same as automatic ones without losing
        the audit trail (the event log records the manual trigger).
        """
        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            if block is None:
                raise ValueError(f"block #{block_id} not found")
            if block.is_terminal:
                return

            # Cancel every pending order.
            for o in block.orders:
                if o.state == OrderState.PENDING and o.entry_ticket:
                    res = await self._adapter.cancel_order(o.entry_ticket)
                    if res.ok:
                        await repository.mark_order_cancelled(
                            session, o, closed_at=_utcnow()
                        )

            # Close every open position. Adapter returns the close
            # price so we can compute realised PnL on the rung.
            for o in block.orders:
                if o.state == OrderState.OPEN and o.position_ticket:
                    res = await self._adapter.close_position(o.position_ticket)
                    if res.ok and res.filled_price is not None:
                        pnl = self._compute_pnl(block, o, res.filled_price)
                        await repository.mark_order_closed(
                            session,
                            o,
                            state=OrderState.SL_HIT,  # Treat as SL-equiv: closed not via TP
                            close_price=res.filled_price,
                            pnl_usd=pnl,
                            commission_usd=None,
                            closed_at=_utcnow(),
                        )

            await repository.append_event(
                session,
                block_id=block.id,
                event_type=EventType.BLOCK_MANUAL_CLOSE,
            )
            net = sum((o.pnl_usd or 0.0) for o in block.orders)
            terminal_status = self._pnl_to_terminal_status(net)
            await repository.set_block_status(
                session,
                block,
                terminal_status,
                closed_at=_utcnow(),
                net_pnl=net,
            )

            chat_id = block.chat_id

        await self._notify(
            notify.block_manual_close(
                block_id=block_id,
                chat_id=chat_id,
                net_pnl=net,
                terminal_status=str(terminal_status),
            )
        )

    # ===================================================================
    # Internal: tick processing
    # ===================================================================

    async def _process_fills(
        self,
        session: AsyncSession,
        block: Block,
        tick: Tick,
    ) -> None:
        """Fill any pending orders whose entry crossed this tick.

        Once an order fills we compute its spread-aware SL and the
        TP from the *cumulative real risk so far*, then ask the
        broker to attach both to the freshly-opened position.
        """
        # Take a snapshot of orders sorted by seq so when we compute
        # cumulative real risk we see all already-filled rungs first.
        sorted_orders = sorted(block.orders, key=lambda o: o.seq)
        symbol_info = await self._adapter.get_symbol_info(block.symbol)

        for order in sorted_orders:
            if order.state != OrderState.PENDING:
                continue
            if not self._entry_crossed(block.side, order.entry_price, tick):
                continue

            # Disable cancel-price guard on first fill.
            if block.cancel_price_active:
                await repository.disable_cancel_price(session, block)

            # Compute live SL with chain-rule spread adjustment.
            next_entry = self._next_entry_price(block, order, sorted_orders)
            fill = FillContext(
                entry_price=order.entry_price,
                spread=tick.spread,
                side=block.side,
            )
            sl_live = compute_sl_price(
                next_entry=next_entry,
                fill=fill,
                safety_multiplier=self._settings.sl_spread_safety,
            )

            # Compute live TP from cumulative real risk (this rung + prior).
            cumulative = self._cumulative_real_risk_through(order, sorted_orders)
            usd_per_unit = usd_per_price_unit_from_lot(
                lot=order.lot,
                trade_tick_size=symbol_info.trade_tick_size,
                trade_tick_value=symbol_info.trade_tick_value,
            )
            tp_live = compute_tp_price(
                fill=fill,
                lot=order.lot,
                cumulative_real_risk_usd=cumulative,
                usd_per_price_unit=usd_per_unit,
                tp_multiplier=self._settings.tp_multiplier,
            )

            # Attach SL/TP to the freshly-opened position.
            position_ticket = order.entry_ticket or ""
            mod_res = await self._adapter.modify_position(
                position_ticket=position_ticket, sl=sl_live, tp=tp_live
            )
            if not mod_res.ok:
                # The position is open but unprotected. Mark as error
                # and let the operator decide; the block is still
                # alive but flagged for attention.
                logger.error(
                    "Failed to attach SL/TP to position {pos}: {err}",
                    pos=position_ticket,
                    err=mod_res.error_message,
                )
                order.state = OrderState.ERROR
                await self._notify(
                    notify.block_error(
                        block_id=block.id,
                        chat_id=block.chat_id,
                        reason=f"SL/TP attach failed on rung {order.seq}",
                    )
                )
                continue

            await repository.mark_order_filled(
                session,
                order,
                fill_price=order.entry_price,
                fill_spread=tick.spread,
                sl_live=sl_live,
                tp_live=tp_live,
                position_ticket=position_ticket,
                filled_at=_utcnow(),
            )
            await repository.append_event(
                session,
                block_id=block.id,
                order_seq=order.seq,
                event_type=EventType.ORDER_FILLED,
                payload={
                    "entry": order.entry_price,
                    "sl_live": sl_live,
                    "tp_live": tp_live,
                    "spread": tick.spread,
                },
            )
            await self._notify(
                notify.order_filled(
                    block_id=block.id,
                    chat_id=block.chat_id,
                    seq=order.seq,
                    entry=order.entry_price,
                    sl=sl_live,
                    tp=tp_live,
                    lot=order.lot,
                    spread=tick.spread,
                )
            )

    async def _process_exits(
        self,
        session: AsyncSession,
        block: Block,
        tick: Tick,
    ) -> None:
        """Close any open positions whose SL/TP hit on this tick."""
        for order in sorted(block.orders, key=lambda o: o.seq):
            if order.state != OrderState.OPEN:
                continue
            exit_state, exit_price = self._exit_for_open(block, order, tick)
            if exit_state is None or exit_price is None:
                continue

            pnl = self._compute_pnl(block, order, exit_price)
            await repository.mark_order_closed(
                session,
                order,
                state=exit_state,
                close_price=exit_price,
                pnl_usd=pnl,
                commission_usd=None,
                closed_at=_utcnow(),
            )
            event_type = (
                EventType.TP_HIT if exit_state == OrderState.TP_HIT else EventType.SL_HIT
            )
            await repository.append_event(
                session,
                block_id=block.id,
                order_seq=order.seq,
                event_type=event_type,
                payload={"price": exit_price, "pnl": pnl},
            )
            if exit_state == OrderState.SL_HIT:
                await self._notify(
                    notify.sl_hit(
                        block_id=block.id,
                        chat_id=block.chat_id,
                        seq=order.seq,
                        price=exit_price,
                        pnl=pnl,
                    )
                )

    async def _finalise_block_state(
        self,
        session: AsyncSession,
        block: Block,
    ) -> None:
        """Move the block to WIN / LOSS if its orders justify it."""
        sorted_orders = sorted(block.orders, key=lambda o: o.seq)

        # WIN — any TP hit terminates the block.
        win_rung = next(
            (o for o in sorted_orders if o.state == OrderState.TP_HIT),
            None,
        )
        if win_rung is not None:
            # Cancel every still-pending order; leave any already-open
            # positions intact ("variant A": let them ride with their
            # own SL/TP). We mark the rest of the orders cancelled.
            for o in sorted_orders:
                if o.state == OrderState.PENDING and o.entry_ticket:
                    res = await self._adapter.cancel_order(o.entry_ticket)
                    if res.ok:
                        await repository.mark_order_cancelled(
                            session, o, closed_at=_utcnow()
                        )

            net = sum((o.pnl_usd or 0.0) for o in sorted_orders)
            await repository.set_block_status(
                session,
                block,
                BlockStatus.WIN,
                closed_at=_utcnow(),
                net_pnl=net,
            )
            await repository.append_event(
                session,
                block_id=block.id,
                event_type=EventType.BLOCK_WIN,
                payload={"win_seq": win_rung.seq, "net_pnl": net},
            )
            await self._notify(
                notify.block_win(
                    block_id=block.id,
                    chat_id=block.chat_id,
                    win_order_seq=win_rung.seq,
                    tp_price=win_rung.close_price,
                    net_pnl=net,
                )
            )
            return

        # LOSS — every rung SL'd.
        if all(o.state == OrderState.SL_HIT for o in sorted_orders):
            net = sum((o.pnl_usd or 0.0) for o in sorted_orders)
            await repository.set_block_status(
                session,
                block,
                BlockStatus.LOSS,
                closed_at=_utcnow(),
                net_pnl=net,
            )
            await repository.append_event(
                session,
                block_id=block.id,
                event_type=EventType.BLOCK_LOSS,
                payload={"net_pnl": net},
            )
            await self._notify(
                notify.block_loss(
                    block_id=block.id,
                    chat_id=block.chat_id,
                    net_pnl=net,
                )
            )

    # ===================================================================
    # Internal: cancel-price handling
    # ===================================================================

    async def _handle_cancel_price_hit(
        self,
        session: AsyncSession,
        block: Block,
        tick: Tick,
    ) -> None:
        """Cancel-price hit before any fill → block becomes INVALID."""
        for o in block.orders:
            if o.state == OrderState.PENDING and o.entry_ticket:
                res = await self._adapter.cancel_order(o.entry_ticket)
                if res.ok:
                    await repository.mark_order_cancelled(
                        session, o, closed_at=_utcnow()
                    )

        await repository.set_block_status(
            session,
            block,
            BlockStatus.INVALID,
            closed_at=_utcnow(),
            net_pnl=0.0,
        )
        await repository.append_event(
            session,
            block_id=block.id,
            event_type=EventType.BLOCK_INVALID,
            payload={
                "cancel_price": block.cancel_price,
                "tick_bid": tick.bid,
                "tick_ask": tick.ask,
            },
        )
        await self._notify(
            notify.block_invalid(
                block_id=block.id,
                chat_id=block.chat_id,
                cancel_price=block.cancel_price,
                mark_price=tick.mid,
            )
        )

    # ===================================================================
    # Internal: utility predicates and math
    # ===================================================================

    def _cancel_price_hit(self, block: Block, tick: Tick) -> bool:
        """True iff the price has crossed the cancel-price barrier.

        BUY blocks treat ``cancel_price`` as an upper barrier (it sits
        above the entry ladder). When ASK ≥ cancel_price the setup is
        invalidated. SELL is symmetric — BID ≤ cancel_price.
        """
        if block.side == BlockSide.BUY:
            return tick.ask >= block.cancel_price
        return tick.bid <= block.cancel_price

    def _entry_crossed(
        self, side: BlockSide, entry: float, tick: Tick
    ) -> bool:
        """True iff this tick fills a pending limit order at ``entry``."""
        if side == BlockSide.BUY:
            return tick.ask <= entry
        return tick.bid >= entry

    def _exit_for_open(
        self,
        block: Block,
        order: Order,
        tick: Tick,
    ) -> tuple[OrderState | None, float | None]:
        """Detect TP or SL hit on an open position.

        Returns ``(state, price)`` or ``(None, None)`` if nothing fired.
        """
        sl = order.sl_price_live
        tp = order.tp_price_live
        if sl is None or tp is None:
            return None, None

        if block.side == BlockSide.BUY:
            if tick.bid <= sl:
                return OrderState.SL_HIT, sl
            if tick.bid >= tp:
                return OrderState.TP_HIT, tp
        else:
            if tick.ask >= sl:
                return OrderState.SL_HIT, sl
            if tick.ask <= tp:
                return OrderState.TP_HIT, tp
        return None, None

    def _next_entry_price(
        self,
        block: Block,
        order: Order,
        sorted_orders: list[Order],
    ) -> float:
        """Return the entry price of rung N+1, or the final SL for the last rung.

        The plan stores ``sl_price_plan`` already equal to that value
        — it is computed by the strategy module from the Fibonacci
        ladder — so we don't have to recompute the ladder here.
        """
        idx = order.seq - 1
        if idx + 1 < len(sorted_orders):
            return sorted_orders[idx + 1].entry_price
        # Final rung — sl_price_plan IS the explicit 138.2% level.
        return order.sl_price_plan

    def _cumulative_real_risk_through(
        self,
        order: Order,
        sorted_orders: list[Order],
    ) -> float:
        """Sum the realised risk of rungs 1..seq (inclusive).

        Uses each order's stored ``real_risk_usd`` so the TP formula
        sees the same numbers the user saw in the plan preview.
        """
        cum = 0.0
        for o in sorted_orders:
            cum += o.real_risk_usd
            if o.seq == order.seq:
                break
        return cum

    def _compute_pnl(
        self, block: Block, order: Order, close_price: float
    ) -> float:
        """Approximate realised PnL for this rung.

        We use the symbol's contract size implicit in ``lot`` and the
        price delta. This is broker-agnostic but doesn't include
        swap / commission — those are filled in from broker
        confirmations elsewhere.
        """
        if order.fill_price is None:
            return 0.0
        if block.side == BlockSide.BUY:
            delta = close_price - order.fill_price
        else:
            delta = order.fill_price - close_price
        # Convert price delta to USD using the symbol's tick value.
        # Cached on the order would be cleaner but the engine doesn't
        # store it; recompute from real_risk_usd / sl_distance instead.
        # We have: real_risk = lot × (sl_distance / tick_size) × tick_value
        # → tick_value/tick_size = real_risk / (lot × sl_distance)
        # Then: pnl = delta × tick_value/tick_size × lot
        #            = delta × real_risk / sl_distance
        sl_distance = abs(order.entry_price - order.sl_price_plan)
        if sl_distance == 0:
            return 0.0
        ratio = order.real_risk_usd / sl_distance
        return round(delta * ratio, 4)

    def _pnl_to_terminal_status(self, net_pnl: float) -> BlockStatus:
        if net_pnl > 0:
            return BlockStatus.WIN
        if net_pnl < 0:
            return BlockStatus.LOSS
        return BlockStatus.INVALID

    # ===================================================================
    # Internal: rollback / error handling
    # ===================================================================

    async def _rollback_placements(
        self, placements: list[_PlacedOrder]
    ) -> None:
        """Best-effort cancel of orders placed during a failed create."""
        for p in placements:
            try:
                await self._adapter.cancel_order(p.ticket)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "rollback cancel failed for ticket {t}: {err}",
                    t=p.ticket,
                    err=exc,
                )

    async def _mark_block_error(
        self, block_id: int, *, reason: str
    ) -> None:
        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            if block is None:
                return
            await repository.set_block_status(
                session,
                block,
                BlockStatus.ERROR,
                closed_at=_utcnow(),
            )
            await repository.append_event(
                session,
                block_id=block_id,
                event_type=EventType.BLOCK_ERROR,
                payload={"reason": reason},
            )
            await self._notify(
                notify.block_error(
                    block_id=block_id,
                    chat_id=block.chat_id,
                    reason=reason,
                )
            )

    # ===================================================================
    # Internal: session helpers
    # ===================================================================

    async def _load_active_blocks_for_symbol(
        self,
        session: AsyncSession,
        *,
        symbol: str,
    ) -> list[Block]:
        """Eager-load active blocks for ``symbol`` with their orders."""
        all_active = await repository.list_active_blocks(session)
        return [b for b in all_active if b.symbol == symbol]

    async def _notify(self, notification: notify.Notification) -> None:
        """Forward a notification, swallowing transport errors."""
        if self._on_notification is None:
            return
        try:
            await self._on_notification(notification)
        except Exception as exc:  # noqa: BLE001
            logger.error("notification delivery failed: {err}", err=exc)


# ---------------------------------------------------------------------
# Standalone helpers
# ---------------------------------------------------------------------

def _make_client_tag(block_id: int, seq: int) -> str:
    """Stable idempotency tag written into MT5 comment / magic-number.

    Format: ``fb-blk{id}-s{seq}``. Short enough to fit MT5's 31-char
    comment limit, structured enough that we can parse it on restart
    to recover blocks from broker-side state alone.
    """
    return f"fb-blk{block_id}-s{seq}"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# Keep imports honest — these are required for type hints and side effects.
__all__ = ["BlockEngine"]

# A small sanity check: the engine assumes the uniform gap constant
# in :mod:`futures_bot.strategy.fib` matches the one used by
# :mod:`futures_bot.core.spread_guard`. Drift would cause subtle bugs
# in the SL/TP math, so we assert once at import time.
assert abs(GAP_PCT - 0.127333) < 1e-4, (
    "fib.GAP_PCT drift — re-check uniform ladder definition"
)

# Imports that some tooling can mistake for unused.
_ = (cumulative_risks, loss_per_lot, SymbolSpec, SymbolInfo, OrderResult)
