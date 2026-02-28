"""Tests for the Delta Skew module."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.risk.delta_skew import DeltaSkew


class TestSkewComputation:
    def test_at_target_no_skew(self) -> None:
        skew = DeltaSkew()
        result = skew.compute(btc_alloc_pct=0.50, target_pct=0.50)
        assert result.buy_offset_bps == Decimal("0")
        assert result.sell_offset_bps == Decimal("0")
        assert result.deviation_pct == 0.0

    def test_over_allocated_skews_to_sell(self) -> None:
        skew = DeltaSkew(sensitivity=Decimal("2.0"))
        result = skew.compute(btc_alloc_pct=0.60, target_pct=0.50)
        # 10% over-allocated → raw_skew = 10 * 2 = 20 bps
        assert result.buy_offset_bps > 0  # Widen buys (less buying)
        assert result.sell_offset_bps < 0  # Tighten sells (more selling)
        assert result.deviation_pct > 0

    def test_under_allocated_skews_to_buy(self) -> None:
        skew = DeltaSkew(sensitivity=Decimal("2.0"))
        result = skew.compute(btc_alloc_pct=0.40, target_pct=0.50)
        assert result.buy_offset_bps < 0  # Tighten buys (more buying)
        assert result.sell_offset_bps > 0  # Widen sells (less selling)
        assert result.deviation_pct < 0

    def test_symmetry(self) -> None:
        skew = DeltaSkew()
        over = skew.compute(btc_alloc_pct=0.60, target_pct=0.50)
        under = skew.compute(btc_alloc_pct=0.40, target_pct=0.50)
        assert over.buy_offset_bps == -under.buy_offset_bps
        assert over.sell_offset_bps == -under.sell_offset_bps


class TestClamp:
    def test_clamped_at_max(self) -> None:
        skew = DeltaSkew(sensitivity=Decimal("10.0"), max_skew_bps=Decimal("30"))
        result = skew.compute(btc_alloc_pct=0.90, target_pct=0.50)
        # 40% deviation * 10 sensitivity = 400 bps raw, clamped to 30
        assert result.buy_offset_bps == Decimal("30")
        assert result.sell_offset_bps == Decimal("-30")
        assert result.raw_skew_bps > Decimal("30")

    def test_negative_clamped(self) -> None:
        skew = DeltaSkew(sensitivity=Decimal("10.0"), max_skew_bps=Decimal("30"))
        result = skew.compute(btc_alloc_pct=0.10, target_pct=0.50)
        assert result.buy_offset_bps == Decimal("-30")
        assert result.sell_offset_bps == Decimal("30")


class TestApplyToSpacing:
    def test_widens_buy_when_over_allocated(self) -> None:
        skew = DeltaSkew(sensitivity=Decimal("2.0"))
        result = skew.compute(btc_alloc_pct=0.60, target_pct=0.50)
        buy_sp, sell_sp = skew.apply_to_spacing(Decimal("50"), result)
        # Buy spacing should increase, sell should decrease
        assert buy_sp > Decimal("50")
        assert sell_sp < Decimal("50")

    def test_floor_at_one_bps(self) -> None:
        skew = DeltaSkew(sensitivity=Decimal("10.0"), max_skew_bps=Decimal("100"))
        result = skew.compute(btc_alloc_pct=0.90, target_pct=0.50)
        buy_sp, sell_sp = skew.apply_to_spacing(Decimal("20"), result)
        # Sell spacing would go negative but is floored at 1
        assert sell_sp >= Decimal("1")

    def test_no_skew_preserves_spacing(self) -> None:
        skew = DeltaSkew()
        result = skew.compute(btc_alloc_pct=0.50, target_pct=0.50)
        buy_sp, sell_sp = skew.apply_to_spacing(Decimal("50"), result)
        assert buy_sp == Decimal("50")
        assert sell_sp == Decimal("50")


class TestSensitivity:
    def test_higher_sensitivity_larger_skew(self) -> None:
        low = DeltaSkew(sensitivity=Decimal("1.0"))
        high = DeltaSkew(sensitivity=Decimal("5.0"))
        result_low = low.compute(btc_alloc_pct=0.60, target_pct=0.50)
        result_high = high.compute(btc_alloc_pct=0.60, target_pct=0.50)
        assert abs(result_high.raw_skew_bps) > abs(result_low.raw_skew_bps)


class TestOBISkew:
    """Tests for order book imbalance (OBI) integration."""

    def test_positive_obi_tightens_buy(self) -> None:
        """Positive OBI (bullish) → tighter buys, wider sells."""
        skew = DeltaSkew(obi_sensitivity_bps=Decimal("15"))
        no_obi = skew.compute(btc_alloc_pct=0.50, target_pct=0.50, obi=0.0)
        with_obi = skew.compute(btc_alloc_pct=0.50, target_pct=0.50, obi=0.5)
        # Positive OBI → negative buy_offset (tighter buy)
        assert with_obi.buy_offset_bps < no_obi.buy_offset_bps
        # Positive OBI → positive sell_offset (wider sell)
        assert with_obi.sell_offset_bps > no_obi.sell_offset_bps

    def test_negative_obi_tightens_sell(self) -> None:
        """Negative OBI (bearish) → tighter sells, wider buys."""
        skew = DeltaSkew(obi_sensitivity_bps=Decimal("15"))
        result = skew.compute(btc_alloc_pct=0.50, target_pct=0.50, obi=-0.5)
        # Negative OBI → positive buy_offset (wider buy)
        assert result.buy_offset_bps > 0
        # Negative OBI → negative sell_offset (tighter sell)
        assert result.sell_offset_bps < 0

    def test_obi_zero_no_effect(self) -> None:
        """Zero OBI should produce no OBI adjustment."""
        skew = DeltaSkew(obi_sensitivity_bps=Decimal("15"))
        result = skew.compute(btc_alloc_pct=0.50, target_pct=0.50, obi=0.0)
        assert result.obi_adjustment_bps == Decimal("0")
        assert result.buy_offset_bps == Decimal("0")

    def test_obi_adjustment_field(self) -> None:
        """OBI adjustment field should reflect the OBI contribution."""
        skew = DeltaSkew(obi_sensitivity_bps=Decimal("10"))
        result = skew.compute(btc_alloc_pct=0.50, target_pct=0.50, obi=1.0)
        # OBI=1.0, sensitivity=10 → obi_adjust = -1.0 * 10 = -10
        assert result.obi_adjustment_bps == Decimal("-10")

    def test_obi_stacks_with_allocation_skew(self) -> None:
        """OBI and allocation deviation should stack."""
        skew = DeltaSkew(
            sensitivity=Decimal("2.0"),
            obi_sensitivity_bps=Decimal("10"),
        )
        # Over-allocated (want to sell) + bullish OBI (also want to sell)
        alloc_only = skew.compute(btc_alloc_pct=0.55, target_pct=0.50, obi=0.0)
        # Combined should have more absolute skew than allocation alone
        # OBI is bullish, alloc is over → both push buy_offset more positive... wait
        # Actually: over-allocated → raw_skew positive → buy_offset positive
        # OBI positive → obi_adjust negative → reduces combined, so they OPPOSE
        # Under-allocated + bullish OBI would be additive
        # Let me test opposing: over-alloc + bearish OBI should amplify
        result2 = skew.compute(btc_alloc_pct=0.55, target_pct=0.50, obi=-0.5)
        # Over-allocated → positive raw skew. Bearish OBI → positive obi_adjust
        # Combined should be larger magnitude than allocation alone
        assert abs(result2.buy_offset_bps) >= abs(alloc_only.buy_offset_bps)

    def test_obi_clamped_input(self) -> None:
        """OBI values outside [-1, 1] should be clamped."""
        skew = DeltaSkew(obi_sensitivity_bps=Decimal("10"))
        extreme = skew.compute(btc_alloc_pct=0.50, target_pct=0.50, obi=5.0)
        clamped = skew.compute(btc_alloc_pct=0.50, target_pct=0.50, obi=1.0)
        assert extreme.obi_adjustment_bps == clamped.obi_adjustment_bps

    def test_default_obi_zero(self) -> None:
        """OBI defaults to 0.0 when not provided."""
        skew = DeltaSkew()
        result = skew.compute(btc_alloc_pct=0.50, target_pct=0.50)
        assert result.obi_adjustment_bps == Decimal("0")
