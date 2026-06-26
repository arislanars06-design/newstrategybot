"""MT5 broker adapter.

Talks to a MetaTrader 5 terminal running under Wine in a Docker
container. The terminal exposes a Python-compatible RPC bridge via
the ``mt5linux`` package (https://github.com/lucas-campagna/mt5linux):
we import ``MetaTrader5`` from there and call its API as if we were
on Windows.

Two architectural decisions worth flagging up front:

* **Blocking RPC.** Every ``mt5linux`` call is a synchronous TCP
  round-trip. We wrap them in :func:`asyncio.to_thread` so the bot's
  event loop never stalls, and serialise them behind an asyncio lock
  because the mt5linux server itself is single-threaded.
* **DTO translation at the boundary.** mt5linux returns ``rpyc``
  netref proxies. We copy the scalar fields out into local Python
  values immediately, so the rest of the bot never sees a network
  proxy and tests can stub the adapter without touching rpyc.

The actual ``mt5linux`` library is only installed in the Docker
container's Python environment; CI and dev laptops use the mock
adapter. Importing this module is therefore safe everywhere — the
network library is only imported lazily inside :meth:`connect`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from futures_bot.adapters.base import (
    BrokerAdapter,
    ClosedPositionInfo,
    OrderRequest,
    OrderResult,
    Position,
    SymbolInfo,
    Tick,
    symbol_name_candidates,
)
from futures_bot.config import Settings
from futures_bot.db.enums import BlockSide


# ---------------------------------------------------------------------
# MT5 constants
# ---------------------------------------------------------------------
#
# Hardcoded so the adapter can be imported (and unit-tested) without
# the ``mt5linux`` / ``MetaTrader5`` library being installed. Values
# come from MetaQuotes' official Python integration docs and have not
# changed since the MT5 build that introduced them. See
# https://www.mql5.com/en/docs/python_metatrader5 for the full table.

# trade actions
_TRADE_ACTION_DEAL = 1       # immediate market deal
_TRADE_ACTION_PENDING = 5    # place a pending order
_TRADE_ACTION_SLTP = 6       # modify SL/TP of an open position
_TRADE_ACTION_MODIFY = 7     # modify a still-pending order
_TRADE_ACTION_REMOVE = 8     # cancel a still-pending order

# order types
_ORDER_TYPE_BUY = 0
_ORDER_TYPE_SELL = 1
_ORDER_TYPE_BUY_LIMIT = 2
_ORDER_TYPE_SELL_LIMIT = 3

# order time / filling
_ORDER_TIME_GTC = 0
_ORDER_FILLING_FOK = 0
_ORDER_FILLING_IOC = 1
_ORDER_FILLING_RETURN = 2

# symbol filling-mode bitmask
_SYMBOL_FILLING_FOK = 1
_SYMBOL_FILLING_IOC = 2

# MT5 deal-reason codes (see official MetaTrader5 Python integration
# docs). We translate them into the broker-agnostic string codes the
# rest of the bot uses so the engine never has to import MT5
# constants.
_DEAL_REASON_SL = 4
_DEAL_REASON_TP = 5

# Deal entry direction. ``DEAL_ENTRY_OUT`` is the closing leg of a
# position — that's the one whose ``reason`` tells us *why* the
# position closed.
_DEAL_ENTRY_OUT = 1

# How far back to search the trade history when reconciling. A week
# is enough for our weekly-trading horizon and short enough to keep
# the RPC call cheap; the trader's own log will surface anything
# older that needs attention.
_HISTORY_LOOKBACK_DAYS = 7

# retcodes — successful outcomes
_TRADE_RETCODE_PLACED = 10008       # pending order accepted
_TRADE_RETCODE_DONE = 10009         # generic success
_TRADE_RETCODE_DONE_PARTIAL = 10010 # partial fill
_SUCCESS_RETCODES = frozenset(
    {_TRADE_RETCODE_PLACED, _TRADE_RETCODE_DONE, _TRADE_RETCODE_DONE_PARTIAL}
)


# Map MT5 retcodes to operator-actionable remediation hints. The
# raw ``comment`` field from the broker is too terse ("AutoTrading
# disabled by client" doesn't tell you to press Ctrl+E in MT5);
# these hints get appended so the Telegram notification points the
# trader at the exact fix. Codes not in this table fall back to
# the broker's comment field, which is fine for "Invalid stops"-
# class errors that need a strategy tweak rather than a setup tweak.
_RETCODE_HINTS: dict[int, str] = {
    10004:  # REQUOTE
        "Брокер запросил новую котировку. Спред нестабилен — "
        "повторите попытку или дождитесь спокойного рынка.",
    10006:  # REJECT
        "Заявка отклонена брокером. Проверьте, не закрыт ли "
        "рынок по этому символу.",
    10013:  # INVALID_REQUEST
        "Запрос отклонён как недопустимый. Проверьте размер лота "
        "и шаг volume_step символа.",
    10014:  # INVALID_VOLUME
        "Некорректный объём. Лот меньше volume_min или не "
        "кратен volume_step.",
    10015:  # INVALID_PRICE
        "Цена ордера далеко от текущей. Возможно, неверный "
        "символ или биржа закрыта.",
    10016:  # INVALID_STOPS
        "SL/TP слишком близко к цене. Расширьте диапазон 0%/100% "
        "или уменьшите количество знаков точности символа.",
    10018:  # MARKET_CLOSED
        "Рынок закрыт. Откройте блок в торговые часы инструмента.",
    10019:  # NO_MONEY
        "Недостаточно свободной маржи. Уменьшите base_risk или "
        "проверьте кредитное плечо аккаунта.",
    10027:  # AUTO_TRADING_DISABLED
        "Включите AutoTrading в MT5: в верхней панели нажмите "
        "иконку «AutoTrading» (или Ctrl+E). Без неё терминал "
        "блокирует любые автоматические заявки.",
    10030:  # INVALID_FILL
        "Брокер не принимает выбранный режим заполнения. "
        "Обычно лечится переключением IOC/FOK — сообщите боту, "
        "какой ваш символ требует.",
}

# MT5 caps the order comment at 31 characters; truncate defensively.
_COMMENT_MAX_LEN = 31

# Max slippage (in points) we tolerate for market orders. Only used
# for close_position; the strategy itself never sends market entries.
_MARKET_DEVIATION_POINTS = 20


class MT5Adapter(BrokerAdapter):
    """Production broker adapter.

    Construction is cheap and does not touch the network. Call
    :meth:`connect` to actually open the RPC session. The adapter
    caches :class:`SymbolInfo` for the lifetime of the connection
    because metadata almost never changes intraday but is hit on
    every fill check.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._mt5: Any | None = None        # populated in connect()
        self._lock = asyncio.Lock()
        self._connected: bool = False
        # Symbol-info cache: filled on first request, invalidated on
        # disconnect so a reconnect after a broker session reset
        # picks up any changed metadata.
        self._symbol_cache: dict[str, SymbolInfo] = {}
        # Resolution cache: requested → broker-actual name. Populated
        # on every successful :meth:`resolve_symbol`. Cleared on
        # disconnect so a reconnect rediscovers names cleanly.
        self._resolved_cache: dict[str, str] = {}

    # ---- Lifecycle ----

    async def connect(self) -> None:
        """Open the RPC bridge and log in to the broker account.

        Imports ``mt5linux`` lazily so unit tests on machines without
        MT5 / Wine don't fail at import time.
        """
        async with self._lock:
            if self._connected:
                return
            await asyncio.to_thread(self._sync_connect)
            self._connected = True

    def _sync_connect(self) -> None:
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

        client = MetaTrader5(
            host=self._settings.mt5_host,
            port=self._settings.mt5_port,
        )
        # initialize() with no args because the terminal is launched
        # by the Wine container; we just need the RPC channel.
        if not client.initialize():
            error = client.last_error()
            raise RuntimeError(f"MT5 initialize() failed: {error}")

        ok = client.login(
            login=int(self._settings.mt5_login),
            password=self._settings.mt5_password,
            server=self._settings.mt5_server,
        )
        if not ok:
            error = client.last_error()
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"MT5 login() failed: {error}")

        self._mt5 = client
        account = client.account_info()
        logger.success(
            "MT5 connected: login={login} balance={bal} server={server}",
            login=getattr(account, "login", "?"),
            bal=getattr(account, "balance", "?"),
            server=getattr(account, "server", "?"),
        )

    async def disconnect(self) -> None:
        async with self._lock:
            if self._mt5 is not None:
                client = self._mt5
                # Drop the reference *before* awaiting the blocking
                # shutdown so a concurrent caller can't try to reuse
                # a half-closed client.
                self._mt5 = None
                try:
                    await asyncio.to_thread(client.shutdown)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("MT5 shutdown raised: {err}", err=exc)
            self._connected = False
            self._symbol_cache.clear()
            self._resolved_cache.clear()

    async def is_connected(self) -> bool:
        return self._connected and self._mt5 is not None

    # ---- Symbol resolution ----

    async def resolve_symbol(self, requested: str) -> str:
        # Cache hit before grabbing the lock — read of a dict[str, str]
        # is atomic in CPython so a racing miss is harmless.
        cleaned = requested.strip()
        cached = self._resolved_cache.get(cleaned)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._resolved_cache.get(cleaned)
            if cached is not None:
                return cached
            return await asyncio.to_thread(self._sync_resolve_symbol, cleaned)

    def _sync_resolve_symbol(self, requested: str) -> str:
        client = self._require_client()
        candidates = symbol_name_candidates(requested)
        for name in candidates:
            info = client.symbol_info(name)
            if info is None:
                continue
            # Hot the symbol into Market Watch so subsequent tick /
            # order calls don't trip on "not visible".
            client.symbol_select(name, True)
            self._resolved_cache[requested] = name
            return name
        raise ValueError(
            f"unknown symbol on broker: {requested!r}. "
            f"Tried: {candidates}. "
            f"Check the symbol name in MT5 'Market Watch'. Exness mini "
            f"accounts often suffix names (e.g. EURUSDm); other brokers "
            f"use '.s', '.r', '.cash', or '#'."
        )

    # ---- Market data ----

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        # Cache hit fast-path — no lock, no RPC. Reads are atomic in
        # Python so a concurrent miss is harmless: at worst we fetch
        # the same info twice.
        cached = self._symbol_cache.get(symbol)
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._symbol_cache.get(symbol)
            if cached is not None:
                return cached
            info = await asyncio.to_thread(self._sync_get_symbol_info, symbol)
            self._symbol_cache[symbol] = info
            return info

    def _sync_get_symbol_info(self, symbol: str) -> SymbolInfo:
        client = self._require_client()
        # ``symbol_select`` is required at least once per symbol per
        # MT5 session — otherwise tick_value is 0 and the symbol is
        # invisible to ``symbol_info_tick``.
        client.symbol_select(symbol, True)
        info = client.symbol_info(symbol)
        if info is None:
            raise ValueError(
                f"unknown symbol on broker: {symbol!r} ({client.last_error()})"
            )
        # MT5 reports spread as an integer count of points; convert to
        # price units so the rest of the bot can work in absolute
        # spread values without re-querying ``point``.
        point = float(info.point)
        return SymbolInfo(
            symbol=str(info.name),
            digits=int(info.digits),
            point=point,
            trade_tick_size=float(info.trade_tick_size),
            trade_tick_value=float(info.trade_tick_value),
            trade_contract_size=float(info.trade_contract_size),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            trade_stops_level=int(info.trade_stops_level),
            spread_typical=float(info.spread) * point,
        )

    async def get_tick(self, symbol: str) -> Tick:
        async with self._lock:
            return await asyncio.to_thread(self._sync_get_tick, symbol)

    def _sync_get_tick(self, symbol: str) -> Tick:
        client = self._require_client()
        # Make sure the symbol is on Market Watch; this is idempotent.
        client.symbol_select(symbol, True)
        t = client.symbol_info_tick(symbol)
        if t is None:
            raise ValueError(
                f"symbol_info_tick({symbol!r}) returned None: {client.last_error()}"
            )
        # ``time`` is a Unix timestamp in seconds; some brokers also
        # populate ``time_msc`` for millisecond resolution but we
        # only need second precision for the polling cadence.
        return Tick(
            symbol=symbol,
            bid=float(t.bid),
            ask=float(t.ask),
            time=datetime.fromtimestamp(int(t.time), tz=timezone.utc),
        )

    # ---- Account ----

    async def get_account_balance(self) -> float:
        async with self._lock:
            return await asyncio.to_thread(self._sync_account_field, "balance")

    async def get_account_equity(self) -> float:
        async with self._lock:
            return await asyncio.to_thread(self._sync_account_field, "equity")

    async def get_free_margin(self) -> float:
        async with self._lock:
            return await asyncio.to_thread(
                self._sync_account_field, "margin_free"
            )

    def _sync_account_field(self, field: str) -> float:
        client = self._require_client()
        info = client.account_info()
        if info is None:
            raise RuntimeError(f"account_info() returned None: {client.last_error()}")
        return float(getattr(info, field))

    # ---- Orders ----

    async def place_order(self, request: OrderRequest) -> OrderResult:
        async with self._lock:
            return await asyncio.to_thread(self._sync_place_order, request)

    def _sync_place_order(self, request: OrderRequest) -> OrderResult:
        client = self._require_client()
        client.symbol_select(request.symbol, True)

        info = client.symbol_info(request.symbol)
        if info is None:
            return _fail_result(
                code=-1,
                message=f"symbol_info({request.symbol!r}) returned None",
            )
        filling_mask = int(getattr(info, "filling_mode", 0) or 0)

        # Build the MT5 request dictionary. Limits and market orders
        # share most fields but diverge on action, type, price, and
        # filling mode — see ``_build_*_request``.
        if request.order_type == "LIMIT":
            if request.price is None:
                return _fail_result(
                    code=-1, message="LIMIT order requires explicit price"
                )
            req_dict = self._build_limit_request(request)
        elif request.order_type == "MARKET":
            req_dict = self._build_market_request(client, request, filling_mask)
            if req_dict is None:
                return _fail_result(
                    code=-1,
                    message=f"no tick available for {request.symbol!r}",
                )
        else:
            return _fail_result(
                code=-1, message=f"unsupported order_type {request.order_type!r}"
            )

        result = client.order_send(req_dict)
        return _translate_order_send_result(result)

    def _build_limit_request(self, request: OrderRequest) -> dict[str, Any]:
        side_is_buy = request.side == BlockSide.BUY
        order_type = _ORDER_TYPE_BUY_LIMIT if side_is_buy else _ORDER_TYPE_SELL_LIMIT
        req: dict[str, Any] = {
            "action": _TRADE_ACTION_PENDING,
            "symbol": request.symbol,
            "volume": float(request.lot),
            "type": order_type,
            "price": float(request.price),  # type: ignore[arg-type]
            "magic": int(request.magic),
            "comment": (request.client_tag or "")[:_COMMENT_MAX_LEN],
            "type_time": _ORDER_TIME_GTC,
            # RETURN means "leave the order on the book if it can't
            # be filled immediately" — exactly what we want for limit
            # ladder entries. FOK / IOC would cancel the limit on
            # placement if no taker matched.
            "type_filling": _ORDER_FILLING_RETURN,
        }
        if request.sl is not None:
            req["sl"] = float(request.sl)
        if request.tp is not None:
            req["tp"] = float(request.tp)
        return req

    def _build_market_request(
        self,
        client: Any,
        request: OrderRequest,
        filling_mask: int,
    ) -> dict[str, Any] | None:
        side_is_buy = request.side == BlockSide.BUY
        tick = client.symbol_info_tick(request.symbol)
        if tick is None:
            return None
        price = float(tick.ask) if side_is_buy else float(tick.bid)
        order_type = _ORDER_TYPE_BUY if side_is_buy else _ORDER_TYPE_SELL
        req: dict[str, Any] = {
            "action": _TRADE_ACTION_DEAL,
            "symbol": request.symbol,
            "volume": float(request.lot),
            "type": order_type,
            "price": price,
            "deviation": _MARKET_DEVIATION_POINTS,
            "magic": int(request.magic),
            "comment": (request.client_tag or "")[:_COMMENT_MAX_LEN],
            "type_time": _ORDER_TIME_GTC,
            "type_filling": _pick_market_filling(filling_mask),
        }
        if request.sl is not None:
            req["sl"] = float(request.sl)
        if request.tp is not None:
            req["tp"] = float(request.tp)
        return req

    async def modify_position(
        self,
        *,
        position_ticket: str,
        sl: float | None,
        tp: float | None,
    ) -> OrderResult:
        async with self._lock:
            return await asyncio.to_thread(
                self._sync_modify_position, position_ticket, sl, tp
            )

    def _sync_modify_position(
        self,
        position_ticket: str,
        sl: float | None,
        tp: float | None,
    ) -> OrderResult:
        client = self._require_client()
        try:
            ticket_int = int(position_ticket)
        except (TypeError, ValueError):
            return _fail_result(code=-1, message=f"bad ticket: {position_ticket!r}")

        positions = client.positions_get(ticket=ticket_int)
        if not positions:
            return _fail_result(
                code=10025,  # MT5 "position not found"
                message=f"position {position_ticket} not found",
            )
        pos = positions[0]
        req = {
            "action": _TRADE_ACTION_SLTP,
            "position": ticket_int,
            "symbol": str(pos.symbol),
            # MT5 treats 0.0 as "remove this leg"; pass through None
            # as 0.0 to support clearing SL or TP intentionally.
            "sl": float(sl) if sl is not None else 0.0,
            "tp": float(tp) if tp is not None else 0.0,
        }
        result = client.order_send(req)
        translated = _translate_order_send_result(result)
        # MT5 SLTP results don't carry a position ticket; re-attach
        # the one we already know so callers can chain modifications.
        if translated.ok and translated.position_ticket is None:
            translated = OrderResult(
                ok=True,
                ticket=translated.ticket,
                position_ticket=position_ticket,
                filled_price=translated.filled_price,
                error_code=translated.error_code,
                error_message=translated.error_message,
            )
        return translated

    async def cancel_order(self, ticket: str) -> OrderResult:
        async with self._lock:
            return await asyncio.to_thread(self._sync_cancel_order, ticket)

    def _sync_cancel_order(self, ticket: str) -> OrderResult:
        client = self._require_client()
        try:
            ticket_int = int(ticket)
        except (TypeError, ValueError):
            return _fail_result(code=-1, message=f"bad ticket: {ticket!r}")
        req = {
            "action": _TRADE_ACTION_REMOVE,
            "order": ticket_int,
        }
        result = client.order_send(req)
        return _translate_order_send_result(result)

    async def close_position(self, position_ticket: str) -> OrderResult:
        async with self._lock:
            return await asyncio.to_thread(
                self._sync_close_position, position_ticket
            )

    def _sync_close_position(self, position_ticket: str) -> OrderResult:
        client = self._require_client()
        try:
            ticket_int = int(position_ticket)
        except (TypeError, ValueError):
            return _fail_result(code=-1, message=f"bad ticket: {position_ticket!r}")

        positions = client.positions_get(ticket=ticket_int)
        if not positions:
            return _fail_result(
                code=10025,
                message=f"position {position_ticket} not found",
            )
        pos = positions[0]
        symbol = str(pos.symbol)
        side_is_buy = int(pos.type) == _ORDER_TYPE_BUY
        tick = client.symbol_info_tick(symbol)
        if tick is None:
            return _fail_result(code=-1, message=f"no tick for {symbol!r}")
        # Closing direction is opposite to the position side.
        order_type = _ORDER_TYPE_SELL if side_is_buy else _ORDER_TYPE_BUY
        price = float(tick.bid) if side_is_buy else float(tick.ask)

        info = client.symbol_info(symbol)
        filling_mask = int(getattr(info, "filling_mode", 0) or 0) if info else 0

        req = {
            "action": _TRADE_ACTION_DEAL,
            "position": ticket_int,
            "symbol": symbol,
            "volume": float(pos.volume),
            "type": order_type,
            "price": price,
            "deviation": _MARKET_DEVIATION_POINTS,
            "magic": int(pos.magic),
            "comment": "fb-close",
            "type_time": _ORDER_TIME_GTC,
            "type_filling": _pick_market_filling(filling_mask),
        }
        result = client.order_send(req)
        return _translate_order_send_result(result)

    # ---- State sync ----

    async def list_open_positions(
        self, symbol: str | None = None
    ) -> list[Position]:
        async with self._lock:
            return await asyncio.to_thread(self._sync_list_positions, symbol)

    def _sync_list_positions(self, symbol: str | None) -> list[Position]:
        client = self._require_client()
        raw = (
            client.positions_get(symbol=symbol)
            if symbol is not None
            else client.positions_get()
        )
        if not raw:
            return []
        out: list[Position] = []
        for p in raw:
            side = BlockSide.BUY if int(p.type) == _ORDER_TYPE_BUY else BlockSide.SELL
            sl_raw = float(p.sl)
            tp_raw = float(p.tp)
            out.append(
                Position(
                    ticket=str(int(p.ticket)),
                    symbol=str(p.symbol),
                    side=side,
                    lot=float(p.volume),
                    open_price=float(p.price_open),
                    # MT5 uses 0.0 as "no SL/TP set". Surface that as
                    # None so the engine doesn't think a position is
                    # protected when it isn't.
                    sl=sl_raw if sl_raw > 0 else None,
                    tp=tp_raw if tp_raw > 0 else None,
                    profit=float(p.profit),
                    swap=float(p.swap),
                    commission=float(getattr(p, "commission", 0.0) or 0.0),
                    open_time=datetime.fromtimestamp(int(p.time), tz=timezone.utc),
                    comment=str(p.comment or ""),
                )
            )
        return out

    async def list_pending_orders(
        self, symbol: str | None = None
    ) -> list[OrderResult]:
        async with self._lock:
            return await asyncio.to_thread(self._sync_list_pending, symbol)

    async def get_position_close_info(
        self, ticket: str
    ) -> ClosedPositionInfo | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._sync_get_position_close_info, ticket
            )

    def _sync_get_position_close_info(
        self, ticket: str
    ) -> ClosedPositionInfo | None:
        """Query MT5 trade history for a position's closing deal.

        ``history_deals_get(position=N)`` returns *every* deal that
        touched the position — opening, partial fills, and closing.
        The closing deal is identified by ``entry == DEAL_ENTRY_OUT``;
        its ``reason`` field carries the SL/TP code we need.

        Returns ``None`` if no closing deal is found, which the
        engine interprets as "still open or never filled".
        """
        client = self._require_client()
        try:
            ticket_int = int(ticket)
        except (TypeError, ValueError):
            return None

        from datetime import datetime as _dt, timedelta as _td

        # The MT5 binding requires a datetime range. We look back
        # ``_HISTORY_LOOKBACK_DAYS`` (a week) — more than enough for
        # the bot's restart-reconciliation use case and short enough
        # to keep the RPC payload tight.
        to_dt = _dt.utcnow()
        from_dt = to_dt - _td(days=_HISTORY_LOOKBACK_DAYS)

        deals = client.history_deals_get(from_dt, to_dt, position=ticket_int)
        if not deals:
            # Some mt5linux versions return None when the position
            # is still open; others return an empty tuple. Either
            # way the answer is "no close info yet".
            return None

        # Find the closing deal — DEAL_ENTRY_OUT == 1. If there are
        # multiple (partial closes), we take the LAST one because
        # that is the one that took the position to zero volume.
        closing = None
        for d in deals:
            if int(getattr(d, "entry", -1)) == _DEAL_ENTRY_OUT:
                closing = d
        if closing is None:
            return None

        reason_code = int(getattr(closing, "reason", -1))
        if reason_code == _DEAL_REASON_SL:
            reason = "SL"
        elif reason_code == _DEAL_REASON_TP:
            reason = "TP"
        elif reason_code in (0, 1, 2, 3):
            reason = "MANUAL"
        else:
            reason = "OTHER"

        return ClosedPositionInfo(
            ticket=ticket,
            close_price=float(closing.price),
            close_time=datetime.fromtimestamp(
                int(closing.time), tz=timezone.utc
            ),
            profit=float(closing.profit),
            reason=reason,
        )

    def _sync_list_pending(self, symbol: str | None) -> list[OrderResult]:
        client = self._require_client()
        raw = (
            client.orders_get(symbol=symbol)
            if symbol is not None
            else client.orders_get()
        )
        if not raw:
            return []
        out: list[OrderResult] = []
        for o in raw:
            out.append(
                OrderResult(
                    ok=True,
                    ticket=str(int(o.ticket)),
                    position_ticket=None,
                    # ``price_open`` is what the limit will fill at —
                    # we surface it as ``filled_price`` so downstream
                    # consumers don't need a separate field, with the
                    # caveat that the order hasn't actually filled.
                    filled_price=float(o.price_open),
                    error_code=None,
                    error_message=None,
                )
            )
        return out

    # ---- Internals ----

    def _require_client(self) -> Any:
        """Return the RPC client or raise if we are not connected."""
        if self._mt5 is None:
            raise RuntimeError("MT5Adapter is not connected; call connect() first")
        return self._mt5


# ---------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------

def _pick_market_filling(filling_mask: int) -> int:
    """Choose a filling mode for a market deal given the symbol's mask.

    Exness symbols typically advertise FOK + IOC. We prefer IOC so a
    partial fill still produces a position rather than rejecting the
    deal entirely. If the broker advertises neither flag (a few exotic
    symbols), fall back to RETURN, which is the universal compatibility
    choice — better to risk a soft rejection than to send an invalid
    filling mode.
    """
    if filling_mask & _SYMBOL_FILLING_IOC:
        return _ORDER_FILLING_IOC
    if filling_mask & _SYMBOL_FILLING_FOK:
        return _ORDER_FILLING_FOK
    return _ORDER_FILLING_RETURN


def _fail_result(*, code: int, message: str) -> OrderResult:
    """Construct a failed OrderResult with consistent shape."""
    return OrderResult(
        ok=False,
        ticket=None,
        position_ticket=None,
        filled_price=None,
        error_code=code,
        error_message=message,
    )


def _translate_order_send_result(result: Any) -> OrderResult:
    """Convert MT5's ``OrderSendResult`` named tuple into our DTO.

    The struct fields we care about:

    * ``retcode``        — outcome; ``10009``/``10008`` = success.
    * ``order``          — order ticket allocated by the broker.
    * ``deal``           — deal ticket for market-order fills; matches
                           the eventual position ticket on netting
                           accounts and the new position ticket on
                           hedging accounts (used by Exness demos).
    * ``price``          — fill price for immediate deals; 0 for
                           pending placements.
    * ``comment``        — error description on failure.
    """
    if result is None:
        return _fail_result(code=-1, message="order_send returned None")

    retcode = int(result.retcode)
    ok = retcode in _SUCCESS_RETCODES
    order_ticket = int(getattr(result, "order", 0) or 0)
    deal_ticket = int(getattr(result, "deal", 0) or 0)
    price = float(getattr(result, "price", 0.0) or 0.0)

    return OrderResult(
        ok=ok,
        ticket=str(order_ticket) if order_ticket else None,
        # On a market fill, MT5 returns both ``order`` and ``deal``.
        # The deal ticket is the one that matches the eventual
        # position on hedging accounts, so prefer it; fall back to
        # the order ticket if deal is absent (e.g. modifications).
        position_ticket=(
            str(deal_ticket)
            if deal_ticket
            else (str(order_ticket) if (ok and order_ticket) else None)
        ),
        filled_price=price if price > 0 else None,
        error_code=retcode if not ok else None,
        error_message=(
            None if ok else _format_error_message(result, retcode)
        ),
    )


def _format_error_message(result: Any, retcode: int) -> str:
    """Build the human-readable error message attached to a failed result.

    Combines the broker's terse ``comment`` field (always present)
    with an operator-actionable remediation hint from
    :data:`_RETCODE_HINTS` when available. Surfaces the retcode as
    a numeric fallback when both are empty.

    The Telegram notifier formats this as a single line per the
    existing engine convention; the hint is short enough to fit
    comfortably alongside the broker comment.
    """
    comment = str(getattr(result, "comment", "") or "").strip()
    hint = _RETCODE_HINTS.get(retcode)

    if comment and hint:
        return f"{retcode} {comment} — {hint}"
    if comment:
        return f"{retcode} {comment}"
    if hint:
        return f"{retcode}: {hint}"
    return f"retcode={retcode}"
