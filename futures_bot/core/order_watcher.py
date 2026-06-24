"""Tick polling loop.

The futures bot drives all of its state transitions from
:meth:`BlockEngine.on_tick`. With a real broker we have to deliver
those ticks ourselves — MetaTrader 5 over ``mt5linux`` doesn't push
ticks to clients, it answers a poll. This module owns the polling
loop:

* Every ``settings.tick_poll_interval_ms`` we ask the broker for the
  current quote on every symbol that has at least one non-terminal
  block.
* Each fresh tick is handed to ``engine.on_tick(tick)`` which runs
  the fill / SL / TP / cancel-price detection.
* The set of "watched symbols" is re-derived from the DB on every
  iteration so newly-created blocks start being watched in at most
  one poll cycle and closed blocks stop wasting RPC budget.

The loop is intentionally defensive: any per-symbol exception is
swallowed (logged) so a single bad symbol or a transient RPC error
can't take the whole bot down. Only an unrecoverable error on the
loop's own bookkeeping is allowed to propagate — and even then we
relaunch the loop after a short backoff.

Lifecycle is symmetric to aiogram's dispatcher: build, ``start``,
``stop``. ``start`` is non-blocking; ``stop`` awaits the loop task
to finish so the outer ``main`` can rely on it being fully drained
before shutting down the adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from loguru import logger

from futures_bot.adapters.base import BrokerAdapter
from futures_bot.config import Settings
from futures_bot.core.engine import BlockEngine
from futures_bot.db import repository, session_scope


# Minimum polling cadence. Even if the operator sets a very small
# tick_poll_interval_ms (e.g. 50), we never poll faster than this —
# the mt5linux RPC bridge is a single-threaded TCP service and
# hammering it past ~20 req/s starts queueing replies.
_MIN_POLL_INTERVAL_MS = 100

# When the broker is unhealthy and the whole loop body raises, wait
# this long before restarting the loop. Prevents a busy crash-loop
# spamming the broker.
_LOOP_BACKOFF_SECONDS = 5.0


class TickWatcher:
    """Polls active symbols and feeds ticks into :class:`BlockEngine`.

    A single instance owns one asyncio Task; ``start`` is idempotent
    (calling it again is a no-op) so tests and restarts don't double-
    schedule the loop. The task only exits cleanly via :meth:`stop`
    or process shutdown — internal exceptions are logged and the
    loop retries.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        adapter: BrokerAdapter,
        engine: BlockEngine,
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._engine = engine

        # Sanity-clamp the interval so an envar typo can't DoS the
        # broker. Convert to seconds once so the hot loop doesn't.
        interval_ms = max(
            _MIN_POLL_INTERVAL_MS, int(settings.tick_poll_interval_ms)
        )
        self._interval_s = interval_ms / 1000.0

        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    # ---- Lifecycle ----

    def start(self) -> None:
        """Schedule the polling loop. Safe to call multiple times."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_supervised(), name="futures-bot.tick-watcher"
        )
        logger.info(
            "TickWatcher started (interval={ms}ms)",
            ms=int(self._interval_s * 1000),
        )

    async def stop(self) -> None:
        """Signal the loop to exit and await its completion.

        Setting ``_stop_event`` lets the loop finish its current
        iteration cleanly. We also cancel the task as a fallback so a
        loop body stuck inside an RPC call doesn't hold up shutdown
        forever.
        """
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            # Swallow on shutdown — we logged any interesting errors
            # from inside the loop already.
            pass
        logger.info("TickWatcher stopped")

    # ---- Loop body ----

    async def _run_supervised(self) -> None:
        """Outer loop: restart the inner loop on unhandled errors.

        ``_run_once`` shouldn't itself raise because every per-symbol
        operation is wrapped in try/except, but if it does (e.g. the
        DB session blew up) we want the bot to keep ticking after a
        short pause rather than going silent.
        """
        while not self._stop_event.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "TickWatcher main loop crashed: {err}; restarting in {s}s",
                    err=exc,
                    s=_LOOP_BACKOFF_SECONDS,
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=_LOOP_BACKOFF_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Expected — no stop in flight, just back off.
                    pass

    async def _run_once(self) -> None:
        """One uninterrupted run of the polling loop."""
        while not self._stop_event.is_set():
            symbols = await self._watched_symbols()

            # No active blocks → idle, but still wake up so newly
            # created blocks get picked up within one poll cycle.
            if symbols:
                await self._poll_symbols(symbols)

            try:
                # Sleep that wakes up early if someone calls stop().
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_s,
                )
                # Event fired — loop condition will exit on next check.
            except asyncio.TimeoutError:
                # Normal cadence path.
                continue

    async def _poll_symbols(self, symbols: Iterable[str]) -> None:
        """Fetch a tick per symbol and deliver it to the engine.

        Errors are isolated to each symbol so one stale instrument
        doesn't starve the others. Symbols are processed sequentially
        because :class:`MT5Adapter` serialises all RPC calls anyway
        — running them concurrently would just queue inside the lock.
        """
        for symbol in symbols:
            if self._stop_event.is_set():
                return
            try:
                tick = await self._adapter.get_tick(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "get_tick({sym}) failed: {err}", sym=symbol, err=exc
                )
                continue

            try:
                await self._engine.on_tick(tick)
            except Exception as exc:  # noqa: BLE001
                # An engine-side bug shouldn't kill the watcher; log
                # loudly so the operator notices, then carry on.
                logger.exception(
                    "engine.on_tick({sym}) failed: {err}",
                    sym=symbol,
                    err=exc,
                )

    async def _watched_symbols(self) -> list[str]:
        """De-duplicate the set of symbols across all active blocks.

        Cheap in normal operation: list_active_blocks selects at most
        a few rows from a small table. Re-querying on every iteration
        means newly created blocks are picked up in O(one poll) and
        terminal blocks drop off the watchlist immediately, with no
        manual cache invalidation.
        """
        async with session_scope() as session:
            blocks = await repository.list_active_blocks(session)
        # Preserve first-seen order so logs look deterministic.
        seen: list[str] = []
        seen_set: set[str] = set()
        for b in blocks:
            if b.symbol not in seen_set:
                seen.append(b.symbol)
                seen_set.add(b.symbol)
        return seen


__all__ = ["TickWatcher"]
