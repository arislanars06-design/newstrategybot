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
from src.core.plan import EXPECTED_ORDERS_PER_BLOCK, BlockPlan
from src.core.risk import order_risk_amount, total_block_risk
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

    async def auto_detect_track_side(self, symbol: str):
        """Try to infer the block's side from unassigned entries.

        Returns ``(side, error)`` — exactly the contract of
        :func:`tracker.auto_detect_side`. Lets the Telegram flow skip
        the BUY/SELL question whenever the symbol unambiguously has
        eight entries on a single side.
        """
        from src.core.tracker import auto_detect_side as _auto

        open_orders = await self._client.list_open_orders(symbol)
        async with session_scope() as session:
            assigned = await repository.get_assigned_exchange_order_ids(
                session, symbol=symbol
            )
        return _auto(open_orders, assigned_order_ids=assigned)

    async def compute_block_realtime_pnl(self, block_id: int) -> dict | None:
        """Return realised + unrealised PnL for an active block.

        For closed orders we sum :attr:`Order.pnl`, which the engine
        populates from Binance's ``realizedPnl`` (already net of fees).
        For still-open positions (``TRIGGERED`` state) we estimate
        unrealised PnL from the current mark price, the order's filled
        entry price, and its quantity.

        Returns ``None`` if the block doesn't exist. The output dict has
        the shape ``{realised, unrealised, total, mark_price, open_count}``.
        Mark price is fetched only once per call regardless of how many
        rungs are open.
        """
        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            if block is None:
                return None
            symbol = block.symbol
            side = block.side
            orders_snapshot = [
                {
                    "state": o.state,
                    "qty": o.qty,
                    "filled_entry": o.filled_entry_price,
                    "pnl": o.pnl,
                }
                for o in block.orders
            ]

        realised = round(sum((o["pnl"] or 0.0) for o in orders_snapshot), 6)
        open_orders = [
            o for o in orders_snapshot
            if o["state"] == OrderState.TRIGGERED and o["filled_entry"] is not None
        ]

        if not open_orders:
            return {
                "realised": realised,
                "unrealised": 0.0,
                "total": realised,
                "mark_price": None,
                "open_count": 0,
            }

        try:
            mark_price = await self._client.get_mark_price(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "mark price fetch failed for block={b}: {err}", b=block_id, err=exc
            )
            return {
                "realised": realised,
                "unrealised": None,  # signal "unknown" rather than fake-zero
                "total": realised,
                "mark_price": None,
                "open_count": len(open_orders),
            }

        direction = 1.0 if side == BlockSide.BUY else -1.0
        unrealised = sum(
            direction * (mark_price - o["filled_entry"]) * o["qty"]
            for o in open_orders
        )

        return {
            "realised": realised,
            "unrealised": round(unrealised, 6),
            "total": round(realised + unrealised, 6),
            "mark_price": mark_price,
            "open_count": len(open_orders),
        }

    async def discover_tracked_orders(
        self, *, symbol: str, side: BlockSide
    ):
        """Read open orders from Binance and group them into a candidate block.

        Pure inspection: nothing is persisted. The caller (Telegram
        handler) shows the result to the user for confirmation, then
        calls :meth:`track_block` if accepted.
        """
        from src.core.tracker import discover_block as _discover

        open_orders = await self._client.list_open_orders(symbol)
        async with session_scope() as session:
            assigned = await repository.get_assigned_exchange_order_ids(
                session, symbol=symbol
            )
        return _discover(
            open_orders, side=side, assigned_order_ids=assigned
        )

    async def track_block(
        self,
        *,
        symbol: str,
        side: BlockSide,
        cancel_price: float,
        chat_id: int,
        rungs,  # list[TrackedRung]
        note: str | None = None,
    ) -> Block:
        """Adopt user-placed Binance orders as a new block.

        Unlike :meth:`create_block`, no orders are sent to the exchange.
        We only persist the block + 8 orders (with their existing
        Binance order IDs) and start watching them.
        """
        if len(rungs) != EXPECTED_ORDERS_PER_BLOCK:
            raise ValueError(
                f"track_block requires exactly {EXPECTED_ORDERS_PER_BLOCK} rungs"
            )

        async with session_scope() as session:
            block = await repository.create_block(
                session,
                symbol=symbol,
                side=side,
                cancel_price=cancel_price,
                chat_id=chat_id,
                note=note,
                is_managed=False,
            )
            for rung in rungs:
                await repository.add_order(
                    session,
                    block_id=block.id,
                    seq=rung.seq,
                    entry_price=rung.entry_price,
                    tp_price=rung.tp_price,
                    sl_price=rung.sl_price,
                    qty=rung.qty,
                    client_id_prefix="trk",
                    entry_order_id=rung.entry_order_id,
                    tp_order_id=rung.tp_order_id,
                    sl_order_id=rung.sl_order_id,
                )
            await repository.add_event(
                session,
                block_id=block.id,
                event_type=EventType.BLOCK_CREATED,
                payload={
                    "symbol": symbol,
                    "side": str(side),
                    "cancel_price": cancel_price,
                    "tracked": True,
                },
            )
            await repository.update_block_status(session, block, BlockStatus.ACTIVE)
            await repository.add_event(
                session,
                block_id=block.id,
                event_type=EventType.ORDERS_PLACED,
                payload={"tracked": True},
            )
            block_id = block.id

        await self._mark_stream.add_symbol(symbol)

        await self._notify(
            type_=NotificationType.BLOCK_CREATED,
            block_id=block_id,
            chat_id=chat_id,
            payload={
                "symbol": symbol,
                "side": str(side),
                "orders": len(rungs),
                "cancel_price": cancel_price,
                "tracked": True,
            },
        )

        async with session_scope() as session:
            return await repository.get_block(session, block_id)

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
                is_managed=True,
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
            await self._maybe_unsubscribe_mark_stream(symbol)
            await self._notify(
                type_=NotificationType.BLOCK_MANUAL_CLOSE,
                block_id=block_id,
                chat_id=chat_id,
                payload={},
            )

    async def modify_cancel_price(
        self, block_id: int, new_cancel_price: float
    ) -> None:
        """Update the cancel price of an active block.

        Only allowed while the block is still ACTIVE *and* its cancel
        price is still active (no rung has triggered yet) — once a
        position has been opened, the cancel-price rule no longer
        applies and modifying it would be misleading.

        The new price must be on the correct side of the ladder:
        above the highest entry for BUY blocks, below the lowest
        entry for SELL blocks.
        """
        async with self._lock_for(block_id):
            async with session_scope() as session:
                block = await repository.get_block(session, block_id)
                if block is None:
                    raise ValueError(f"Block #{block_id} not found.")
                if block.is_terminal:
                    raise ValueError(
                        f"Block #{block_id} is already in a terminal state "
                        f"({block.status}); cancel price cannot be changed."
                    )
                if not block.cancel_price_active:
                    raise ValueError(
                        "A position has already opened in this block; the "
                        "cancel-price rule no longer applies."
                    )

                entries = [o.entry_price for o in block.orders]
                if block.side == BlockSide.BUY:
                    if new_cancel_price <= max(entries):
                        raise ValueError(
                            f"BUY block: cancel price must be > the highest "
                            f"entry ({max(entries)})."
                        )
                else:
                    if new_cancel_price >= min(entries):
                        raise ValueError(
                            f"SELL block: cancel price must be < the lowest "
                            f"entry ({min(entries)})."
                        )

                old_cancel = block.cancel_price
                block.cancel_price = new_cancel_price
                await repository.add_event(
                    session,
                    block_id=block_id,
                    event_type=EventType.BLOCK_MODIFIED,
                    payload={
                        "field": "cancel_price",
                        "old": old_cancel,
                        "new": new_cancel_price,
                    },
                )

    # ----- WebSocket handlers -----

    async def _handle_order_update(self, update: OrderUpdate) -> None:
        """Entry point for every ORDER_TRADE_UPDATE event.

        Matching strategy:

        1. If the client_order_id starts with Binance's ``autoclose-``
           prefix, the event is a forced liquidation (margin call /
           bankruptcy auto-close). Route it to :meth:`_on_liquidation`
           which marks the rung as ``LIQUIDATED`` and re-places any
           subsequent rungs Binance cancelled in the process — this
           is how the chain keeps going past a single rung's
           liquidation.
        2. If the client_order_id parses as one we generated (managed
           blocks created via /newblock or /fib), use it directly —
           it carries block_id and seq, so the lookup is O(1) and
           works even before we have persisted the exchange order_id.
        3. Otherwise (tracked blocks, replacement orders whose ids
           don't follow our prefix, or any race where the client_id
           shortcut is unavailable) fall back to looking up by the
           Binance ``orderId`` against entry/tp/sl_order_id columns.
        """
        if update.client_order_id.startswith("autoclose-"):
            await self._on_liquidation(update)
            return
        parsed = _parse_client_id(update.client_order_id)
        if parsed is not None:
            await self._dispatch_with_lock_managed(parsed, update)
            return
        await self._dispatch_with_lock_tracked(update)

    async def _on_liquidation(self, update: OrderUpdate) -> None:
        """React to a Binance forced-liquidation event.

        Binance signals a liquidation by sending an ORDER_TRADE_UPDATE
        whose client_order_id starts with ``autoclose-``. The event
        represents the synthetic market order Binance creates to
        close the position at the bankruptcy price.

        Side effects:

        1. Find the active block whose ``TRIGGERED`` rung corresponds
           to the liquidated position (matched by symbol + position
           side).
        2. Mark that rung as ``LIQUIDATED`` with the actual exit price
           and realised PnL from the event.
        3. Cancel the rung's now-orphan TP (Binance usually does this
           too, but we do it idempotently as defense in depth).
        4. Re-place every subsequent rung that Binance auto-cancelled
           on liquidation (entries N+1..last). Each replacement uses
           a fresh client_order_id (``-r{n}`` suffix) so we don't
           collide with the cancelled order in Binance's dedup window.
        5. Emit ``RUNG_LIQUIDATED`` notification so the trader sees
           the chain restart in Telegram.

        Only fires on terminal fill events (``update.is_filled``).
        Intermediate states from the liquidation engine are ignored.
        """
        if not update.is_filled:
            return

        async with session_scope() as session:
            active = await repository.list_active_blocks(session)
            target_block_id = None
            target_seq = None
            for b in active:
                if b.symbol != update.symbol:
                    continue
                expected = (
                    "LONG" if b.side == BlockSide.BUY else "SHORT"
                )
                if (
                    update.position_side
                    and update.position_side != "BOTH"
                    and update.position_side != expected
                ):
                    continue
                triggered = [
                    o for o in b.orders if o.state == OrderState.TRIGGERED
                ]
                if not triggered:
                    continue
                target_block_id = b.id
                # Pick the lowest-seq triggered rung — chain only ever
                # has one open position at a time, so this is unique
                # in practice. If we ever see multiple, log it.
                if len(triggered) > 1:
                    logger.warning(
                        "Liquidation: block={b} has {n} TRIGGERED rungs; "
                        "picking the lowest seq", b=b.id, n=len(triggered),
                    )
                target_seq = min(o.seq for o in triggered)
                break

        if target_block_id is None:
            logger.warning(
                "Liquidation event for {sym}/{ps} did not match any active "
                "block with a TRIGGERED rung — ignoring",
                sym=update.symbol, ps=update.position_side,
            )
            return

        async with self._lock_for(target_block_id):
            await self._apply_liquidation(target_block_id, target_seq, update)

    async def _apply_liquidation(
        self, block_id: int, seq: int, update: OrderUpdate
    ) -> None:
        """Apply liquidation effects to a single rung and restart the chain."""
        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            if block is None or block.is_terminal:
                return
            rung = next((o for o in block.orders if o.seq == seq), None)
            if rung is None or rung.state != OrderState.TRIGGERED:
                return

            # 1. Mark rung as LIQUIDATED. PnL is best-effort: prefer
            # exchange-reported realisedPnl; fall back to entry/exit
            # math.
            pnl = update.realized_pnl
            if not pnl and rung.filled_entry_price is not None:
                direction = 1 if block.side == BlockSide.BUY else -1
                pnl = direction * (
                    update.avg_fill_price - rung.filled_entry_price
                ) * rung.qty
            await repository.update_order_state(
                session,
                rung,
                OrderState.LIQUIDATED,
                filled_exit_price=update.avg_fill_price,
                pnl=pnl,
            )
            await repository.add_event(
                session,
                block_id=block.id,
                order_seq=rung.seq,
                event_type=EventType.RUNG_LIQUIDATED,
                payload={
                    "exit_price": update.avg_fill_price,
                    "pnl": pnl,
                },
            )

            # 2. Orphan TP cancel (idempotent).
            await self._cancel_one_order(block, rung, "t")

            # 3. Re-place subsequent rungs.
            chat_id = block.chat_id
            symbol = block.symbol
            side = block.side
            following = [
                o for o in block.orders
                if o.seq > rung.seq and o.state in (
                    OrderState.PENDING, OrderState.CANCELLED
                )
            ]

        replaced: list[int] = []
        for follower in following:
            try:
                await self._replace_rung_orders(block_id, follower.seq)
                replaced.append(follower.seq)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Chain restart: re-placement failed for block={b} "
                    "seq={s}; aborting further re-placements",
                    b=block_id, s=follower.seq,
                )
                # Mark block ERROR and stop — partial restart is worse
                # than no restart because the trader can't reason about
                # what's on Binance.
                await self._mark_block_error(
                    block_id,
                    f"chain restart failed at rung {follower.seq}: {exc}",
                )
                return

        if replaced:
            async with session_scope() as session:
                await repository.add_event(
                    session,
                    block_id=block_id,
                    event_type=EventType.CHAIN_RESTART,
                    payload={
                        "from_seq": rung.seq + 1,
                        "replaced_seqs": replaced,
                    },
                )

        # 4. Notify the trader. One message per liquidation; the chain
        # restart is summarised in the same payload so the trader sees
        # it without parsing two notifications.
        await self._notify(
            type_=NotificationType.RUNG_LIQUIDATED,
            block_id=block_id,
            chat_id=chat_id,
            payload={
                "seq": rung.seq,
                "exit_price": update.avg_fill_price,
                "pnl": pnl,
                "replaced_seqs": replaced,
            },
        )

    async def _replace_rung_orders(self, block_id: int, seq: int) -> None:
        """Place a fresh entry+SL pair for a rung after Binance cancelled them.

        Uses a fresh client_order_id (with a millisecond suffix) so we
        sidestep Binance's 5-minute dedup window on the original ids.
        The order rows in the DB get their new exchange ids updated;
        client_ids are NOT changed in the DB because every other code
        path keys off them. Replacement client_ids don't match our
        regex (intentionally) — fill events for them route through
        the ``_dispatch_with_lock_tracked`` path via exchange order id.
        """
        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            assert block is not None
            rung = next(o for o in block.orders if o.seq == seq)
            entry_client_id = rung.entry_client_id
            sl_client_id = rung.sl_client_id
            symbol = block.symbol
            side = block.side
            entry_price = rung.entry_price
            sl_price = rung.sl_price
            qty = rung.qty

        # Fresh client ids for the replacement to dodge Binance's
        # dedup window. ``-r{epoch_ms}`` is unique per replacement.
        import time
        suffix = f"-r{int(time.time() * 1000)}"
        new_entry_cid = (entry_client_id + suffix)[:36]  # Binance caps at 36
        new_sl_cid = (sl_client_id + suffix)[:36]

        entry_side = _entry_side_for(side)
        exit_side = _exit_side_for(side)
        position_side = _position_side_for(side)

        entry_id = await self._client.place_entry_limit(
            symbol=symbol,
            side=entry_side,
            position_side=position_side,
            qty=qty,
            price=entry_price,
            client_id=new_entry_cid,
        )
        try:
            sl_id = await self._client.place_sl_stop(
                symbol=symbol,
                side=exit_side,
                position_side=position_side,
                qty=qty,
                stop_price=sl_price,
                client_id=new_sl_cid,
            )
        except Exception:
            # Roll back the entry we just placed, mirror the upfront
            # placement logic in _place_entry_orders.
            try:
                await self._client.cancel_order_by_client_id(
                    symbol, new_entry_cid
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Replacement rollback: cancel of fresh entry %s failed",
                    new_entry_cid,
                )
            raise

        async with session_scope() as session:
            fresh_block = await repository.get_block(session, block_id)
            assert fresh_block is not None
            target = next(
                o for o in fresh_block.orders if o.seq == seq
            )
            target.entry_order_id = entry_id
            target.sl_order_id = sl_id
            target.state = OrderState.PENDING
            # Clear any stale exit-side fields from the cancelled
            # incarnation; the rung is "fresh" again.
            target.filled_entry_price = None
            target.filled_exit_price = None
            target.pnl = None
            target.triggered_at = None
            target.closed_at = None

    async def _dispatch_with_lock_managed(
        self, parsed: _ParsedClientId, update: OrderUpdate
    ) -> None:
        async with self._lock_for(parsed.block_id):
            try:
                await self._dispatch_order_update(
                    block_id=parsed.block_id, seq=parsed.seq, kind=parsed.kind, update=update
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "order-update dispatch failed for block={b} seq={s} kind={k}",
                    b=parsed.block_id,
                    s=parsed.seq,
                    k=parsed.kind,
                )

    async def _dispatch_with_lock_tracked(self, update: OrderUpdate) -> None:
        if not update.order_id:
            return
        # Resolve outside the per-block lock so we know which block to
        # lock; then re-fetch under the lock for atomicity.
        async with session_scope() as session:
            found = await repository.find_order_and_kind_by_exchange_id(
                session, update.order_id
            )
        if found is None:
            return  # not one of our orders
        order, kind = found
        block_id = order.block_id
        seq = order.seq

        async with self._lock_for(block_id):
            try:
                await self._dispatch_order_update(
                    block_id=block_id, seq=seq, kind=kind, update=update
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "order-update dispatch failed for tracked block={b} seq={s} kind={k}",
                    b=block_id, s=seq, k=kind,
                )

    async def _dispatch_order_update(
        self,
        *,
        block_id: int,
        seq: int,
        kind: str,
        update: OrderUpdate,
    ) -> None:
        if not (update.is_filled or update.is_canceled):
            # Intermediate states (NEW, PARTIALLY_FILLED) — ignore.
            return

        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            if block is None or block.is_terminal:
                return
            order = next((o for o in block.orders if o.seq == seq), None)
            if order is None:
                return

            if kind == "e":
                if update.is_filled:
                    await self._on_entry_filled(session, block, order, update)
                elif update.is_canceled:
                    await self._on_entry_canceled(session, block, order, update)
            elif kind == "t":
                if update.is_filled:
                    await self._on_tp_filled(session, block, order, update)
            elif kind == "l":
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
                await self._maybe_unsubscribe_mark_stream(fresh.symbol)
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
        await self._place_tp_only(session, block, order)

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
        await self._cancel_one_order(block, order, "l")

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
        await self._cancel_one_order(block, order, "t")

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

    async def _maybe_unsubscribe_mark_stream(self, symbol: str) -> None:
        """Drop the mark-stream subscription for ``symbol`` IF safe to do so.

        The cancel-price watcher (``_handle_mark_price``) reads from one
        per-symbol mark-stream subscription. We must keep that
        subscription alive as long as **any** active block on this
        symbol still wants cancel-price detection. Calling
        ``mark_stream.remove_symbol`` unconditionally — as several call
        sites used to — silently broke cancel-price for unrelated
        blocks: trader had block A active on ETHUSDT, then block B
        on the same symbol was manually cancelled, and the unconditional
        unsubscribe killed mark-price ticks for block A. The bug was
        only visible when the trader noticed cancel-price never firing
        on a block that had been sitting idle for hours — verified in
        production via an empty journalctl grep for ``MarkPriceStream``
        despite an active block existing.

        ``cancel_price_active`` filters out blocks where the rule no
        longer applies (an entry has already triggered) so we don't
        keep a useless subscription open.
        """
        async with session_scope() as session:
            active = await repository.list_active_blocks(session)
        still_needed = any(
            b.symbol == symbol and b.cancel_price_active
            for b in active
        )
        if not still_needed:
            await self._mark_stream.remove_symbol(symbol)

    async def _maybe_finalize(self, session: AsyncSession, block: Block) -> None:
        """Detect terminal state by inspecting all child orders."""
        if block.is_terminal:
            # Already terminal (set elsewhere — typically _on_tp_filled
            # for a WIN). Drop the mark-stream subscription if no other
            # active block on this symbol still needs it.
            await self._maybe_unsubscribe_mark_stream(block.symbol)
            return

        states = [o.state for o in block.orders]
        terminal_states = {
            OrderState.TP_HIT,
            OrderState.SL_HIT,
            OrderState.LIQUIDATED,
            OrderState.CANCELLED,
            OrderState.ERROR,
        }
        if not all(s in terminal_states for s in states):
            return  # still some pending or triggered

        any_tp = any(o.state == OrderState.TP_HIT for o in block.orders)
        # LIQUIDATED rungs count as losing rungs for the LOSS-vs-INVALID
        # decision. The block is considered LOSS if any rung ended at
        # SL_HIT or LIQUIDATED with no TP anywhere.
        any_sl = any(
            o.state in (OrderState.SL_HIT, OrderState.LIQUIDATED)
            for o in block.orders
        )

        net_pnl = _compute_net_pnl(block)

        if any_tp:
            # Already handled by _on_tp_filled. Just refresh PnL.
            if block.status != BlockStatus.WIN:
                await repository.update_block_status(
                    session, block, BlockStatus.WIN, net_pnl=net_pnl
                )
            await self._maybe_unsubscribe_mark_stream(block.symbol)
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
            await self._maybe_unsubscribe_mark_stream(block.symbol)
            return

        # All cancelled, no TP, no SL — equivalent to invalid.
        await repository.update_block_status(session, block, BlockStatus.INVALID)
        await repository.add_event(
            session,
            block_id=block.id,
            event_type=EventType.BLOCK_INVALID,
            payload={"reason": "all_cancelled"},
        )
        await self._maybe_unsubscribe_mark_stream(block.symbol)

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
        """Place entry LIMITs **and** SL STOP_MARKETs upfront for every rung.

        Pre-placing SLs makes the trader's whole risk plan visible on
        Binance immediately, which matches their mental model of how
        the strategy should appear on the chart.

        TPs are still placed lazily (in :meth:`_on_entry_filled`) — for
        a typical Fibonacci ladder the TP stopPrice often sits on the
        already-passed side of current market (e.g. a LONG TP at 103
        when the market is at 105) and Binance rejects such orders
        with "would trigger immediately". Once an entry actually fills
        the market is at or near the entry price, so TP stopPrices
        ahead of the trade direction become safe to register.

        If SL placement fails for a rung we cancel that rung's entry
        we just placed, so the engine never leaves dangling entries
        without their matching SL.
        """
        side = _entry_side_for(plan.side)
        exit_side = _exit_side_for(plan.side)
        position_side = _position_side_for(plan.side)

        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            assert block is not None
            orders = list(block.orders)

        for order in orders:
            try:
                entry_id = await self._client.place_entry_limit(
                    symbol=plan.symbol,
                    side=side,
                    position_side=position_side,
                    qty=order.qty,
                    price=order.entry_price,
                    client_id=order.entry_client_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Entry placement failed for block={b} seq={s}: {err}",
                    b=block_id, s=order.seq, err=exc,
                )
                raise

            try:
                sl_id = await self._client.place_sl_stop(
                    symbol=plan.symbol,
                    side=exit_side,
                    position_side=position_side,
                    qty=order.qty,
                    stop_price=order.sl_price,
                    client_id=order.sl_client_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Upfront SL placement failed for block={b} seq={s}: {err}; "
                    "cancelling the just-placed entry to keep state consistent.",
                    b=block_id, s=order.seq, err=exc,
                )
                # Roll back the matching entry so we don't leave it dangling.
                try:
                    await self._client.cancel_order_by_client_id(
                        plan.symbol, order.entry_client_id
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("rollback cancel of entry also failed; ignoring")
                raise

            async with session_scope() as session:
                fresh = await repository.get_block(session, block_id)
                assert fresh is not None
                target = next(o for o in fresh.orders if o.seq == order.seq)
                await repository.set_order_exchange_ids(
                    session, target, entry_id=entry_id, sl_id=sl_id
                )
            await asyncio.sleep(self._settings.order_place_delay_ms / 1000.0)

    async def _place_tp_only(
        self, session: AsyncSession, block: Block, order: Order
    ) -> None:
        """Place the rung's TP after its entry has filled.

        SL is already on the book from the upfront placement done in
        :meth:`_place_entry_orders`, so this only needs to add the TP.
        On failure the rung is parked in ``ERROR`` state and an
        audit-log entry is written; the SL stays where it is so the
        position is never left without protection.
        """
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
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "TP placement failed for block={b} seq={s}", b=block.id, s=order.seq
            )
            await repository.update_order_state(session, order, OrderState.ERROR)
            await repository.add_event(
                session,
                block_id=block.id,
                order_seq=order.seq,
                event_type=EventType.BLOCK_ERROR,
                payload={"stage": "tp_placement", "error": str(exc)},
            )
            return

        await repository.set_order_exchange_ids(session, order, tp_id=tp_id)

    async def _cancel_pending_entries(
        self, session: AsyncSession, block: Block
    ) -> None:
        """Cancel every still-pending rung's entry **and** its upfront SL.

        Called when the block reaches a terminal state via WIN. The
        SL was placed upfront, so leaving it on the book after the
        block is closed would be a stray order — cancel it here.
        """
        pendings = [o for o in block.orders if o.state == OrderState.PENDING]
        for order in pendings:
            await self._cancel_one_order(block, order, "e")
            await self._cancel_one_order(block, order, "l")
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
        """Cancel every order owned by this block (defensive close).

        Two-phase cancel:

        1. Targeted per-order pass tries Binance's regular cancel
           endpoint for each rung's entry, TP and SL. ``-2011`` /
           ``-2013`` (already gone) are treated as success.
        2. A single ``futures_cancel_all_open_orders`` sweep catches
           anything the per-order pass couldn't reach — typically
           orders we never recorded an exchange ID for, or stragglers
           created during a partial restart.

        The sweep is gated on the symbol having no other active blocks:
        otherwise we'd kill another block's resting orders. For tracked
        blocks (``is_managed=False``) we skip the sweep entirely
        because the trader may have unrelated manual orders on the same
        symbol that they wouldn't expect us to wipe out.
        """
        for order in block.orders:
            for kind in ("e", "t", "l"):
                await self._cancel_one_order(block, order, kind)
            if order.state == OrderState.PENDING:
                await repository.update_order_state(session, order, OrderState.CANCELLED)
                await repository.add_event(
                    session,
                    block_id=block.id,
                    order_seq=order.seq,
                    event_type=EventType.ORDER_CANCELLED,
                    payload={"reason": "block_terminal"},
                )

        if not block.is_managed:
            return

        other_active = [
            b for b in await repository.list_active_blocks(session)
            if b.id != block.id and b.symbol == block.symbol
        ]
        if other_active:
            logger.info(
                "Skipping cancel-all sweep on {sym}: {n} other active "
                "block(s) share this symbol; their orders would be hit too",
                sym=block.symbol, n=len(other_active),
            )
            return

        try:
            await self._client.cancel_all_for_symbol(block.symbol)
            logger.info(
                "Cancel-all sweep completed on {sym} after closing block #{b}",
                sym=block.symbol, b=block.id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Cancel-all sweep for {sym} failed: {err}",
                sym=block.symbol, err=exc,
            )

    async def _cancel_one_order(
        self, block: Block, order: Order, kind: str
    ) -> bool:
        """Cancel a single child order, picking the right identifier.

        We prefer the exchange ID (Binance ``orderId``) when it's
        available; the client_id route is a safety net for the brief
        window between persisting an order row and Binance returning
        the exchange ID. Both client methods are idempotent: they
        return ``False`` (rather than raising) when Binance reports
        ``-2011`` / ``-2013`` "Unknown order" — i.e. the order was
        already filled, expired or cancelled.
        """
        if kind == "e":
            client_id = order.entry_client_id
            exchange_id = order.entry_order_id
        elif kind == "t":
            client_id = order.tp_client_id
            exchange_id = order.tp_order_id
        elif kind == "l":
            client_id = order.sl_client_id
            exchange_id = order.sl_order_id
        else:
            return False

        try:
            if exchange_id:
                return await self._client.cancel_order_by_exchange_id(
                    block.symbol, exchange_id
                )
            if client_id:
                return await self._client.cancel_order_by_client_id(
                    block.symbol, client_id
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "cancel failed (block={b} seq={s} kind={k}): {err}",
                b=block.id, s=order.seq, k=kind, err=exc,
            )
        return False

    async def _mark_block_error(self, block_id: int, reason: str) -> None:
        """Mark a block as ERROR and clean up any orders we already placed.

        Production-found bug: ``_place_entry_orders`` places entries
        and SLs rung-by-rung. If a later rung fails (typically on SL
        placement), the per-rung rollback only cancels *that* rung's
        entry — earlier rungs that succeeded fully are left **dangling
        on Binance**. Block goes to ERROR but real money stays parked
        in the orphan margin reservations.

        This method now cancels every order whose Binance ID we managed
        to record before the failure, by walking the block's rungs and
        attempting cancel-by-id for entry, TP, and SL slots. Each call
        is idempotent — :meth:`_cancel_one_order` returns ``False``
        rather than raising for already-gone orders, so this is safe
        to call even when only some rungs were placed.

        We also drop the mark-stream subscription if no other active
        block on this symbol still needs it (PR #6 helper) — pure
        bookkeeping, since a block in ERROR state never re-arms its
        cancel-price rule.
        """
        async with session_scope() as session:
            block = await repository.get_block(session, block_id)
            if block is None or block.is_terminal:
                return
            symbol = block.symbol

            # Best-effort cleanup of any orders we placed before failing.
            # _cancel_one_order is idempotent against -2011/-2013 so
            # we don't need to track which slots succeeded vs failed.
            for order in block.orders:
                for kind in ("e", "t", "l"):
                    try:
                        await self._cancel_one_order(block, order, kind)
                    except Exception:  # noqa: BLE001
                        # Cleanup is best-effort. A persistent failure
                        # gets captured in the BLOCK_ERROR audit event
                        # via the original `reason` string; we don't
                        # let it block the status transition.
                        logger.debug(
                            "BLOCK_ERROR cleanup: cancel block={b} seq={s} "
                            "kind={k} failed; continuing",
                            b=block_id, s=order.seq, k=kind,
                        )

            await repository.update_block_status(session, block, BlockStatus.ERROR)
            await repository.add_event(
                session,
                block_id=block_id,
                event_type=EventType.BLOCK_ERROR,
                payload={"reason": reason},
            )
            chat_id = block.chat_id

        # Mark stream cleanup — only if no other active block on this
        # symbol still wants cancel-price detection.
        await self._maybe_unsubscribe_mark_stream(symbol)

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
