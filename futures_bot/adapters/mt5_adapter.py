"""MT5 broker adapter.

Talks to a MetaTrader 5 terminal running under Wine in a Docker
container. The terminal exposes a Python-compatible RPC bridge via
the ``mt5linux`` package (https://github.com/lucas-campagna/mt5linux):
we import ``MetaTrader5`` from there and call its API as if we were
on Windows.

Because the actual ``mt5linux`` library is only installed inside the
Wine container's Python environment (and only relevant when MT5 is
present), this module imports it lazily inside :meth:`connect`. The
file is otherwise importable in CI / on the developer's laptop, where
the mock adapter is used.

This implementation is a **skeleton**: the public method bodies raise
``NotImplementedError`` for now and call ``_todo`` so the engine can
be wired up against the interface today. The actual MT5 calls are
filled in once the demo account exists and we can verify the response
shapes. Every method already includes the docstring and the type
contract so the rest of the team can code against it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from futures_bot.adapters.base import (
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    Position,
    SymbolInfo,
    Tick,
)
from futures_bot.config import Settings


def _todo(method: str) -> None:
    """Raise a uniform message so it's obvious which method still needs work."""
    raise NotImplementedError(
        f"MT5Adapter.{method} is not implemented yet — fill in once we have "
        f"a connected demo account to validate the response shape."
    )


class MT5Adapter(BrokerAdapter):
    """Production broker adapter.

    Construction is cheap and does not touch the network. Call
    :meth:`connect` to actually open the RPC session. The adapter
    keeps the imported MT5 module on ``self._mt5`` so concurrent
    callers share one bridge; an asyncio lock serialises requests
    because the mt5linux server is single-threaded.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._mt5: Any | None = None        # populated in connect()
        self._lock = asyncio.Lock()
        self._connected: bool = False

    # ---- Lifecycle ----

    async def connect(self) -> None:
        """Open the RPC bridge and log in to the broker account.

        Imports ``mt5linux`` lazily so unit tests on machines without
        MT5 / Wine don't fail at import time.
        """
        async with self._lock:
            if self._connected:
                return

            try:
                # ``mt5linux`` exposes a near-identical surface to the
                # Windows ``MetaTrader5`` package. The connection to
                # the Wine container is opened here.
                from mt5linux import MetaTrader5  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover — env-dependent
                raise RuntimeError(
                    "mt5linux is not installed in this environment. "
                    "Run inside the Docker container or install with "
                    "`pip install mt5linux`."
                ) from exc

            self._mt5 = MetaTrader5(
                host=self._settings.mt5_host,
                port=self._settings.mt5_port,
            )
            if not self._mt5.initialize():
                error = self._mt5.last_error()
                raise RuntimeError(f"MT5 initialize() failed: {error}")

            ok = self._mt5.login(
                login=self._settings.mt5_login,
                password=self._settings.mt5_password,
                server=self._settings.mt5_server,
            )
            if not ok:
                error = self._mt5.last_error()
                self._mt5.shutdown()
                self._mt5 = None
                raise RuntimeError(f"MT5 login() failed: {error}")

            self._connected = True
            account = self._mt5.account_info()
            logger.success(
                "MT5 connected: login={login} balance={bal} server={server}",
                login=getattr(account, "login", "?"),
                bal=getattr(account, "balance", "?"),
                server=getattr(account, "server", "?"),
            )

    async def disconnect(self) -> None:
        async with self._lock:
            if self._mt5 is not None:
                try:
                    self._mt5.shutdown()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("MT5 shutdown raised: {err}", err=exc)
            self._mt5 = None
            self._connected = False

    async def is_connected(self) -> bool:
        return self._connected and self._mt5 is not None

    # ---- Market data ----

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        # The MT5 ``symbol_info`` struct exposes everything we need;
        # we just need to copy the relevant fields into our DTO and
        # call ``symbol_select(symbol, True)`` first to make sure the
        # symbol is added to Market Watch (otherwise tick_value is 0).
        _todo("get_symbol_info")
        raise AssertionError("unreachable")

    async def get_tick(self, symbol: str) -> Tick:
        # ``symbol_info_tick`` returns a struct with bid/ask/time;
        # convert to our Tick dataclass.
        _todo("get_tick")
        raise AssertionError("unreachable")

    # ---- Account ----

    async def get_account_balance(self) -> float:
        _todo("get_account_balance")
        raise AssertionError("unreachable")

    async def get_account_equity(self) -> float:
        _todo("get_account_equity")
        raise AssertionError("unreachable")

    async def get_free_margin(self) -> float:
        _todo("get_free_margin")
        raise AssertionError("unreachable")

    # ---- Orders ----

    async def place_order(self, request: OrderRequest) -> OrderResult:
        # MT5 path: build a TradeRequest dict, call ``order_send``,
        # interpret the result struct. Limit orders need
        # ``action=TRADE_ACTION_PENDING``, market orders
        # ``TRADE_ACTION_DEAL``. The ``type_filling`` must be
        # ``ORDER_FILLING_RETURN`` so limit orders sit on the book
        # instead of being cancelled when not immediately fillable.
        _todo("place_order")
        raise AssertionError("unreachable")

    async def modify_position(
        self,
        *,
        position_ticket: str,
        sl: float | None,
        tp: float | None,
    ) -> OrderResult:
        # MT5 path: TRADE_ACTION_SLTP with the position ticket and
        # new SL/TP. Modifications are atomic — both legs change in
        # one call, which is exactly what the order-watcher wants
        # after a fill.
        _todo("modify_position")
        raise AssertionError("unreachable")

    async def cancel_order(self, ticket: str) -> OrderResult:
        # TRADE_ACTION_REMOVE with the order ticket. Returns the
        # standard result struct.
        _todo("cancel_order")
        raise AssertionError("unreachable")

    async def close_position(self, position_ticket: str) -> OrderResult:
        # TRADE_ACTION_DEAL with an opposite-side market order, lot
        # equal to the position size, and the position ticket
        # referenced in ``position`` field. MT5 nets to zero.
        _todo("close_position")
        raise AssertionError("unreachable")

    # ---- State sync ----

    async def list_open_positions(
        self, symbol: str | None = None
    ) -> list[Position]:
        # ``positions_get(symbol=…)`` returns a tuple of structs.
        _todo("list_open_positions")
        raise AssertionError("unreachable")

    async def list_pending_orders(
        self, symbol: str | None = None
    ) -> list[OrderResult]:
        # ``orders_get(symbol=…)`` returns the pending limits.
        _todo("list_pending_orders")
        raise AssertionError("unreachable")
