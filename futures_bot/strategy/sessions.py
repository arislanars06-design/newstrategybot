"""Trading-session metadata and per-session instrument recommendations.

The bot has a session-filter (``futures_bot/core/session_filter.py``)
that answers "may we open a block right now?" — weekend / blackout
gating. This module is the educational counterpart that the
Telegram UI surfaces to the trader: "which sessions are open *now*
and which instruments do those sessions favour?".

Why separate?

* ``session_filter`` is a gate the engine checks before every block
  creation. It must stay tight and side-effect-free.
* ``sessions`` is presentation data — a Russian-labelled mapping of
  human session names to UTC windows and recommended symbols. Pure
  data + a couple of helpers; the Telegram handler renders it.

The session windows are the textbook retail FX clock — the bot is
not a professional market-maker, so we don't model regional bank
holidays here. The trader gets a "Лондон + Нью-Йорк" answer; if
they want anything finer, they fall back to the symbol picker and
their own judgement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class TradingSession(StrEnum):
    """Named retail-FX session windows.

    ``LONDON_NY_OVERLAP`` is the four-hour intersection of the
    London and New York sessions — the most liquid block of the day
    and the one the strategy gets the most useful range from. It's
    a separate enum entry rather than a virtual flag so the
    recommended-symbol table can carry its own (smaller, more
    focused) curated list for that window.
    """

    SYDNEY = "SYDNEY"
    TOKYO = "TOKYO"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    LONDON_NY_OVERLAP = "LONDON_NY_OVERLAP"


@dataclass(slots=True, frozen=True)
class SessionWindow:
    """One named session with its open/close hours in UTC.

    Hours use the 24-hour clock (``hour`` field of a ``datetime``).
    A window may wrap across midnight (Sydney runs 21:00–06:00 UTC);
    :func:`_hour_in_window` handles the wrap so callers don't have to.
    """

    session: TradingSession
    start_hour: int        # inclusive, UTC
    end_hour: int          # exclusive, UTC
    label: str             # human-readable Russian short label


# Retail-FX session windows (UTC). End hour is exclusive so the
# windows compose cleanly — Tokyo runs until 08:00 UTC exclusive,
# London starts at 07:00, the one-hour overlap is intentional.
SESSION_WINDOWS: tuple[SessionWindow, ...] = (
    SessionWindow(TradingSession.SYDNEY,   21, 6,  "Сидней"),
    SessionWindow(TradingSession.TOKYO,    23, 8,  "Токио"),
    SessionWindow(TradingSession.LONDON,    7, 16, "Лондон"),
    SessionWindow(TradingSession.NEW_YORK, 12, 21, "Нью-Йорк"),
)

# The flagship overlap — London + NY active simultaneously. Highest
# volume block of the day; the strategy's chain math gets the cleanest
# fills in this window because spreads are tight and ranges are wide.
OVERLAP_WINDOW: SessionWindow = SessionWindow(
    TradingSession.LONDON_NY_OVERLAP, 12, 16, "Лондон + Нью-Йорк"
)


# Recommended instruments per session.
#
# Two sources of truth shape these lists:
# 1. Where each currency's central bank / equity exchange sits.
#    GBP and EUR are most active during London hours; USD news lands
#    during NY; JPY through Tokyo. Trading those during their
#    home-session gives you the day's range and not just the dribble.
# 2. Where the strategy actually works — Fibonacci grids need decent
#    intraday range. Symbols we'd flag as "tier D" elsewhere don't
#    get a free pass into a session's recommendation list just
#    because they're regionally active.
RECOMMENDED_BY_SESSION: dict[TradingSession, tuple[str, ...]] = {
    # Lighter volume; AUD/NZD focus before Tokyo wakes up.
    TradingSession.SYDNEY: (
        "AUDUSD", "NZDUSD", "AUDJPY", "NZDJPY", "AUDNZD",
    ),
    # JPY pairs dominate; carry trades and cross-yen flows.
    TradingSession.TOKYO: (
        "USDJPY", "AUDJPY", "GBPJPY", "EURJPY",
        "NZDJPY", "CADJPY", "CHFJPY",
    ),
    # Highest liquidity hours for EUR/GBP/CHF. Gold also picks up
    # decisively once London opens because the LBMA fixings happen here.
    TradingSession.LONDON: (
        "XAUUSD", "GBPUSD", "GBPJPY", "GBPAUD", "GBPCAD",
        "EURUSD", "EURJPY", "EURAUD", "EURGBP",
        "USDCHF", "USDCAD",
    ),
    # USD news drops, indices and crypto get their main session;
    # gold often retests London ranges during the early NY hours.
    TradingSession.NEW_YORK: (
        "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF",
        "XAUUSD", "XAGUSD",
        "BTCUSD", "ETHUSD",
        "US30", "US500", "NAS100",
    ),
    # The 12:00-16:00 UTC sweet spot. Curated tighter than the
    # individual session lists because we want only the truly
    # high-volume names — anything that doesn't print decent range
    # right now belongs back in the standalone London or NY lists.
    TradingSession.LONDON_NY_OVERLAP: (
        "XAUUSD", "EURUSD", "GBPUSD", "USDJPY",
        "GBPJPY", "EURJPY", "USDCAD",
    ),
}


# ---------------------------------------------------------------------
# Time-window helpers
# ---------------------------------------------------------------------

def _hour_in_window(hour: int, start: int, end: int) -> bool:
    """True if ``hour`` falls inside ``[start, end)`` on a 24h clock.

    Handles wrap-around when ``start >= end`` (e.g. Sydney 21–06):
    in that case the window is the union of ``[start, 24)`` and
    ``[0, end)``. Returning a plain bool keeps callers from having
    to reason about midnight.
    """
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _to_utc(dt: datetime) -> datetime:
    """Treat naive inputs as UTC; cast aware inputs into UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------
# Public API — what the Telegram handler calls
# ---------------------------------------------------------------------

def active_sessions(now: datetime | None = None) -> list[SessionWindow]:
    """Return every session currently open, overlap-first.

    Ordering: the London/NY overlap appears first when it is active
    so the renderer can put the most useful information at the top
    of the message. The remaining sessions follow in
    :data:`SESSION_WINDOWS` order.
    """
    now_utc = _to_utc(now) if now is not None else datetime.now(tz=timezone.utc)
    hour = now_utc.hour

    out: list[SessionWindow] = []
    if _hour_in_window(hour, OVERLAP_WINDOW.start_hour, OVERLAP_WINDOW.end_hour):
        out.append(OVERLAP_WINDOW)
    for window in SESSION_WINDOWS:
        if _hour_in_window(hour, window.start_hour, window.end_hour):
            out.append(window)
    return out


def recommended_symbols_for(
    sessions: list[SessionWindow],
) -> list[str]:
    """De-duplicate the union of recommended symbols across sessions.

    Preserves first-seen order so the LONDON_NY_OVERLAP curated
    list lands at the top when it is active. The Telegram renderer
    then groups by tier; here we only care about the symbol set.
    """
    seen: set[str] = set()
    out: list[str] = []
    for window in sessions:
        for sym in RECOMMENDED_BY_SESSION.get(window.session, ()):
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


def next_session_after(now: datetime | None = None) -> tuple[SessionWindow, datetime]:
    """Return the next named session that opens after ``now`` (UTC).

    Used when the trader pulls up the session screen outside trading
    hours — the message gets a "следующая через 2.3ч" suffix so they
    know when to come back.

    The overlap window participates in the search; it's a real
    session boundary the trader cares about even though it's a
    subset of London and NY hours.
    """
    now_utc = _to_utc(now) if now is not None else datetime.now(tz=timezone.utc)
    hour = now_utc.hour

    candidates: list[tuple[int, int, SessionWindow]] = []
    # ``priority`` is used as a tie-breaker so the overlap wins when
    # NY and the overlap open at the same wall-clock hour — that is
    # the more informative answer for a Fibonacci-grid trader.
    iteration: tuple[tuple[int, SessionWindow], ...] = (
        (0, OVERLAP_WINDOW),
        *((1, w) for w in SESSION_WINDOWS),
    )
    for priority, window in iteration:
        # Hours until ``start_hour`` on a 24-hour clock. ``% 24`` so
        # midnight wrap doesn't hand us a negative number.
        hours_until = (window.start_hour - hour) % 24
        if hours_until == 0:
            hours_until = 24       # "now" already inside → next instance
        candidates.append((hours_until, priority, window))
    candidates.sort(key=lambda t: (t[0], t[1]))
    hours_ahead, _, window = candidates[0]

    next_start = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=hours_ahead
    )
    return window, next_start


__all__ = [
    "OVERLAP_WINDOW",
    "RECOMMENDED_BY_SESSION",
    "SESSION_WINDOWS",
    "SessionWindow",
    "TradingSession",
    "active_sessions",
    "next_session_after",
    "recommended_symbols_for",
]
