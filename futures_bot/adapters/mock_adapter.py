"""Deterministic in-memory broker used by unit tests.

The mock has two jobs:

1. Let us exercise the engine, order-watcher, and Telegram handlers
   end-to-end without a Wine container, an Exness account, or a
   network — making ``pytest`` runnable on any laptop or CI runner.
2. Give the trader a working bot during the first development weeks
   before they open a demo account. The mock supports market
   replay scripts (``feed_tick`` / ``advance_to_price``) so the
   trader can stage what-if scenarios.

Behaviour matches MT5 closely enough for development:

* Pending limit orders fill when the appropriate side of the
  bid/ask crosses the limit price.
* Open positions close when bid hits TP/SL (for BUY) or ask hits
  them (for SELL).
* Account balance updates only when positions close, not on
  unrealised P&L — keeps the deltas easy to assert in tests.

What it deliberately does *not* model: slippage, commission tiers,
news-time spread widening, requotes. The MT5 adapter is the place
to test those.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from futures_bot.adapters.base import (
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    Position,
    SymbolInfo,
    Tick,
)
from futures_bot.db.enums import BlockSide


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# Default symbol catalogue. Mirrors XAUUSD / EURUSD / GBPJPY etc as
# Exness Raw Spread quotes them. Add to it from tests via
# ``MockAdapter.add_symbol(...)`` rather than editing this file.
_DEFAULT_SYMBOLS: dict[str, SymbolInfo] = {
    "XAUUSD": SymbolInfo(
        symbol="XAUUSD",
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1.0,           # $1 P&L per 1¢ move on 1.00 lot
        trade_contract_size=100.0,      # 1 lot = 100 oz
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        spread_typical=0.20,
    ),
    "EURUSD": SymbolInfo(
        symbol="EURUSD",
        digits=5,
        point=0.00001,
        trade_tick_size=0.00001,
        trade_tick_value=0.1,           # $0.1 P&L per point on 1.00 lot
        trade_contract_size=100000.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        spread_typical=0.00008,         # 0.8 pip
    ),
    "GBPUSD": SymbolInfo(
        symbol="GBPUSD",
        digits=5,
        point=0.00001,
        trade_tick_size=0.00001,
        trade_tick_value=0.1,
        trade_contract_size=100000.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        spread_typical=0.0001,          # 1.0 pip
    ),
}


@dataclass
class _PendingOrder:
    """Server-side bookkeeping for a not-yet-filled limit order."""

    ticket: str
    request: OrderRequest


@dataclass
class _OpenPosition:
    """Server-side bookkeeping for a position the mock has 'opened'."""

    ticket: str
    request: OrderRequest
    fill_price: float
    sl: float | None
    tp: float | None
    open_time: datetime = field(default_factory=_utcnow)


class MockAdapter(BrokerAdapter):
    """In-memory broker for tests and pre-MT5 development.

    Not thread-safe across event loops; use one instance per test.
    Within a single event loop the public coroutines acquire a
    private lock so callers can fire-and-forget concurrent requests
    just like they would against MT5.
    """

    def __init__(
        self,
        *,
        starting_balance: float = 10_000.0,
        symbols: dict[str, SymbolInfo] | None = None,
    ) -> None:
        self._balance = starting_balance
        self._symbols: dict[str, SymbolInfo] = dict(symbols or _DEFAULT_SYMBOLS)
        self._ticks: dict[str, Tick] = {}
        self._pending: dict[str, _PendingOrder] = {}
        self._positions: dict[str, _OpenPosition] = {}
        self._next_ticket: int = 1_000_000
        self._connected: bool = False
        self._lock = asyncio.Lock()

    # ---- Test-only helpers ----

    def add_symbol(self, info: SymbolInfo) -> None:
        """Register an extra symbol for the test."""
        self._symbols[info.symbol] = info

    async def feed_tick(self, tick: Tick) -> list[OrderResult]:
        """Push a tick into the broker and run the matching engine.

        Returns the list of order-results produced by this tick:
        new fills, SL/TP hits, etc. Tests can assert directly on
        what changed.
        """
        async with self._lock:
            self._ticks[tick.symbol] = tick
            return self._match(tick)

    # ---- BrokerAdapter ----

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        info = self._symbols.get(symbol)
        if info is None:
            raise ValueError(f"unknown symbol: {symbol!r}")
        return info

    async def get_tick(self, symbol: str) -> Tick:
        tick = self._ticks.get(symbol)
        if tick is None:
            raise ValueError(
                f"no tick has been fed for {symbol!r}; call feed_tick first"
            )
        return tick

    async def get_account_balance(self) -> float:
        return self._balance

    async def get_account_equity(self) -> float:
        # Match MT5: equity = balance + unrealised P&L of open positions.
        unrealised = 0.0
        for pos in self._positions.values():
            tick = self._ticks.get(pos.request.symbol)
            if tick is None:
                continue
            unrealised += self._unrealised(pos, tick)
        return self._balance + unrealised

    async def get_free_margin(self) -> float:
        # Mock doesn't model margin requirements; report full equity.
        return await self.get_account_equity()

    async def place_order(self, request: OrderRequest) -> OrderResult:
        async with self._lock:
            ticket = self._allocate_ticket()
            if request.order_type == "MARKET":
                tick = self._ticks.get(request.symbol)
                if tick is None:
                    return OrderResult(
                        ok=False,
                        ticket=None,
                        position_ticket=None,
                        filled_price=None,
                        error_code=10004,
                        error_message="no tick available",
                    )
                fill = tick.ask if request.side == BlockSide.BUY else tick.bid
                pos = _OpenPosition(
                    ticket=ticket,
                    request=request,
                    fill_price=fill,
                    sl=request.sl,
                    tp=request.tp,
                )
                self._positions[ticket] = pos
                return OrderResult(
                    ok=True,
                    ticket=ticket,
                    position_ticket=ticket,
                    filled_price=fill,
                    error_code=None,
                    error_message=None,
                )

            # LIMIT order — sits as pending until matched by a tick.
            self._pending[ticket] = _PendingOrder(ticket=ticket, request=request)
            return OrderResult(
                ok=True,
                ticket=ticket,
                position_ticket=None,
                filled_price=None,
                error_code=None,
                error_message=None,
            )

    async def modify_position(
        self,
        *,
        position_ticket: str,
        sl: float | None,
        tp: float | None,
    ) -> OrderResult:
        async with self._lock:
            pos = self._positions.get(position_ticket)
            if pos is None:
                return OrderResult(
                    ok=False,
                    ticket=None,
                    position_ticket=None,
                    filled_price=None,
                    error_code=10025,
                    error_message="position not found",
                )
            pos.sl = sl
            pos.tp = tp
            return OrderResult(
                ok=True,
                ticket=position_ticket,
                position_ticket=position_ticket,
                filled_price=pos.fill_price,
                error_code=None,
                error_message=None,
            )

    async def cancel_order(self, ticket: str) -> OrderResult:
        async with self._lock:
            popped = self._pending.pop(ticket, None)
            if popped is None:
                return OrderResult(
                    ok=False,
                    ticket=ticket,
                    position_ticket=None,
                    filled_price=None,
                    error_code=10009,
                    error_message="order not pending",
                )
            return OrderResult(
                ok=True,
                ticket=ticket,
                position_ticket=None,
                filled_price=None,
                error_code=None,
                error_message=None,
            )

    async def close_position(self, position_ticket: str) -> OrderResult:
        async with self._lock:
            pos = self._positions.pop(position_ticket, None)
            if pos is None:
                return OrderResult(
                    ok=False,
                    ticket=position_ticket,
                    position_ticket=None,
                    filled_price=None,
                    error_code=10025,
                    error_message="position not found",
                )
            tick = self._ticks.get(pos.request.symbol)
            if tick is None:
                # Roll back so the test can pinpoint the missing tick.
                self._positions[position_ticket] = pos
                return OrderResult(
                    ok=False,
                    ticket=position_ticket,
                    position_ticket=None,
                    filled_price=None,
                    error_code=10004,
                    error_message="no tick available",
                )
            close_price = tick.bid if pos.request.side == BlockSide.BUY else tick.ask
            self._book_pnl(pos, close_price)
            return OrderResult(
                ok=True,
                ticket=position_ticket,
                position_ticket=position_ticket,
                filled_price=close_price,
                error_code=None,
                error_message=None,
            )

    async def list_open_positions(
        self, symbol: str | None = None
    ) -> list[Position]:
        out: list[Position] = []
        for pos in self._positions.values():
            if symbol is not None and pos.request.symbol != symbol:
                continue
            tick = self._ticks.get(pos.request.symbol)
            profit = self._unrealised(pos, tick) if tick else 0.0
            out.append(
                Position(
                    ticket=pos.ticket,
                    symbol=pos.request.symbol,
                    side=pos.request.side,
                    lot=pos.request.lot,
                    open_price=pos.fill_price,
                    sl=pos.sl,
                    tp=pos.tp,
                    profit=profit,
                    swap=0.0,
                    commission=0.0,
                    open_time=pos.open_time,
                    comment=pos.request.client_tag,
                )
            )
        return out

    async def list_pending_orders(
        self, symbol: str | None = None
    ) -> list[OrderResult]:
        out: list[OrderResult] = []
        for pending in self._pending.values():
            if symbol is not None and pending.request.symbol != symbol:
                continue
            out.append(
                OrderResult(
                    ok=True,
                    ticket=pending.ticket,
                    position_ticket=None,
                    filled_price=None,
                    error_code=None,
                    error_message=None,
                )
            )
        return out

    # ---- Internals ----

    def _allocate_ticket(self) -> str:
        self._next_ticket += 1
        return str(self._next_ticket)

    def _match(self, tick: Tick) -> list[OrderResult]:
        """Run the simple matching engine for the just-fed tick."""
        results: list[OrderResult] = []

        # 1. Limit-order fills.
        for ticket, pending in list(self._pending.items()):
            req = pending.request
            if req.symbol != tick.symbol or req.order_type != "LIMIT":
                continue
            if req.price is None:
                continue
            triggered = (
                req.side == BlockSide.BUY and tick.ask <= req.price
            ) or (
                req.side == BlockSide.SELL and tick.bid >= req.price
            )
            if not triggered:
                continue
            del self._pending[ticket]
            pos = _OpenPosition(
                ticket=ticket,
                request=req,
                fill_price=req.price,
                sl=req.sl,
                tp=req.tp,
            )
            self._positions[ticket] = pos
            results.append(
                OrderResult(
                    ok=True,
                    ticket=ticket,
                    position_ticket=ticket,
                    filled_price=req.price,
                    error_code=None,
                    error_message=None,
                )
            )

        # 2. SL/TP exits on open positions.
        for ticket, pos in list(self._positions.items()):
            if pos.request.symbol != tick.symbol:
                continue
            close_price = self._exit_price_for(pos, tick)
            if close_price is None:
                continue
            del self._positions[ticket]
            self._book_pnl(pos, close_price)
            results.append(
                OrderResult(
                    ok=True,
                    ticket=ticket,
                    position_ticket=ticket,
                    filled_price=close_price,
                    error_code=None,
                    error_message=None,
                )
            )

        return results

    def _exit_price_for(self, pos: _OpenPosition, tick: Tick) -> float | None:
        """Return the price the mock closes ``pos`` at, or None if no hit."""
        if pos.request.side == BlockSide.BUY:
            if pos.sl is not None and tick.bid <= pos.sl:
                return pos.sl
            if pos.tp is not None and tick.bid >= pos.tp:
                return pos.tp
        else:  # SELL
            if pos.sl is not None and tick.ask >= pos.sl:
                return pos.sl
            if pos.tp is not None and tick.ask <= pos.tp:
                return pos.tp
        return None

    def _unrealised(self, pos: _OpenPosition, tick: Tick) -> float:
        info = self._symbols[pos.request.symbol]
        if pos.request.side == BlockSide.BUY:
            delta = tick.bid - pos.fill_price
        else:
            delta = pos.fill_price - tick.ask
        return delta * pos.request.lot * info.trade_contract_size

    def _book_pnl(self, pos: _OpenPosition, close_price: float) -> None:
        info = self._symbols[pos.request.symbol]
        if pos.request.side == BlockSide.BUY:
            delta = close_price - pos.fill_price
        else:
            delta = pos.fill_price - close_price
        pnl = delta * pos.request.lot * info.trade_contract_size
        self._balance += pnl
