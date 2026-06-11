"""User-supplied specification for a new block, with validation."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.db.enums import BlockSide

EXPECTED_ORDERS_PER_BLOCK = 8


@dataclass(slots=True)
class OrderSpec:
    """One rung of the block ladder."""

    seq: int           # 1..8
    entry_price: float
    tp_price: float
    sl_price: float
    qty: float         # base-asset quantity (e.g. BTC)


@dataclass(slots=True)
class BlockPlan:
    """Everything required to create a block, before it is persisted.

    Validation is intentionally pure: no exchange calls, no DB. The
    engine validates the plan first and rejects it cheaply if anything
    is wrong with the user's input.
    """

    symbol: str
    side: BlockSide
    cancel_price: float
    orders: list[OrderSpec] = field(default_factory=list)
    note: str | None = None

    # ---- Validation ----

    def validate(self) -> None:
        """Raise ``ValueError`` with a descriptive message on any problem."""
        if not self.symbol or not self.symbol.isupper():
            raise ValueError("symbol must be a non-empty uppercase ticker (e.g. BTCUSDT)")

        if len(self.orders) != EXPECTED_ORDERS_PER_BLOCK:
            raise ValueError(
                f"block must have exactly {EXPECTED_ORDERS_PER_BLOCK} orders, "
                f"got {len(self.orders)}"
            )

        seqs = [o.seq for o in self.orders]
        if seqs != list(range(1, EXPECTED_ORDERS_PER_BLOCK + 1)):
            raise ValueError("orders must have seq=1..8 in order")

        for o in self.orders:
            if o.qty <= 0:
                raise ValueError(f"order {o.seq}: qty must be > 0")
            if o.entry_price <= 0 or o.tp_price <= 0 or o.sl_price <= 0:
                raise ValueError(f"order {o.seq}: all prices must be positive")

        if self.side == BlockSide.BUY:
            self._validate_buy()
        elif self.side == BlockSide.SELL:
            self._validate_sell()
        else:  # pragma: no cover — enum guarantees coverage
            raise ValueError(f"unknown side: {self.side!r}")

    def _validate_buy(self) -> None:
        # BUY (long) block: entries descend (100, 99, 98, ...).
        # Each TP is above its entry, each SL below.
        # Cancel price is above all entries.
        prev: float | None = None
        for o in self.orders:
            if prev is not None and o.entry_price >= prev:
                raise ValueError(
                    f"BUY block entries must descend (order {o.seq}: "
                    f"{o.entry_price} >= previous {prev})"
                )
            prev = o.entry_price
            if o.tp_price <= o.entry_price:
                raise ValueError(
                    f"BUY order {o.seq}: TP ({o.tp_price}) must be > entry ({o.entry_price})"
                )
            if o.sl_price >= o.entry_price:
                raise ValueError(
                    f"BUY order {o.seq}: SL ({o.sl_price}) must be < entry ({o.entry_price})"
                )
        if self.cancel_price <= self.orders[0].entry_price:
            raise ValueError(
                f"BUY block: cancel_price ({self.cancel_price}) must be > "
                f"first entry ({self.orders[0].entry_price})"
            )

    def _validate_sell(self) -> None:
        # SELL (short) block: entries ascend.
        # Each TP below entry, each SL above.
        # Cancel price below all entries.
        prev: float | None = None
        for o in self.orders:
            if prev is not None and o.entry_price <= prev:
                raise ValueError(
                    f"SELL block entries must ascend (order {o.seq}: "
                    f"{o.entry_price} <= previous {prev})"
                )
            prev = o.entry_price
            if o.tp_price >= o.entry_price:
                raise ValueError(
                    f"SELL order {o.seq}: TP ({o.tp_price}) must be < entry ({o.entry_price})"
                )
            if o.sl_price <= o.entry_price:
                raise ValueError(
                    f"SELL order {o.seq}: SL ({o.sl_price}) must be > entry ({o.entry_price})"
                )
        if self.cancel_price >= self.orders[0].entry_price:
            raise ValueError(
                f"SELL block: cancel_price ({self.cancel_price}) must be < "
                f"first entry ({self.orders[0].entry_price})"
            )

    # ---- Convenience builders ----

    @classmethod
    def with_chained_sl(
        cls,
        *,
        symbol: str,
        side: BlockSide,
        entries: list[float],
        tps: list[float],
        last_sl: float,
        qty: float,
        cancel_price: float,
        note: str | None = None,
    ) -> BlockPlan:
        """Build a plan where each SL is set to the next entry (the chain rule).

        ``last_sl`` is the SL for the final order (no successor entry).
        """
        if len(entries) != EXPECTED_ORDERS_PER_BLOCK or len(tps) != EXPECTED_ORDERS_PER_BLOCK:
            raise ValueError(f"need exactly {EXPECTED_ORDERS_PER_BLOCK} entries and tps")

        orders: list[OrderSpec] = []
        for i, (entry, tp) in enumerate(zip(entries, tps, strict=True)):
            sl = entries[i + 1] if i + 1 < len(entries) else last_sl
            orders.append(
                OrderSpec(
                    seq=i + 1,
                    entry_price=entry,
                    tp_price=tp,
                    sl_price=sl,
                    qty=qty,
                )
            )
        return cls(
            symbol=symbol,
            side=side,
            cancel_price=cancel_price,
            orders=orders,
            note=note,
        )
