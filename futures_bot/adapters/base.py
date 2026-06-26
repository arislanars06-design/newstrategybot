"""Broker-adapter interface.

The engine and the order-watcher talk to brokers through this abstract
class, never directly. Two implementations live alongside it:

* :class:`MockAdapter` — fakes MT5 responses for unit tests and the
  bot-without-broker development phase.
* :class:`MT5Adapter` — drives a real MT5 terminal over the
  ``mt5linux`` RPC bridge running in the Wine container.

The dataclasses in this module are the lingua franca between the two:
adapters convert their broker-specific responses into these shapes,
and the rest of the bot never imports anything broker-specific. This
keeps the strategy/engine code testable and lets us add other
brokers (cTrader, IBKR) later by adding a third adapter file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from futures_bot.db.enums import BlockSide


# ---------------------------------------------------------------------
# Symbol-name resolution helper (shared by every adapter)
# ---------------------------------------------------------------------

# Suffixes the major retail MT5 brokers append to base symbol names.
# Stored in the exact case the brokers ship them — MT5's symbol
# lookup is case-sensitive ('EURUSDm' is a different name from
# 'EURUSDM') so we must preserve the conventional casing.
_BROKER_SUFFIXES: tuple[str, ...] = (
    "m",        # Exness Standard Mini, Pepperstone Razor mini
    ".s",       # IC Markets Standard, FP Markets Pro
    ".r",       # raw-spread / ECN flavours
    ".cash",    # cash-settled indices (Pepperstone, OANDA)
    "#",        # XM and some white-label MT5
    ".pro",     # ECN-pro variants
    "_i",       # institutional variants
)


def symbol_name_candidates(requested: str) -> list[str]:
    """All plausible broker names to try for one user-supplied symbol.

    The brokers in the retail MT5 ecosystem (Exness, IC Markets,
    Pepperstone, XM, FP Markets) expose the same instrument under
    a small set of suffixed variants. Rather than make the trader
    learn each broker's convention we generate every reasonable
    candidate and let the adapter probe ``symbol_info`` in order.

    Properties of the returned list:

    * **Stable order.** Exact input first (lets a trader who already
      knows the broker name short-circuit the search), then uppercase
      base, then each suffix appended in :data:`_BROKER_SUFFIXES`
      order — that order is the prior probability of running into
      each broker, biggest first.
    * **Suffix-aware stripping.** If the input already ends in a
      known suffix (case-insensitive) we strip it before generating
      variants, so a user who typed ``EURUSDm`` still reaches plain
      ``EURUSD`` and the other suffixes.
    * **De-duped.** No name appears twice even when multiple rules
      would emit it.
    """
    out: list[str] = []

    def push(name: str) -> None:
        if name and name not in out:
            out.append(name)

    cleaned = requested.strip()
    push(cleaned)                            # exactly what the user typed
    push(cleaned.upper())                    # common lower-case input case

    # Determine the bare base, stripping any known suffix the user
    # may have included themselves.
    upper = cleaned.upper()
    base = upper
    for suf in _BROKER_SUFFIXES:
        if upper.endswith(suf.upper()):
            base = upper[: -len(suf)]
            break

    push(base)
    for suf in _BROKER_SUFFIXES:
        push(f"{base}{suf}")

    return out


# ---------------------------------------------------------------------
# DTOs (data transfer objects)
# ---------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class SymbolInfo:
    """Static-ish metadata about a tradable instrument.

    Fetched once when a symbol enters the watch-list and cached
    until the bot restarts. Field names match the MT5
    ``SymbolInfo`` struct so the MT5 adapter is a straight copy.
    """

    symbol: str
    digits: int                 # number of fractional digits in the price
    point: float                # smallest meaningful price unit (= 10**-digits)
    trade_tick_size: float      # smallest price increment for orders
    trade_tick_value: float     # USD P&L per tick on 1.0 lot
    trade_contract_size: float  # base units per 1.0 lot (e.g. 100 for gold)
    volume_min: float
    volume_max: float
    volume_step: float
    trade_stops_level: int      # minimum price ticks between order and SL/TP
    spread_typical: float       # broker's advertised typical spread in price units


@dataclass(slots=True, frozen=True)
class Tick:
    """Snapshot of the live quote."""

    symbol: str
    bid: float
    ask: float
    time: datetime              # broker timestamp, UTC

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(slots=True, frozen=True)
class Position:
    """Open position on the broker, as the adapter sees it."""

    ticket: str
    symbol: str
    side: BlockSide
    lot: float
    open_price: float
    sl: float | None
    tp: float | None
    profit: float               # current unrealised P&L (USD)
    swap: float
    commission: float
    open_time: datetime
    comment: str


@dataclass(slots=True, frozen=True)
class OrderRequest:
    """What we want the broker to do for one rung.

    Used for both pending limit orders (entry placement) and for
    SL/TP modifications. ``client_tag`` is the bot-side idempotency
    key written into MT5's ``comment`` field and used to recover
    state on restart.
    """

    symbol: str
    side: BlockSide
    order_type: str             # "LIMIT" | "MARKET"
    price: float | None         # None means market order
    lot: float
    sl: float | None = None
    tp: float | None = None
    client_tag: str = ""        # written into MT5 comment
    magic: int = 0              # MT5 magic-number; we use one per block


@dataclass(slots=True, frozen=True)
class OrderResult:
    """What the broker said back to an :class:`OrderRequest`.

    ``ticket`` is the order ticket immediately after submission. Once
    a limit fills, the broker assigns a separate *position* ticket
    that we have to track separately — adapters populate
    ``position_ticket`` whenever the response carries one (market
    orders, immediate fills, post-fill modifications).
    """

    ok: bool
    ticket: str | None
    position_ticket: str | None
    filled_price: float | None
    error_code: int | None
    error_message: str | None


# ---------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class ClosedPositionInfo:
    """Outcome of a position that has already left the broker's books.

    Returned by :meth:`BrokerAdapter.get_position_close_info` so the
    reconciliation pass on bot restart can attribute a closed
    position to SL, TP, or a manual / unusual close — without
    guessing from prices alone (a position closed exactly at SL by
    a third-party tool would otherwise be indistinguishable from
    a manual close).

    ``reason`` is a normalised string instead of the MT5 numeric
    code so the engine, formatters, and tests don't have to import
    broker-specific constants.
    """

    ticket: str
    close_price: float
    close_time: datetime
    profit: float           # broker-reported, includes commissions
    reason: str             # one of: "SL", "TP", "MANUAL", "OTHER"


class BrokerAdapter(ABC):
    """Common surface every concrete broker adapter must implement.

    All methods are coroutines because the production MT5 adapter
    talks over RPC and the engine runs on asyncio. The mock adapter
    is async too so its callers don't need to special-case it.

    Adapters MUST be safe to call concurrently from multiple
    coroutines. The MT5 RPC bridge is single-threaded, so the MT5
    adapter serialises requests internally with a lock; the mock
    adapter is naturally thread-safe because it doesn't share state
    with anything external.
    """

    # ---- Lifecycle ----

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection to the broker. Idempotent."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection cleanly. Safe to call multiple times."""

    @abstractmethod
    async def is_connected(self) -> bool:
        """Cheap health probe used by the engine before sending orders."""

    # ---- Market data ----

    @abstractmethod
    async def resolve_symbol(self, requested: str) -> str:
        """Translate a user-typed symbol to the broker's actual name.

        Different brokers expose the same instrument under different
        names — Exness mini accounts append ``m``, some IC Markets
        accounts use ``.s`` for raw spread, Pepperstone uses ``.cash``
        for indices. Adapters try the requested name exactly first,
        then a list of common variants, and return the first one the
        broker recognises.

        Raises :class:`ValueError` with the tried list if no variant
        is recognised; never silently falls back to a placeholder so
        the caller can surface the real name to the trader.

        Result MUST be the exact name that subsequent
        :meth:`get_symbol_info` / :meth:`get_tick` / order calls will
        accept — callers store the returned string on the block and
        reuse it for the rest of the lifecycle.
        """

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """Return the static metadata for ``symbol``.

        Raises ``ValueError`` if the broker doesn't know the symbol —
        adapters MUST NOT silently return placeholder data.
        """

    @abstractmethod
    async def get_tick(self, symbol: str) -> Tick:
        """Return the most recent bid/ask snapshot."""

    # ---- Account ----

    @abstractmethod
    async def get_account_balance(self) -> float:
        """Account balance in USD (or quote-currency of the account)."""

    @abstractmethod
    async def get_account_equity(self) -> float:
        """Account equity (balance + unrealised P&L)."""

    @abstractmethod
    async def get_free_margin(self) -> float:
        """Margin currently available for new positions."""

    # ---- Order management ----

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Submit a new order (limit or market)."""

    @abstractmethod
    async def modify_position(
        self,
        *,
        position_ticket: str,
        sl: float | None,
        tp: float | None,
    ) -> OrderResult:
        """Attach or update SL/TP on an open position."""

    @abstractmethod
    async def cancel_order(self, ticket: str) -> OrderResult:
        """Cancel a still-pending order by its ticket."""

    @abstractmethod
    async def close_position(self, position_ticket: str) -> OrderResult:
        """Force-close an open position at market price."""

    # ---- State sync ----

    @abstractmethod
    async def list_open_positions(
        self, symbol: str | None = None
    ) -> list[Position]:
        """All currently open positions, optionally filtered by symbol."""

    @abstractmethod
    async def list_pending_orders(
        self, symbol: str | None = None
    ) -> list[OrderResult]:
        """All still-pending (not yet filled, not yet cancelled) orders.

        We re-use :class:`OrderResult` here because every field
        we need (ticket, price, status) is already on it. Adapter
        implementations fill in ``filled_price=None`` for genuinely
        pending orders.
        """

    @abstractmethod
    async def get_position_close_info(
        self, ticket: str
    ) -> "ClosedPositionInfo | None":
        """Look up a closed position by its ticket.

        Returns ``None`` when no matching closing deal exists in
        the broker's history — interpret that as "the position is
        still open, or the ticket never produced a position (e.g.
        the pending order was cancelled before fill)".

        Used by :meth:`BlockEngine.reconcile_open_blocks` on
        startup to attribute closed positions to SL / TP / manual
        close. The MT5 implementation queries the trade history
        via ``history_deals_get(position=ticket)`` and inspects
        the exit deal's ``reason`` field; the mock keeps an
        in-memory ledger of closures for the tests.
        """
