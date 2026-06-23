"""Trading-session / blackout calendar.

The futures market only runs 24×5: closed on Saturday, plus an awkward
sliver of Friday-evening-into-Sunday-evening when spreads explode and
the chain math breaks down. Some sessions inside the trading week are
also notoriously bad for our strategy (Asia rollover, news prints).

The session filter answers one question: *is it safe to open a new
block right now?* It does NOT manage already-open blocks — those are
left alone, because the spread guard and the engine handle them.

The filter is timezone-aware: every rule is expressed in UTC, but
inputs accept any ``datetime`` (naive or aware) and convert before
comparing. Tests pass an explicit ``now`` so they don't depend on the
wall clock.

There is no news-calendar integration yet — that's a follow-up. For
now the filter takes a manual list of blackout windows the trader can
populate from the .env file. Honest defaults (no NFP magic) beats a
half-built news scraper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum


class SessionStatus(StrEnum):
    """Why the filter is in its current state."""

    OPEN = "OPEN"                # trading freely
    SESSION_CLOSED = "SESSION_CLOSED"   # weekend / Friday close / Sunday open
    BLACKOUT = "BLACKOUT"        # configured news/event window
    HOLIDAY = "HOLIDAY"          # configured non-trading day


@dataclass(slots=True, frozen=True)
class SessionDecision:
    """Result of asking the filter whether a new block may open now."""

    allowed: bool
    status: SessionStatus
    reason: str
    next_open: datetime | None   # when the filter expects to re-open (UTC)


@dataclass(slots=True, frozen=True)
class BlackoutWindow:
    """A configured pause window (news, holiday, manual freeze)."""

    name: str
    start: datetime
    end: datetime


# Defaults — overridable per call, hard-coded here as a single source
# of truth so the engine and tests agree.

# Friday close: most brokers stop accepting orders at 21:00 UTC. We
# refuse new blocks from 20:30 UTC on Friday so positions opened just
# before the close don't get caught in the weekend gap.
FRIDAY_CLOSE_FROM = time(20, 30)
# Sunday market re-open: 22:00 UTC. Spreads stay wide for the first
# hour or so; refuse new blocks until 23:30 UTC.
SUNDAY_REOPEN_UNTIL = time(23, 30)


# ---------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------

def _to_utc(dt: datetime) -> datetime:
    """Return ``dt`` in UTC; treat naive inputs as already UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_weekend(now_utc: datetime) -> bool:
    """True when the broker is closed for the regular weekly break.

    The check follows the broker convention (Friday late evening
    through Sunday late evening) rather than a strict Saturday-only
    test.
    """
    weekday = now_utc.weekday()  # Monday = 0
    if weekday == 5:             # Saturday — fully closed
        return True
    if weekday == 4 and now_utc.time() >= FRIDAY_CLOSE_FROM:
        return True
    if weekday == 6 and now_utc.time() < SUNDAY_REOPEN_UNTIL:
        return True
    return False


def next_session_open(now_utc: datetime) -> datetime:
    """Return the next moment the regular session re-opens (UTC).

    Used to populate ``SessionDecision.next_open`` so the bot can tell
    the trader exactly when it will start accepting blocks again.
    """
    weekday = now_utc.weekday()

    # Friday after close → Sunday re-open.
    if weekday == 4 and now_utc.time() >= FRIDAY_CLOSE_FROM:
        days_until_sunday = 6 - weekday  # 4 → 6 = +2
        target_date = (now_utc + timedelta(days=days_until_sunday)).date()
        return datetime.combine(
            target_date, SUNDAY_REOPEN_UNTIL, tzinfo=timezone.utc
        )

    # Saturday → Sunday re-open.
    if weekday == 5:
        target_date = (now_utc + timedelta(days=1)).date()
        return datetime.combine(
            target_date, SUNDAY_REOPEN_UNTIL, tzinfo=timezone.utc
        )

    # Sunday before re-open → today at 23:30 UTC.
    if weekday == 6 and now_utc.time() < SUNDAY_REOPEN_UNTIL:
        return datetime.combine(
            now_utc.date(), SUNDAY_REOPEN_UNTIL, tzinfo=timezone.utc
        )

    # Already open.
    return now_utc


# ---------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------

class SessionFilter:
    """Decides whether a new block may open right now.

    Construction takes the configured blackout windows so the engine
    can hand them in at startup. Adding/removing windows at runtime
    goes through dedicated methods so we keep a single mutation
    surface — handy for /set-blackout style admin commands later.
    """

    def __init__(
        self,
        *,
        blackouts: list[BlackoutWindow] | None = None,
    ) -> None:
        self._blackouts: list[BlackoutWindow] = list(blackouts or [])

    def add_blackout(self, window: BlackoutWindow) -> None:
        """Add a manual pause window (e.g. for a known news event)."""
        self._blackouts.append(window)

    def clear_blackouts(self) -> None:
        """Remove every configured blackout."""
        self._blackouts.clear()

    def decide(self, *, now: datetime | None = None) -> SessionDecision:
        """Return the decision for the given ``now`` (defaults to wall clock).

        Tests pass ``now`` explicitly so they don't depend on the
        clock. Production callers pass nothing and get UTC now.
        """
        now_utc = _to_utc(now) if now is not None else datetime.now(tz=timezone.utc)

        if is_weekend(now_utc):
            return SessionDecision(
                allowed=False,
                status=SessionStatus.SESSION_CLOSED,
                reason="market session closed (Friday close → Sunday re-open)",
                next_open=next_session_open(now_utc),
            )

        # Active blackout?
        for window in self._blackouts:
            start = _to_utc(window.start)
            end = _to_utc(window.end)
            if start <= now_utc < end:
                return SessionDecision(
                    allowed=False,
                    status=SessionStatus.BLACKOUT,
                    reason=f"blackout window active: {window.name}",
                    next_open=end,
                )

        return SessionDecision(
            allowed=True,
            status=SessionStatus.OPEN,
            reason="session open",
            next_open=None,
        )
