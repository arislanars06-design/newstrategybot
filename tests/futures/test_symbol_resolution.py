"""Tests for the cross-broker symbol-name resolver.

This is the new contract the bot relies on whenever a trader types
(or taps) a symbol that isn't named *exactly* the way their broker
exposes it. Exness mini accounts append ``m``, IC Markets uses
``.s`` for raw spread, Pepperstone uses ``.cash`` for indices.
:func:`symbol_name_candidates` plus :meth:`BrokerAdapter.resolve_symbol`
together turn "EURUSD" into "EURUSDm" automatically.

Tests live in two layers:

* Pure function tests against :func:`symbol_name_candidates` — fast,
  no broker.
* Integration tests against :class:`MockAdapter` to make sure the
  candidate list interacts correctly with a catalogue that includes
  suffixed and un-suffixed names.
"""

from __future__ import annotations

import pytest

from futures_bot.adapters.base import SymbolInfo, symbol_name_candidates
from futures_bot.adapters.mock_adapter import MockAdapter


def _eurusdm_info() -> SymbolInfo:
    return SymbolInfo(
        symbol="EURUSDm",
        digits=5,
        point=0.00001,
        trade_tick_size=0.00001,
        trade_tick_value=0.1,
        trade_contract_size=100000.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        spread_typical=0.00008,
    )


# ---------------------------------------------------------------------
# Pure candidate generation
# ---------------------------------------------------------------------

class TestSymbolNameCandidates:
    """Behaviour of the pure ``symbol_name_candidates`` helper."""

    def test_exact_input_is_tried_first(self):
        # A trader who knows their broker's name should hit the
        # resolver in one round-trip.
        cands = symbol_name_candidates("EURUSDm")
        assert cands[0] == "EURUSDm"

    def test_uppercase_input_yields_suffix_variants(self):
        # The most common pattern — type the canonical name, the
        # resolver picks the suffixed variant the broker actually has.
        cands = symbol_name_candidates("EURUSD")
        assert "EURUSDm" in cands
        assert "EURUSD.s" in cands
        assert "EURUSD.r" in cands
        assert "EURUSD.cash" in cands

    def test_lowercase_input_normalises(self):
        # The trader's keyboard state shouldn't matter.
        cands = symbol_name_candidates("eurusd")
        assert "EURUSD" in cands
        assert "EURUSDm" in cands

    def test_input_with_suffix_strips_and_retries_other_suffixes(self):
        # Someone typing ``EURUSD.s`` on an account that uses ``m``
        # should still resolve; the resolver strips the suffix, then
        # tries the others.
        cands = symbol_name_candidates("EURUSD.s")
        assert "EURUSD.s" in cands       # try as-typed first
        assert "EURUSD" in cands         # then the bare base
        assert "EURUSDm" in cands        # and other suffixes

    def test_candidates_are_deduplicated(self):
        cands = symbol_name_candidates("EURUSD")
        assert len(cands) == len(set(cands))

    def test_whitespace_is_trimmed(self):
        # Telegram likes to add a trailing newline on long-press paste.
        cands = symbol_name_candidates("  EURUSD\n")
        assert "EURUSD" in cands


# ---------------------------------------------------------------------
# Adapter integration
# ---------------------------------------------------------------------

class TestMockAdapterResolveSymbol:
    """End-to-end resolution against an in-memory broker catalogue."""

    @pytest.mark.asyncio
    async def test_prefers_exact_match_when_both_exist(self):
        # Some Exness accounts list both ``EURUSD`` and ``EURUSDm``.
        # The bot should prefer the canonical (un-suffixed) name so
        # logs and Telegram messages stay readable.
        adapter = MockAdapter()
        adapter.add_symbol(_eurusdm_info())
        resolved = await adapter.resolve_symbol("EURUSD")
        assert resolved == "EURUSD"

    @pytest.mark.asyncio
    async def test_falls_back_to_suffixed_when_base_missing(self):
        # The point of the resolver: trader types EURUSD, broker
        # only has EURUSDm — we should still find it.
        adapter = MockAdapter(symbols={})       # empty catalogue
        adapter.add_symbol(_eurusdm_info())
        resolved = await adapter.resolve_symbol("EURUSD")
        assert resolved == "EURUSDm"

    @pytest.mark.asyncio
    async def test_resolves_when_user_typed_suffix_already(self):
        # If the trader knows their broker's name, the resolver
        # should not "helpfully" strip it.
        adapter = MockAdapter(symbols={})
        adapter.add_symbol(_eurusdm_info())
        resolved = await adapter.resolve_symbol("EURUSDm")
        assert resolved == "EURUSDm"

    @pytest.mark.asyncio
    async def test_lowercase_input_still_resolves(self):
        adapter = MockAdapter(symbols={})
        adapter.add_symbol(_eurusdm_info())
        resolved = await adapter.resolve_symbol("eurusd")
        # Either 'EURUSDm' (preferred) or 'EURUSD' would be valid;
        # what matters is we don't raise.
        assert resolved in {"EURUSD", "EURUSDm"}

    @pytest.mark.asyncio
    async def test_unknown_symbol_raises_with_message(self):
        adapter = MockAdapter()
        with pytest.raises(ValueError, match="unknown symbol"):
            await adapter.resolve_symbol("FOOBAR123")
