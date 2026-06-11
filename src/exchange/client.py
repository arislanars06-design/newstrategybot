"""Thin async wrapper around python-binance's Futures REST client.

Exposes only the operations the engine needs, hides Binance's many
parameter names and string conventions, and centralises rounding via
cached symbol filters.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from binance import AsyncClient
from binance.exceptions import BinanceAPIException
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import Settings
from src.exchange.normalize import format_decimal, round_to_step
from src.exchange.types import SymbolFilters

# Side / positionSide constants mirrored from Binance docs to avoid magic strings.
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
POSITION_LONG = "LONG"
POSITION_SHORT = "SHORT"

ORDER_TYPE_LIMIT = "LIMIT"
ORDER_TYPE_STOP_MARKET = "STOP_MARKET"
TIME_IN_FORCE_GTC = "GTC"
WORKING_TYPE_MARK = "MARK_PRICE"


class BinanceClient:
    """High-level wrapper around the Binance USDT-M Futures API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncClient | None = None
        self._symbol_filters: dict[str, SymbolFilters] = {}

    # ----- Lifecycle -----

    async def start(self) -> None:
        """Create the underlying client and load exchange info."""
        self._client = await AsyncClient.create(
            api_key=self._settings.binance_api_key,
            api_secret=self._settings.binance_api_secret,
            testnet=self._settings.binance_testnet,
        )
        await self._load_exchange_info()
        logger.info(
            "BinanceClient started (testnet={testnet}, symbols_cached={n})",
            testnet=self._settings.binance_testnet,
            n=len(self._symbol_filters),
        )

    async def stop(self) -> None:
        """Close the underlying HTTP session."""
        if self._client is not None:
            await self._client.close_connection()
            self._client = None

    @property
    def raw(self) -> AsyncClient:
        """Access the underlying python-binance AsyncClient (used by streams)."""
        if self._client is None:
            raise RuntimeError("BinanceClient not started")
        return self._client

    # ----- Exchange info / precision -----

    async def _load_exchange_info(self) -> None:
        assert self._client is not None
        info: dict[str, Any] = await self._client.futures_exchange_info()
        cache: dict[str, SymbolFilters] = {}
        for sym in info.get("symbols", []):
            symbol = sym["symbol"]
            filters = {f["filterType"]: f for f in sym.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            min_notional = filters.get("MIN_NOTIONAL", {}) or filters.get(
                "NOTIONAL", {}
            )
            cache[symbol] = SymbolFilters(
                symbol=symbol,
                tick_size=Decimal(price_filter.get("tickSize", "0")),
                step_size=Decimal(lot_filter.get("stepSize", "0")),
                min_qty=Decimal(lot_filter.get("minQty", "0")),
                min_notional=Decimal(min_notional.get("notional", "0") or "0"),
                price_precision=int(sym.get("pricePrecision", 2)),
                quantity_precision=int(sym.get("quantityPrecision", 3)),
            )
        self._symbol_filters = cache

    def get_filters(self, symbol: str) -> SymbolFilters:
        """Return cached filters for a symbol; raises if unknown."""
        if symbol not in self._symbol_filters:
            raise KeyError(f"Symbol {symbol!r} not found in exchange info")
        return self._symbol_filters[symbol]

    def normalize_price(self, symbol: str, price: float | Decimal | str) -> str:
        """Round a price to the symbol tick size and format it as a string."""
        filt = self.get_filters(symbol)
        rounded = round_to_step(price, filt.tick_size)
        return format_decimal(rounded, filt.price_precision)

    def normalize_qty(self, symbol: str, qty: float | Decimal | str) -> str:
        """Round a quantity to the symbol step size and format it as a string."""
        filt = self.get_filters(symbol)
        rounded = round_to_step(qty, filt.step_size)
        return format_decimal(rounded, filt.quantity_precision)

    # ----- Account / position mode -----

    async def get_balance_usdt(self) -> float:
        """Return the wallet balance for USDT (or USDC fallback)."""
        assert self._client is not None
        balances: list[dict[str, Any]] = await self._client.futures_account_balance()
        for entry in balances:
            if entry.get("asset") == "USDT":
                return float(entry.get("balance", 0))
        return 0.0

    async def ensure_hedge_mode(self) -> bool:
        """Enable Hedge Mode (dual-side positions) if not already active.

        Returns True if hedge mode is active after the call. If the
        exchange refuses because there are existing open orders or
        positions (-4067), we log a clear message instructing the user
        how to recover and return False without attempting again.
        """
        assert self._client is not None
        current = await self._client.futures_get_position_mode()
        if current.get("dualSidePosition"):
            return True
        try:
            await self._client.futures_change_position_mode(dualSidePosition="true")
        except BinanceAPIException as exc:
            # -4059: "No need to change position side" — already in hedge mode.
            if exc.code == -4059:
                return True
            # -4067: open orders / positions block the change.
            if exc.code == -4067:
                logger.error(
                    "Cannot enable Hedge Mode: existing open orders or "
                    "positions block the change. Cancel all open orders and "
                    "close all positions in the Binance Futures UI, or "
                    "switch Position Mode to Hedge manually, then restart "
                    "the bot."
                )
                return False
            # Convert the exception to a plain string so loguru does not
            # try to pickle the underlying aiohttp response object.
            logger.error("Failed to enable hedge mode: {msg}", msg=str(exc))
            return False
        return True

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Adjust isolated/cross leverage for a symbol."""
        assert self._client is not None
        await self._client.futures_change_leverage(symbol=symbol, leverage=leverage)

    async def get_position(self, symbol: str, position_side: str) -> dict[str, Any] | None:
        """Return the position dict for a given symbol+side, or None."""
        assert self._client is not None
        positions = await self._client.futures_position_information(symbol=symbol)
        for p in positions:
            if p.get("positionSide") == position_side:
                return p
        return None

    # ----- Order placement -----

    @retry(
        retry=retry_if_exception_type(BinanceAPIException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        reraise=True,
    )
    async def place_entry_limit(
        self,
        *,
        symbol: str,
        side: str,           # "BUY" for long block, "SELL" for short block
        position_side: str,  # "LONG" / "SHORT"
        qty: float | Decimal | str,
        price: float | Decimal | str,
        client_id: str,
    ) -> str:
        """Place a GTC LIMIT order that opens or adds to a position."""
        assert self._client is not None
        order = await self._client.futures_create_order(
            symbol=symbol,
            side=side,
            positionSide=position_side,
            type=ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=self.normalize_qty(symbol, qty),
            price=self.normalize_price(symbol, price),
            newClientOrderId=client_id,
        )
        return str(order["orderId"])

    @retry(
        retry=retry_if_exception_type(BinanceAPIException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        reraise=True,
    )
    async def place_tp_limit(
        self,
        *,
        symbol: str,
        side: str,           # opposite of entry side
        position_side: str,  # same as entry positionSide
        qty: float | Decimal | str,
        price: float | Decimal | str,
        client_id: str,
    ) -> str:
        """Place a take-profit LIMIT order that reduces the open position.

        In hedge mode, ``side`` opposite of ``position_side`` automatically
        means the order is reducing rather than opening — no reduceOnly
        flag needed.
        """
        assert self._client is not None
        order = await self._client.futures_create_order(
            symbol=symbol,
            side=side,
            positionSide=position_side,
            type=ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=self.normalize_qty(symbol, qty),
            price=self.normalize_price(symbol, price),
            newClientOrderId=client_id,
        )
        return str(order["orderId"])

    @retry(
        retry=retry_if_exception_type(BinanceAPIException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        reraise=True,
    )
    async def place_sl_stop(
        self,
        *,
        symbol: str,
        side: str,           # opposite of entry side
        position_side: str,
        qty: float | Decimal | str,
        stop_price: float | Decimal | str,
        client_id: str,
    ) -> str:
        """Place a STOP_MARKET order that closes the position at the stop price."""
        assert self._client is not None
        order = await self._client.futures_create_order(
            symbol=symbol,
            side=side,
            positionSide=position_side,
            type=ORDER_TYPE_STOP_MARKET,
            quantity=self.normalize_qty(symbol, qty),
            stopPrice=self.normalize_price(symbol, stop_price),
            workingType=WORKING_TYPE_MARK,
            newClientOrderId=client_id,
        )
        return str(order["orderId"])

    # ----- Order cancellation -----

    async def cancel_order_by_client_id(self, symbol: str, client_id: str) -> bool:
        """Cancel an order by its client order ID. Returns False if not found."""
        assert self._client is not None
        try:
            await self._client.futures_cancel_order(
                symbol=symbol, origClientOrderId=client_id
            )
            return True
        except BinanceAPIException as exc:
            # -2011 = "Unknown order sent" — already filled or cancelled.
            if exc.code == -2011:
                logger.debug(
                    "cancel_order: {cid} already gone ({msg})", cid=client_id, msg=exc.message
                )
                return False
            raise

    async def cancel_all_for_symbol(self, symbol: str) -> None:
        """Defensive: cancel every open order for a symbol.

        Used when a block enters a terminal state to make sure no stray
        orders remain (e.g. after a partial-failure during placement).
        """
        assert self._client is not None
        try:
            await self._client.futures_cancel_all_open_orders(symbol=symbol)
        except BinanceAPIException as exc:
            logger.warning(
                "cancel_all_for_symbol({sym}) failed: {err}", sym=symbol, err=exc
            )

    # ----- Mark price (one-shot) -----

    async def get_mark_price(self, symbol: str) -> float:
        """Return the current mark price for a symbol."""
        assert self._client is not None
        data = await self._client.futures_mark_price(symbol=symbol)
        return float(data["markPrice"])
