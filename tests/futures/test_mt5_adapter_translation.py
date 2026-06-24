"""Unit tests for the pure-Python helpers in :mod:`mt5_adapter`.

We can't unit-test the live RPC path without a Wine container, but
the translation helpers and the filling-mode picker are pure
functions of the inputs and absolutely need to stay correct: a
silently mis-parsed retcode would turn a failed order into a
"success" and the engine would mark the rung OPEN with no actual
position behind it.

Goals of this test module:

* Guarantee :func:`_translate_order_send_result` agrees with the
  documented MT5 retcode table.
* Pin down :func:`_pick_market_filling` so a refactor can't quietly
  start emitting FOK for an IOC-only symbol.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from futures_bot.adapters.mt5_adapter import (
    _ORDER_FILLING_FOK,
    _ORDER_FILLING_IOC,
    _ORDER_FILLING_RETURN,
    _SYMBOL_FILLING_FOK,
    _SYMBOL_FILLING_IOC,
    _fail_result,
    _pick_market_filling,
    _translate_order_send_result,
)


@dataclass
class _FakeResult:
    """Stand-in for MT5's OrderSendResult named tuple.

    Only the fields the translator reads — the real struct has more.
    """

    retcode: int
    order: int = 0
    deal: int = 0
    price: float = 0.0
    comment: str = ""


class TestTranslateOrderSendResult:
    """:func:`_translate_order_send_result` against the MT5 retcode table."""

    def test_none_result_is_failure(self) -> None:
        r = _translate_order_send_result(None)
        assert not r.ok
        assert r.error_code == -1
        assert r.error_message is not None and "None" in r.error_message

    def test_pending_placement_success(self) -> None:
        # retcode 10008 = TRADE_RETCODE_PLACED — pending limit accepted.
        raw = _FakeResult(retcode=10008, order=555, deal=0, price=0.0)
        r = _translate_order_send_result(raw)
        assert r.ok
        assert r.ticket == "555"
        # No deal on a placement, so position_ticket falls back to the
        # order ticket — gives the engine *something* to call back with
        # if it needs to modify SL/TP on a pending order.
        assert r.position_ticket == "555"
        assert r.filled_price is None
        assert r.error_code is None

    def test_market_done_uses_deal_as_position_ticket(self) -> None:
        # retcode 10009 = TRADE_RETCODE_DONE — market fill.
        raw = _FakeResult(retcode=10009, order=111, deal=222, price=2640.50)
        r = _translate_order_send_result(raw)
        assert r.ok
        assert r.ticket == "111"
        # Deal ticket is preferred because on hedging accounts it
        # matches the new position's identifier.
        assert r.position_ticket == "222"
        assert r.filled_price == pytest.approx(2640.50)

    def test_partial_fill_is_success(self) -> None:
        raw = _FakeResult(retcode=10010, order=1, deal=2, price=1.0)
        r = _translate_order_send_result(raw)
        assert r.ok

    def test_rejected_retcode_propagates_error(self) -> None:
        # 10004 = TRADE_RETCODE_REQUOTE — typical "no fill" case.
        raw = _FakeResult(
            retcode=10004, order=0, deal=0, price=0.0, comment="Requote"
        )
        r = _translate_order_send_result(raw)
        assert not r.ok
        assert r.error_code == 10004
        assert r.error_message == "Requote"
        assert r.ticket is None
        assert r.position_ticket is None

    def test_failed_result_with_empty_comment_falls_back_to_code(self) -> None:
        raw = _FakeResult(retcode=10006, order=0, deal=0, comment="")
        r = _translate_order_send_result(raw)
        assert not r.ok
        # When the broker forgets to attach a human message we surface
        # the bare retcode rather than an empty string — at least the
        # operator can grep for it.
        assert r.error_message == "retcode=10006"


class TestPickMarketFilling:
    """Filling-mode picker prefers IOC, then FOK, then RETURN."""

    def test_prefers_ioc_when_both_supported(self) -> None:
        mask = _SYMBOL_FILLING_FOK | _SYMBOL_FILLING_IOC
        assert _pick_market_filling(mask) == _ORDER_FILLING_IOC

    def test_uses_fok_when_only_fok_supported(self) -> None:
        assert _pick_market_filling(_SYMBOL_FILLING_FOK) == _ORDER_FILLING_FOK

    def test_uses_ioc_when_only_ioc_supported(self) -> None:
        assert _pick_market_filling(_SYMBOL_FILLING_IOC) == _ORDER_FILLING_IOC

    def test_falls_back_to_return_when_neither_set(self) -> None:
        # Some exotic instruments advertise no filling flag at all;
        # RETURN is the universal fallback that lets the broker decide.
        assert _pick_market_filling(0) == _ORDER_FILLING_RETURN


class TestFailResultHelper:
    """Small shape check — easy to break with a careless refactor."""

    def test_fail_result_is_consistently_structured(self) -> None:
        r = _fail_result(code=42, message="nope")
        assert r.ok is False
        assert r.ticket is None
        assert r.position_ticket is None
        assert r.filled_price is None
        assert r.error_code == 42
        assert r.error_message == "nope"
