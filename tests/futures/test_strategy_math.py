"""Unit tests for the broker-agnostic strategy math.

Pure-function tests — no DB, no broker, no Telegram. They pin the
numbers the trader and I agreed on during design so any future change
to the constants (1.5× progression, 12.73% gap, ROUND UP, 3× TP
multiplier) shows up as a test failure rather than a silent regression.
"""

from __future__ import annotations

import math

import pytest

from futures_bot.db.enums import BlockSide
from futures_bot.strategy.fib import (
    GAP_PCT,
    NUM_RUNGS,
    compute_levels,
    level_percentages,
)
from futures_bot.strategy.plan import build_plan
from futures_bot.strategy.risk import (
    SymbolSpec,
    calculate_lot,
    cumulative_risks,
    loss_per_lot,
    min_viable_base_risk,
    risk_for_rung,
    risk_schedule,
    size_rungs,
)
from futures_bot.strategy.tp import (
    FillContext,
    compute_sl_price,
    compute_tp_price,
    usd_per_price_unit_from_lot,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def xau_spec() -> SymbolSpec:
    """XAUUSD spec as the mock adapter would supply it on Exness Raw."""
    return SymbolSpec(
        symbol="XAUUSD",
        trade_tick_size=0.01,
        trade_tick_value=1.0,     # $1 P&L per cent move on 1.00 lot
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
    )


# ---------------------------------------------------------------------
# Fibonacci ladder
# ---------------------------------------------------------------------

class TestFibLevels:
    """The exact percentages the trader defined."""

    def test_eight_levels_returned(self) -> None:
        pcts = level_percentages()
        assert len(pcts) == 8

    def test_first_and_last_anchors(self) -> None:
        pcts = level_percentages()
        assert pcts[0] == 0.0
        assert math.isclose(pcts[-1], 1.382, rel_tol=0, abs_tol=1e-9)

    def test_intermediate_levels_match_spec(self) -> None:
        """The six entries are exactly 61.8% .. 125.46%."""
        pcts = level_percentages()
        expected = [0.0, 0.618, 0.7453, 0.8727, 1.0, 1.1273, 1.2547, 1.382]
        # Use abs tolerance rather than relative because the small
        # value 0.0 would otherwise make relative comparisons spike.
        for got, want in zip(pcts, expected, strict=True):
            assert math.isclose(got, want, abs_tol=1e-4)

    def test_uniform_gap_value(self) -> None:
        """Every gap between adjacent levels (after the anchor) is identical."""
        assert math.isclose(GAP_PCT, 0.127333, abs_tol=1e-4)

    def test_six_rungs(self) -> None:
        assert NUM_RUNGS == 6


class TestComputeLevels:
    """compute_levels() turns two anchors into eight prices."""

    def test_buy_block_descending_ladder(self) -> None:
        levels = compute_levels(zero_price=2650.0, hundred_price=2620.0)
        # Anchor + 6 entries + final SL = 8 prices
        assert len(levels.prices) == 8
        # Range matches the inputs.
        assert math.isclose(levels.range_size, 30.0)
        # First entry sits 61.8% of the way down from 2650 toward 2620.
        assert math.isclose(levels.entry_for_rung(1), 2650 - 30 * 0.618, abs_tol=1e-9)
        # Final SL sits 38.2% below 2620 (== 138.2% from the 0% anchor).
        assert math.isclose(levels.final_sl, 2650 - 30 * 1.382, abs_tol=1e-9)

    def test_uniform_sl_distance(self) -> None:
        """Every rung's plan SL distance is identical (the whole point)."""
        levels = compute_levels(zero_price=2650.0, hundred_price=2620.0)
        distances = [
            abs(levels.entry_for_rung(s) - levels.sl_for_rung(s))
            for s in range(1, NUM_RUNGS + 1)
        ]
        for d in distances[1:]:
            assert math.isclose(d, distances[0], abs_tol=1e-6)
        # And equals 12.73% of the range.
        assert math.isclose(distances[0], 30.0 * GAP_PCT, abs_tol=1e-6)

    def test_sell_block_ascending_ladder(self) -> None:
        levels = compute_levels(zero_price=2620.0, hundred_price=2650.0)
        # Entries ascend.
        assert levels.entry_for_rung(1) < levels.entry_for_rung(2)
        assert levels.entry_for_rung(6) > levels.entry_for_rung(1)

    @pytest.mark.parametrize("bad", [
        (0.0, 100.0),
        (100.0, 0.0),
        (-1.0, 100.0),
        (100.0, 100.0),
    ])
    def test_degenerate_anchors_raise(self, bad: tuple[float, float]) -> None:
        with pytest.raises(ValueError):
            compute_levels(*bad)


# ---------------------------------------------------------------------
# Risk progression
# ---------------------------------------------------------------------

class TestRiskProgression:
    """1.5× geometric progression — the numbers from our discussion."""

    def test_rung_1_is_base_risk(self) -> None:
        assert risk_for_rung(5.0, 1) == 5.0

    def test_progression_matches_spec(self) -> None:
        risks = risk_schedule(5.0, NUM_RUNGS)
        # Hand-computed from our chat: 5, 7.5, 11.25, 16.875, 25.3125, 37.96875.
        for got, want in zip(
            risks, [5.0, 7.5, 11.25, 16.875, 25.3125, 37.96875], strict=True
        ):
            assert math.isclose(got, want, abs_tol=1e-9)

    def test_cumulative_risks(self) -> None:
        cum = cumulative_risks([5.0, 7.5, 11.25])
        assert cum == [5.0, 12.5, 23.75]

    @pytest.mark.parametrize("bad_kwargs", [
        {"base_risk": 0},
        {"base_risk": -1},
        {"rung_seq": 0},
        {"multiplier": 0},
        {"multiplier": -1.5},
    ])
    def test_rejects_invalid_inputs(self, bad_kwargs: dict) -> None:
        kwargs = {"base_risk": 5.0, "rung_seq": 1, "multiplier": 1.5}
        kwargs.update(bad_kwargs)
        with pytest.raises(ValueError):
            risk_for_rung(**kwargs)


# ---------------------------------------------------------------------
# Lot sizing
# ---------------------------------------------------------------------

class TestLotSizing:
    """ROUND UP is the trader's chosen mode for the futures bot."""

    def test_loss_per_lot_xauusd_3_82(self, xau_spec: SymbolSpec) -> None:
        # $3.82 SL × 100 ticks/$ × $1/tick = $382 per 1.00 lot.
        assert math.isclose(loss_per_lot(xau_spec, 3.82), 382.0, abs_tol=1e-6)

    @pytest.mark.parametrize(
        ("raw_risk", "expected_lot"),
        [
            (5.00, 0.02),    # 0.0131 raw → up to 0.02
            (7.50, 0.02),    # 0.0196 raw → up to 0.02
            (11.25, 0.03),   # 0.0294 raw → up to 0.03
            (16.88, 0.05),   # 0.0442 raw → up to 0.05
            (25.31, 0.07),   # 0.0663 raw → up to 0.07
            (37.97, 0.10),   # 0.0994 raw → up to 0.10
        ],
    )
    def test_round_up_matches_discussion_table(
        self,
        xau_spec: SymbolSpec,
        raw_risk: float,
        expected_lot: float,
    ) -> None:
        lot, _raw = calculate_lot(
            symbol=xau_spec, risk_usd=raw_risk, sl_distance=3.82, mode="up"
        )
        assert math.isclose(lot, expected_lot, abs_tol=1e-6)

    def test_size_rungs_totals(self, xau_spec: SymbolSpec) -> None:
        sizings = size_rungs(
            symbol=xau_spec,
            sl_distance=3.82,
            base_risk=5.0,
            num_rungs=NUM_RUNGS,
            multiplier=1.5,
            mode="up",
        )
        # Total planned == the spec total of $103.91 (within rounding).
        planned_total = sum(s.planned_risk_usd for s in sizings)
        assert math.isclose(planned_total, 103.91, abs_tol=0.01)
        # Real total with ROUND UP overshoots by ~6-7 % per our chat.
        real_total = sum(s.real_risk_usd for s in sizings)
        assert math.isclose(real_total, 110.78, abs_tol=0.1)
        assert real_total >= planned_total  # ROUND UP never under-risks total

    def test_min_viable_base_risk(self, xau_spec: SymbolSpec) -> None:
        # 0.01 lot × $382 loss-per-lot = $3.82 minimum.
        assert math.isclose(
            min_viable_base_risk(xau_spec, 3.82), 3.82, abs_tol=1e-6
        )


# ---------------------------------------------------------------------
# TP / SL pricing
# ---------------------------------------------------------------------

class TestSlPricing:
    """Chain-rule SL adjustment keeps the ladder unbroken."""

    def test_buy_block_sl_below_next_entry(self) -> None:
        fill = FillContext(entry_price=2631.46, spread=0.20, side=BlockSide.BUY)
        sl = compute_sl_price(
            next_entry=2627.64, fill=fill, safety_multiplier=1.5
        )
        # next_entry - spread × safety = 2627.64 - 0.30 = 2627.34
        assert math.isclose(sl, 2627.34, abs_tol=1e-6)

    def test_sell_block_sl_above_next_entry(self) -> None:
        fill = FillContext(entry_price=2620.0, spread=0.20, side=BlockSide.SELL)
        sl = compute_sl_price(
            next_entry=2623.82, fill=fill, safety_multiplier=1.5
        )
        # For SELL we add the buffer so SL sits above next_entry.
        assert math.isclose(sl, 2624.12, abs_tol=1e-6)

    def test_zero_safety_multiplier_rejected(self) -> None:
        fill = FillContext(entry_price=1.0, spread=0.1, side=BlockSide.BUY)
        with pytest.raises(ValueError):
            compute_sl_price(next_entry=0.5, fill=fill, safety_multiplier=0)


class TestTpPricing:
    """TP_gross = tp_multiplier × cumulative_real_risk."""

    def test_buy_tp_for_rung1_matches_chat_example(
        self, xau_spec: SymbolSpec
    ) -> None:
        # Rung 1: entry $2631.46, lot 0.02 (ROUND UP from 0.0131).
        # cumulative_real_risk = $7.64 (single rung).
        # TP_gross = 3 × $7.64 = $22.92.
        # USD-per-$1 on 0.02 lot = 0.02 × 100 = $2/$.
        # TP distance = $22.92 / $2 = $11.46.
        # TP price = 2631.46 + 11.46 + 0.20 (spread comp) = 2643.12.
        fill = FillContext(entry_price=2631.46, spread=0.20, side=BlockSide.BUY)
        usd = usd_per_price_unit_from_lot(
            lot=0.02,
            trade_tick_size=xau_spec.trade_tick_size,
            trade_tick_value=xau_spec.trade_tick_value,
        )
        tp = compute_tp_price(
            fill=fill,
            lot=0.02,
            cumulative_real_risk_usd=7.64,
            usd_per_price_unit=usd,
            tp_multiplier=3.0,
        )
        assert math.isclose(tp, 2643.12, abs_tol=1e-6)

    def test_sell_tp_subtracts_spread_compensation(
        self, xau_spec: SymbolSpec
    ) -> None:
        fill = FillContext(entry_price=2620.0, spread=0.20, side=BlockSide.SELL)
        usd = usd_per_price_unit_from_lot(
            lot=0.02,
            trade_tick_size=xau_spec.trade_tick_size,
            trade_tick_value=xau_spec.trade_tick_value,
        )
        tp = compute_tp_price(
            fill=fill,
            lot=0.02,
            cumulative_real_risk_usd=7.64,
            usd_per_price_unit=usd,
            tp_multiplier=3.0,
        )
        # 2620 - 11.46 - 0.20 = 2608.34
        assert math.isclose(tp, 2608.34, abs_tol=1e-6)


# ---------------------------------------------------------------------
# Plan builder (integration of the above)
# ---------------------------------------------------------------------

class TestBuildPlan:
    """End-to-end: anchors + risk → BlockPlan ready for the engine."""

    def test_buy_plan_totals_match_discussion(self, xau_spec: SymbolSpec) -> None:
        plan = build_plan(
            symbol="XAUUSD",
            side=BlockSide.BUY,
            zero_price=2650.0,
            hundred_price=2620.0,
            base_risk_usd=5.0,
            cancel_price=2655.0,
            symbol_spec=xau_spec,
            lot_rounding="up",
        )
        assert len(plan.rungs) == NUM_RUNGS
        assert math.isclose(plan.total_planned_risk(), 103.91, abs_tol=0.01)
        assert math.isclose(plan.total_real_risk(), 110.78, abs_tol=0.1)
        # cumulative_real_risk[5] (== last) equals total_real_risk.
        assert math.isclose(
            plan.cumulative_real_risk()[-1], plan.total_real_risk(), abs_tol=1e-6
        )

    def test_buy_orientation_validation(self, xau_spec: SymbolSpec) -> None:
        """BUY block requires zero > hundred."""
        with pytest.raises(ValueError):
            build_plan(
                symbol="XAUUSD",
                side=BlockSide.BUY,
                zero_price=2620.0,         # wrong way around
                hundred_price=2650.0,
                base_risk_usd=5.0,
                cancel_price=2660.0,
                symbol_spec=xau_spec,
            )

    def test_buy_cancel_price_must_be_above(
        self, xau_spec: SymbolSpec
    ) -> None:
        """Cancel price below the ladder for a BUY block is rejected."""
        with pytest.raises(ValueError):
            build_plan(
                symbol="XAUUSD",
                side=BlockSide.BUY,
                zero_price=2650.0,
                hundred_price=2620.0,
                base_risk_usd=5.0,
                cancel_price=2630.0,       # inside the entry range
                symbol_spec=xau_spec,
            )

    def test_sell_plan_works(self, xau_spec: SymbolSpec) -> None:
        plan = build_plan(
            symbol="XAUUSD",
            side=BlockSide.SELL,
            zero_price=2620.0,
            hundred_price=2650.0,
            base_risk_usd=5.0,
            cancel_price=2615.0,
            symbol_spec=xau_spec,
        )
        entries = [r.entry for r in plan.rungs]
        # Entries ascend for a SELL block.
        assert entries == sorted(entries)
