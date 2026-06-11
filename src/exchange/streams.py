"""Background WebSocket workers: order updates and mark-price feeds."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from binance import BinanceSocketManager
from loguru import logger

from src.exchange.client import BinanceClient
from src.exchange.types import OrderUpdate

OrderUpdateHandler = Callable[[OrderUpdate], Awaitable[None]]
MarkPriceHandler = Callable[[str, float], Awaitable[None]]


class UserDataStream:
    """Listens to the futures user data stream and dispatches order events.

    Reconnects automatically with exponential backoff. Subscribers are
    invoked sequentially per event so that the engine sees a strict order
    of updates.
    """

    def __init__(
        self, client: BinanceClient, on_order_update: OrderUpdateHandler
    ) -> None:
        self._client = client
        self._on_order_update = on_order_update
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="user-data-stream")
        logger.info("UserDataStream started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
            logger.info("UserDataStream stopped")

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                bsm = BinanceSocketManager(self._client.raw)
                async with bsm.futures_user_socket() as socket:
                    backoff = 1.0  # reset after successful connect
                    while not self._stop_event.is_set():
                        msg = await socket.recv()
                        await self._handle_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("UserDataStream error: {err}", err=exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return
        event_type = msg.get("e")
        if event_type != "ORDER_TRADE_UPDATE":
            return
        o = msg.get("o") or {}
        try:
            update = OrderUpdate(
                symbol=str(o.get("s", "")),
                client_order_id=str(o.get("c", "")),
                order_id=str(o.get("i", "")),
                side=str(o.get("S", "")),
                position_side=str(o.get("ps", "BOTH")),
                order_type=str(o.get("o", "")),
                status=str(o.get("X", "")),
                avg_fill_price=float(o.get("ap", 0) or 0),
                executed_qty=float(o.get("z", 0) or 0),
                realized_pnl=float(o.get("rp", 0) or 0),
                event_time_ms=int(msg.get("E", 0) or 0),
                raw=o,
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Failed to parse ORDER_TRADE_UPDATE: {err}", err=exc)
            return

        try:
            await self._on_order_update(update)
        except Exception:  # noqa: BLE001 — never crash the stream
            logger.exception(
                "Order-update handler raised for cid={cid}",
                cid=update.client_order_id,
            )


class MarkPriceStream:
    """Subscribes to per-symbol mark-price streams.

    The set of subscribed symbols can grow or shrink at runtime (e.g.
    when a new block is created or closed). Internally each symbol has
    its own task; adding/removing a symbol only touches that one task.
    """

    def __init__(
        self, client: BinanceClient, on_price: MarkPriceHandler
    ) -> None:
        self._client = client
        self._on_price = on_price
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_event = asyncio.Event()

    @property
    def symbols(self) -> set[str]:
        return set(self._tasks.keys())

    async def add_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol in self._tasks:
            return
        task = asyncio.create_task(self._run_symbol(symbol), name=f"mark-price-{symbol}")
        self._tasks[symbol] = task
        logger.info("MarkPriceStream subscribed to {sym}", sym=symbol)

    async def remove_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        task = self._tasks.pop(symbol, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        logger.info("MarkPriceStream unsubscribed from {sym}", sym=symbol)

    async def stop(self) -> None:
        self._stop_event.set()
        for symbol in list(self._tasks.keys()):
            await self.remove_symbol(symbol)

    async def _run_symbol(self, symbol: str) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                bsm = BinanceSocketManager(self._client.raw)
                async with bsm.symbol_mark_price_socket(symbol=symbol, fast=False) as sock:
                    backoff = 1.0
                    while not self._stop_event.is_set():
                        msg = await sock.recv()
                        if not isinstance(msg, dict):
                            continue
                        try:
                            price = float(msg.get("p") or msg.get("markPrice") or 0)
                        except (TypeError, ValueError):
                            continue
                        if price <= 0:
                            continue
                        try:
                            await self._on_price(symbol, price)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "Mark-price handler raised for {sym}", sym=symbol
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "MarkPriceStream({sym}) error: {err}", sym=symbol, err=exc
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
