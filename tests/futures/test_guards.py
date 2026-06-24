"""Tests for spread_guard and session_filter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from futures_bot.core.session_filter import (
    BlackoutWindow,
    SessionFilter,
    SessionStatus,
    is_weekend,
    next_session_open,
)
from futures_bot.core.spread_guard import (
    SpreadSeverity,
    check_block_range,
    classify_spread,
    preflight_block,
)


# ---------------------------------------------------------------------
# Spread guard
# ---------------------------------------------------------------------

class TestRangeCheck:
    """Static pre-flight: is the block range itself big enough?"""

    def test_xauusd_30usd_range_is_fine_with_20c_spread(self) -> None:
        r = check_block_range(range_size=30.0, spread=0.20)
        assert r.ok
        # Ratio = 0.20 / (30 × 0.1273) = 5.2 %.
        assert r.ratio < 0.20

    def test_tiny_range_against_normal_spread_refused(self) -> None:
        r = check_block_range(range_size=5.0, spread=0.20)
        assert not r.ok
        # Recommended min equals safety × spread / (gap × max_ratio).
        # = 1.5 × 0.20 / (0.1273 × 0.20) ≈ 11.78.
        assert r.recommended_min_range == pytest.approx(11.78, abs=0.05)

    @pytest.mark.parametrize("bad_input", [
        {"range_size": 0, "spread": 0.20},
        {"range_size": -1, "spread": 0.20},
        {"range_size": 30, "spread": -0.01},
    ])
    def test_invalid_inputs_return_not_ok(self, bad_input: dict) -> None:
        assert not check_block_range(**bad_input).ok


class TestClassifySpread:
    """Live-spread severity bucket."""

    def test_typical_spread_is_ok(self) -> None:
        out = classify_spread(spread=0.20, typical_spread=0.20)
        assert out.severity == SpreadSeverity.OK

    def test_double_typical_alerts(self) -> None:
        out = classify_spread(spread=0.45, typical_spread=0.20)
        assert out.severity == SpreadSeverity.ALERT

    def test_seven_times_typical_blocked(self) -> None:
        out = classify_spread(spread=1.50, typical_spread=0.20)
        assert out.severity == SpreadSeverity.BLOCKED

    def test_degenerate_inputs_dont_crash(self) -> None:
        out = classify_spread(spread=0.1, typical_spread=0.0)
        # Treats unknown typical as OK rather than raising.
        assert out.severity == SpreadSeverity.OK


class TestPreflight:
    """Combined static + live check used by /newblock."""

    def test_all_clear(self) -> None:
        rep = preflight_block(
            range_size=30.0, spread=0.20, typical_spread=0.20
        )
        assert rep.ok

    def test_blocked_when_live_spread_too_high(self) -> None:
        rep = preflight_block(
            range_size=30.0, spread=1.50, typical_spread=0.20
        )
        assert not rep.ok
        assert rep.spread_reading.severity == SpreadSeverity.BLOCKED


# ---------------------------------------------------------------------
# Session filter
# ---------------------------------------------------------------------

UTC = timezone.utc


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class TestWeekendDetection:
    """Friday 20:30 UTC → Sunday 23:30 UTC is the broker weekend."""

    def test_mid_week_is_open(self) -> None:
        # Tuesday 14:00 UTC.
        assert not is_weekend(_utc(2026, 6, 16, 14, 0))

    def test_saturday_fully_closed(self) -> None:
        assert is_weekend(_utc(2026, 6, 13, 12, 0))

    def test_friday_close_is_closed(self) -> None:
        # Friday 22:00 UTC — past 20:30 UTC cut-off.
        assert is_weekend(_utc(2026, 6, 12, 22, 0))

    def test_friday_morning_is_open(self) -> None:
        assert not is_weekend(_utc(2026, 6, 12, 10, 0))

    def test_sunday_before_reopen_closed(self) -> None:
        assert is_weekend(_utc(2026, 6, 14, 22, 0))

    def test_sunday_after_reopen_open(self) -> None:
        # 23:31 UTC — re-opened.
        assert not is_weekend(_utc(2026, 6, 14, 23, 31))


class TestNextSessionOpen:
    """The filter should be able to tell the trader when to come back."""

    def test_saturday_routes_to_sunday_reopen(self) -> None:
        nxt = next_session_open(_utc(2026, 6, 13, 12, 0))
        assert nxt == _utc(2026, 6, 14, 23, 30)

    def test_friday_after_close_routes_to_sunday_reopen(self) -> None:
        nxt = next_session_open(_utc(2026, 6, 12, 22, 0))
        assert nxt == _utc(2026, 6, 14, 23, 30)

    def test_during_session_returns_now(self) -> None:
        now = _utc(2026, 6, 16, 14, 0)
        assert next_session_open(now) == now


class TestSessionFilter:
    """End-to-end decide() including blackout windows."""

    def test_open_session_allows(self) -> None:
        sf = SessionFilter()
        d = sf.decide(now=_utc(2026, 6, 16, 14, 0))
        assert d.allowed
        assert d.status == SessionStatus.OPEN

    def test_weekend_blocks_with_next_open(self) -> None:
        sf = SessionFilter()
        d = sf.decide(now=_utc(2026, 6, 13, 12, 0))
        assert not d.allowed
        assert d.status == SessionStatus.SESSION_CLOSED
        assert d.next_open == _utc(2026, 6, 14, 23, 30)

    def test_blackout_window_blocks(self) -> None:
        sf = SessionFilter(blackouts=[
            BlackoutWindow(
                name="NFP",
                start=_utc(2026, 6, 16, 13, 0),
                end=_utc(2026, 6, 16, 14, 0),
            )
        ])
        # 13:30 UTC sits inside the window.
        d = sf.decide(now=_utc(2026, 6, 16, 13, 30))
        assert not d.allowed
        assert d.status == SessionStatus.BLACKOUT
        assert d.reason.endswith("NFP")
        # Outside the window — open again.
        d = sf.decide(now=_utc(2026, 6, 16, 14, 30))
        assert d.allowed

    def test_clear_blackouts(self) -> None:
        sf = SessionFilter(blackouts=[
            BlackoutWindow(
                name="x",
                start=_utc(2026, 6, 16, 13, 0),
                end=_utc(2026, 6, 16, 14, 0),
            )
        ])
        sf.clear_blackouts()
        d = sf.decide(now=_utc(2026, 6, 16, 13, 30))
        assert d.allowed

    def test_naive_datetime_treated_as_utc(self) -> None:
        sf = SessionFilter()
        naive = datetime(2026, 6, 13, 12, 0)  # naive == UTC by convention
        d = sf.decide(now=naive)
        assert not d.allowed
