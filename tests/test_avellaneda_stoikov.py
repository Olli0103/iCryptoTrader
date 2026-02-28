"""Tests for the Avellaneda-Stoikov optimal market making model."""

from __future__ import annotations

from decimal import Decimal

import pytest

from icryptotrader.strategy.avellaneda_stoikov import ASResult, AvellanedaStoikov


class TestBasicComputation:
    def test_balanced_inventory_symmetric_spread(self) -> None:
        """With zero inventory delta, buy and sell spacing should be equal."""
        model = AvellanedaStoikov(gamma=Decimal("0.3"))
        result = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("33"),
        )
        assert result.buy_spacing_bps == result.sell_spacing_bps
        assert result.inventory_skew_bps == Decimal("0")

    def test_returns_as_result(self) -> None:
        model = AvellanedaStoikov()
        result = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("33"),
        )
        assert isinstance(result, ASResult)

    def test_zero_volatility_uses_fee_floor(self) -> None:
        """When vol is 0, spread should be the fee floor."""
        model = AvellanedaStoikov(gamma=Decimal("0.3"))
        result = model.compute(
            volatility_bps=Decimal("0"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("33"),
        )
        assert result.half_spread_bps == Decimal("33")
        assert result.buy_spacing_bps == Decimal("33")
        assert result.sell_spacing_bps == Decimal("33")


class TestVolatilityScaling:
    def test_higher_vol_wider_spread(self) -> None:
        """Spread should increase with volatility."""
        model = AvellanedaStoikov(gamma=Decimal("0.5"))
        low_vol = model.compute(
            volatility_bps=Decimal("50"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
        )
        high_vol = model.compute(
            volatility_bps=Decimal("200"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
        )
        assert high_vol.half_spread_bps > low_vol.half_spread_bps

    def test_vol_proportional_to_gamma(self) -> None:
        """Higher gamma → wider spread at same volatility."""
        low_gamma = AvellanedaStoikov(gamma=Decimal("0.1"))
        high_gamma = AvellanedaStoikov(gamma=Decimal("0.5"))
        r_low = low_gamma.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
        )
        r_high = high_gamma.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
        )
        assert r_high.half_spread_bps > r_low.half_spread_bps

    def test_spread_capped_at_max(self) -> None:
        """Spread should not exceed max_spread_bps."""
        model = AvellanedaStoikov(
            gamma=Decimal("1.0"), max_spread_bps=Decimal("100"),
        )
        result = model.compute(
            volatility_bps=Decimal("500"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
        )
        assert result.half_spread_bps <= Decimal("100")


class TestInventorySkew:
    def test_long_inventory_widens_buy(self) -> None:
        """When long (positive delta), buy should be wider than sell."""
        model = AvellanedaStoikov(gamma=Decimal("0.3"))
        result = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0.2"),
            fee_floor_bps=Decimal("10"),
        )
        assert result.buy_spacing_bps > result.sell_spacing_bps
        assert result.inventory_skew_bps > 0

    def test_short_inventory_widens_sell(self) -> None:
        """When short (negative delta), sell should be wider than buy."""
        model = AvellanedaStoikov(gamma=Decimal("0.3"))
        result = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("-0.2"),
            fee_floor_bps=Decimal("10"),
        )
        assert result.sell_spacing_bps > result.buy_spacing_bps
        assert result.inventory_skew_bps < 0

    def test_skew_scales_with_volatility(self) -> None:
        """Key A-S insight: inventory skew should be larger when vol is higher."""
        model = AvellanedaStoikov(gamma=Decimal("0.3"))
        low_vol = model.compute(
            volatility_bps=Decimal("50"),
            inventory_delta=Decimal("0.2"),
            fee_floor_bps=Decimal("10"),
        )
        high_vol = model.compute(
            volatility_bps=Decimal("200"),
            inventory_delta=Decimal("0.2"),
            fee_floor_bps=Decimal("10"),
        )
        assert abs(high_vol.inventory_skew_bps) > abs(low_vol.inventory_skew_bps)

    def test_skew_clamped_at_max(self) -> None:
        """Skew should not exceed max_skew_bps."""
        model = AvellanedaStoikov(
            gamma=Decimal("1.0"), max_skew_bps=Decimal("20"),
        )
        result = model.compute(
            volatility_bps=Decimal("500"),
            inventory_delta=Decimal("0.5"),
            fee_floor_bps=Decimal("10"),
        )
        # Total skew (inv + obi) clamped
        diff = abs(result.buy_spacing_bps - result.sell_spacing_bps) / 2
        assert diff <= Decimal("20")


class TestOBIIntegration:
    def test_positive_obi_tightens_buy(self) -> None:
        """Positive OBI (bullish) → tighter buys, wider sells."""
        model = AvellanedaStoikov(
            gamma=Decimal("0.3"), obi_sensitivity_bps=Decimal("10"),
        )
        no_obi = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
        )
        with_obi = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
            obi=0.5,
        )
        assert with_obi.buy_spacing_bps < no_obi.buy_spacing_bps
        assert with_obi.sell_spacing_bps > no_obi.sell_spacing_bps

    def test_negative_obi_tightens_sell(self) -> None:
        """Negative OBI (bearish) → tighter sells, wider buys."""
        model = AvellanedaStoikov(
            gamma=Decimal("0.3"), obi_sensitivity_bps=Decimal("10"),
        )
        result = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
            obi=-0.5,
        )
        neutral = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
        )
        assert result.sell_spacing_bps < neutral.sell_spacing_bps
        assert result.buy_spacing_bps > neutral.buy_spacing_bps

    def test_obi_clamped_to_range(self) -> None:
        """OBI values outside [-1, 1] should be clamped."""
        model = AvellanedaStoikov(obi_sensitivity_bps=Decimal("10"))
        r1 = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
            obi=2.0,  # Clamped to 1.0
        )
        r2 = model.compute(
            volatility_bps=Decimal("100"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("10"),
            obi=1.0,
        )
        assert r1.obi_skew_bps == r2.obi_skew_bps


class TestFloorAndSafety:
    def test_spacing_never_below_one(self) -> None:
        """Spacing should always be >= 1 bps."""
        model = AvellanedaStoikov(
            gamma=Decimal("0.3"), max_skew_bps=Decimal("200"),
        )
        result = model.compute(
            volatility_bps=Decimal("10"),
            inventory_delta=Decimal("0.5"),
            fee_floor_bps=Decimal("5"),
        )
        assert result.buy_spacing_bps >= Decimal("1")
        assert result.sell_spacing_bps >= Decimal("1")

    def test_gamma_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="gamma must be positive"):
            AvellanedaStoikov(gamma=Decimal("0"))

    def test_fee_floor_respected(self) -> None:
        """Half spread should never go below the fee floor."""
        model = AvellanedaStoikov(gamma=Decimal("0.01"))
        result = model.compute(
            volatility_bps=Decimal("10"),
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("50"),
        )
        assert result.half_spread_bps >= Decimal("50")


class TestPracticalScenarios:
    def test_calm_market_balanced(self) -> None:
        """Calm market, balanced inventory → spreads near fee floor."""
        model = AvellanedaStoikov(gamma=Decimal("0.3"))
        result = model.compute(
            volatility_bps=Decimal("30"),  # 0.3% vol
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("33"),
        )
        # gamma * 30 = 9, below fee floor of 33
        assert result.half_spread_bps == Decimal("33")

    def test_volatile_market_balanced(self) -> None:
        """High vol, balanced → spreads widen beyond fee floor."""
        model = AvellanedaStoikov(gamma=Decimal("0.3"))
        result = model.compute(
            volatility_bps=Decimal("200"),  # 2% vol
            inventory_delta=Decimal("0"),
            fee_floor_bps=Decimal("33"),
        )
        # gamma * 200 = 60, above fee floor
        assert result.half_spread_bps == Decimal("60")
        assert result.buy_spacing_bps == Decimal("60")

    def test_volatile_market_long_position(self) -> None:
        """High vol + long inventory → wide buy, tight sell."""
        model = AvellanedaStoikov(gamma=Decimal("0.3"))
        result = model.compute(
            volatility_bps=Decimal("200"),
            inventory_delta=Decimal("0.3"),
            fee_floor_bps=Decimal("33"),
        )
        # half_spread = 60, inv_skew = 0.3 * 200 * 0.3 = 18
        assert result.buy_spacing_bps > result.sell_spacing_bps
        # Sell should be tighter to incentivize reducing position
        assert result.sell_spacing_bps < Decimal("60")
