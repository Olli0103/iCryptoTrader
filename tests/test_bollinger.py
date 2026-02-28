"""Tests for Bollinger Band volatility-adaptive grid spacing."""

from decimal import Decimal

from icryptotrader.strategy.bollinger import BollingerSpacing, BollingerState


class TestWindowFilling:
    def test_returns_none_before_window_full(self) -> None:
        bb = BollingerSpacing(window=5)
        for _ in range(4):
            result = bb.update(Decimal("85000"))
            assert result is None
        assert bb.state is None
        assert bb.suggested_spacing_bps is None

    def test_returns_state_when_window_full(self) -> None:
        bb = BollingerSpacing(window=5)
        for _ in range(5):
            result = bb.update(Decimal("85000"))
        assert result is not None
        assert isinstance(result, BollingerState)
        assert bb.state is not None


class TestConstantPrices:
    def test_zero_volatility_uses_min_spacing(self) -> None:
        """Constant prices → std_dev ≈ 0 → spacing = min_spacing."""
        bb = BollingerSpacing(window=10, min_spacing_bps=Decimal("20"))
        for _ in range(10):
            bb.update(Decimal("85000"))
        state = bb.state
        assert state is not None
        assert state.std_dev == Decimal("0")
        assert state.suggested_spacing_bps == Decimal("20")

    def test_sma_equals_price_when_constant(self) -> None:
        bb = BollingerSpacing(window=5)
        for _ in range(5):
            bb.update(Decimal("85000"))
        assert bb.state.sma == Decimal("85000")
        assert bb.state.upper == Decimal("85000")
        assert bb.state.lower == Decimal("85000")
        assert bb.state.band_width_bps == 0


class TestVolatilePrices:
    def test_volatile_prices_widen_spacing(self) -> None:
        """Volatile prices → wider bands → larger spacing."""
        bb = BollingerSpacing(
            window=10,
            multiplier=Decimal("2.0"),
            spacing_scale=Decimal("0.5"),
            min_spacing_bps=Decimal("15"),
        )
        # Oscillating prices: $84,000 - $86,000
        prices = [Decimal("84000"), Decimal("86000")] * 5
        for p in prices:
            bb.update(p)

        state = bb.state
        assert state is not None
        assert state.std_dev > 0
        assert state.band_width_bps > 0
        assert state.suggested_spacing_bps > Decimal("15")

    def test_higher_volatility_means_wider_spacing(self) -> None:
        """More volatile prices → even wider spacing."""
        bb_low = BollingerSpacing(window=10, min_spacing_bps=Decimal("15"))
        bb_high = BollingerSpacing(window=10, min_spacing_bps=Decimal("15"))

        # Low vol: ±$100
        low_prices = [Decimal("84900"), Decimal("85100")] * 5
        for p in low_prices:
            bb_low.update(p)

        # High vol: ±$2000
        high_prices = [Decimal("83000"), Decimal("87000")] * 5
        for p in high_prices:
            bb_high.update(p)

        assert bb_high.state.suggested_spacing_bps > bb_low.state.suggested_spacing_bps


class TestFloorAndCap:
    def test_floor_enforced(self) -> None:
        """Spacing never goes below min_spacing_bps."""
        bb = BollingerSpacing(
            window=5,
            min_spacing_bps=Decimal("50"),
        )
        # Tiny volatility
        for p in [Decimal("85000"), Decimal("85001")] * 3:
            bb.update(p)

        # Still need to fill window
        bb = BollingerSpacing(window=5, min_spacing_bps=Decimal("50"))
        for _ in range(5):
            bb.update(Decimal("85000"))
        assert bb.state.suggested_spacing_bps == Decimal("50")

    def test_cap_enforced(self) -> None:
        """Spacing never exceeds max_spacing_bps."""
        bb = BollingerSpacing(
            window=5,
            max_spacing_bps=Decimal("100"),
            spacing_scale=Decimal("10.0"),  # Extreme scale to hit cap
        )
        prices = [Decimal("80000"), Decimal("90000")] * 3
        for p in prices[:5]:
            bb.update(p)
        assert bb.state.suggested_spacing_bps == Decimal("100")


class TestSpacingScale:
    def test_scale_factor_applied(self) -> None:
        """Doubling spacing_scale doubles the output (up to cap)."""
        bb1 = BollingerSpacing(
            window=10, spacing_scale=Decimal("0.25"),
            min_spacing_bps=Decimal("0"), max_spacing_bps=Decimal("1000"),
        )
        bb2 = BollingerSpacing(
            window=10, spacing_scale=Decimal("0.50"),
            min_spacing_bps=Decimal("0"), max_spacing_bps=Decimal("1000"),
        )
        prices = [Decimal("84000"), Decimal("86000")] * 5
        for p in prices:
            bb1.update(p)
            bb2.update(p)

        # bb2 should be ~2x bb1 (both have same band width, different scale)
        ratio = bb2.state.suggested_spacing_bps / bb1.state.suggested_spacing_bps
        assert Decimal("1.9") < ratio < Decimal("2.1")


class TestMultiplier:
    def test_larger_multiplier_wider_bands(self) -> None:
        bb1 = BollingerSpacing(
            window=10, multiplier=Decimal("1.0"),
            min_spacing_bps=Decimal("0"), max_spacing_bps=Decimal("1000"),
        )
        bb2 = BollingerSpacing(
            window=10, multiplier=Decimal("3.0"),
            min_spacing_bps=Decimal("0"), max_spacing_bps=Decimal("1000"),
        )
        prices = [Decimal("84000"), Decimal("86000")] * 5
        for p in prices:
            bb1.update(p)
            bb2.update(p)

        assert bb2.state.band_width_bps > bb1.state.band_width_bps


class TestReset:
    def test_reset_clears_state(self) -> None:
        bb = BollingerSpacing(window=5)
        for _ in range(5):
            bb.update(Decimal("85000"))
        assert bb.state is not None

        bb.reset()
        assert bb.state is None
        assert bb.suggested_spacing_bps is None

    def test_works_after_reset(self) -> None:
        bb = BollingerSpacing(window=3)
        for _ in range(3):
            bb.update(Decimal("85000"))
        bb.reset()
        for _ in range(3):
            bb.update(Decimal("86000"))
        assert bb.state is not None
        assert bb.state.sma == Decimal("86000")


class TestEdgeCases:
    def test_minimum_window_size(self) -> None:
        bb = BollingerSpacing(window=1)  # Should be clamped to 2
        bb.update(Decimal("85000"))
        assert bb.state is None  # Window min is 2
        bb.update(Decimal("85000"))
        assert bb.state is not None

    def test_sliding_window(self) -> None:
        """Old prices drop off as new ones arrive."""
        bb = BollingerSpacing(window=3)
        bb.update(Decimal("85000"))
        bb.update(Decimal("85000"))
        bb.update(Decimal("85000"))
        assert bb.state.sma == Decimal("85000")

        # Push new price, oldest drops
        bb.update(Decimal("86000"))
        expected_sma = (Decimal("85000") + Decimal("85000") + Decimal("86000")) / 3
        assert bb.state.sma == expected_sma
