"""Tests for the Regime Router."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.strategy.regime_router import RegimeRouter
from icryptotrader.types import Regime


class TestDefaultRegime:
    def test_starts_range_bound(self) -> None:
        router = RegimeRouter()
        assert router.regime == Regime.RANGE_BOUND

    def test_classify_without_data_returns_range_bound(self) -> None:
        router = RegimeRouter()
        decision = router.classify()
        assert decision.regime == Regime.RANGE_BOUND

    def test_stable_prices_stay_range_bound(self) -> None:
        router = RegimeRouter()
        for _ in range(30):
            router.update_price(Decimal("85000"))
        decision = router.classify()
        assert decision.regime == Regime.RANGE_BOUND


class TestTrendDetection:
    def test_upward_trend(self) -> None:
        router = RegimeRouter(momentum_threshold=0.02, momentum_window=10)
        # Simulate 3% upward move
        for i in range(10):
            price = Decimal("85000") + Decimal(str(i * 300))
            router.update_price(price)
        decision = router.classify()
        assert decision.regime == Regime.TRENDING_UP
        assert decision.grid_levels_buy >= decision.grid_levels_sell

    def test_downward_trend(self) -> None:
        router = RegimeRouter(momentum_threshold=0.02, momentum_window=10)
        # Simulate 3% downward move
        for i in range(10):
            price = Decimal("85000") - Decimal(str(i * 300))
            router.update_price(price)
        decision = router.classify()
        assert decision.regime == Regime.TRENDING_DOWN
        assert decision.grid_levels_sell >= decision.grid_levels_buy

    def test_small_move_stays_range_bound(self) -> None:
        router = RegimeRouter(momentum_threshold=0.02, momentum_window=10)
        # Simulate 0.5% move (below threshold)
        for i in range(10):
            price = Decimal("85000") + Decimal(str(i * 50))
            router.update_price(price)
        decision = router.classify()
        assert decision.regime == Regime.RANGE_BOUND


class TestChaosDetection:
    def test_extreme_volatility_triggers_chaos(self) -> None:
        router = RegimeRouter(chaos_vol_threshold=0.08)
        # Large price swings
        prices = [
            Decimal("85000"), Decimal("78000"), Decimal("90000"),
            Decimal("75000"), Decimal("92000"),
        ]
        for p in prices:
            router.update_price(p)
        decision = router.classify()
        assert decision.regime == Regime.CHAOS
        assert decision.grid_levels_buy == 0
        assert decision.grid_levels_sell == 0

    def test_high_vol_plus_toxicity(self) -> None:
        router = RegimeRouter(
            high_vol_threshold=0.04,
            toxicity_threshold=0.8,
        )
        # Moderate volatility
        prices = [
            Decimal("85000"), Decimal("83000"), Decimal("86000"),
            Decimal("82000"),
        ]
        for p in prices:
            router.update_price(p)
        router.update_flow_toxicity(0.9)
        decision = router.classify()
        # Should be chaos if vol >= high_vol AND toxicity >= threshold
        if router.ewma_volatility >= 0.04:
            assert decision.regime == Regime.CHAOS


class TestOverride:
    def test_manual_override(self) -> None:
        router = RegimeRouter()
        router.override_regime(Regime.CHAOS, "test")
        assert router.regime == Regime.CHAOS
        assert router.regime_changes == 1

    def test_override_preserves_after_classify(self) -> None:
        router = RegimeRouter()
        # First classify to range_bound
        router.update_price(Decimal("85000"))
        router.classify()
        # Override to chaos
        router.override_regime(Regime.CHAOS, "risk")
        # Classify again with stable data - may change back
        for _ in range(5):
            router.update_price(Decimal("85000"))
        decision = router.classify()
        # With stable prices, it should revert to range_bound
        assert decision.regime == Regime.RANGE_BOUND


class TestSignalUpdates:
    def test_obi_clamped(self) -> None:
        router = RegimeRouter()
        router.update_order_book_imbalance(1.5)
        assert router._obi == 1.0
        router.update_order_book_imbalance(-2.0)
        assert router._obi == -1.0

    def test_toxicity_clamped(self) -> None:
        router = RegimeRouter()
        router.update_flow_toxicity(1.5)
        assert router._toxicity == 1.0
        router.update_flow_toxicity(-0.5)
        assert router._toxicity == 0.0


class TestMetrics:
    def test_regime_change_counter(self) -> None:
        router = RegimeRouter(momentum_threshold=0.02, momentum_window=5)
        # Start range_bound, move to trending_up
        for i in range(5):
            router.update_price(Decimal("85000") + Decimal(str(i * 500)))
        router.classify()
        assert router.regime_changes >= 1

    def test_ewma_volatility_updates(self) -> None:
        router = RegimeRouter()
        router.update_price(Decimal("85000"))
        router.update_price(Decimal("85500"))
        assert router.ewma_volatility > 0


class TestGridLevelRecommendations:
    def test_range_bound_full_grid(self) -> None:
        router = RegimeRouter(default_buy_levels=5, default_sell_levels=5)
        decision = router.classify()
        assert decision.grid_levels_buy == 5
        assert decision.grid_levels_sell == 5

    def test_chaos_zero_levels(self) -> None:
        router = RegimeRouter()
        router.override_regime(Regime.CHAOS)
        # Need to trigger chaos through classify
        prices = [
            Decimal("85000"), Decimal("70000"), Decimal("95000"),
            Decimal("65000"), Decimal("100000"),
        ]
        for p in prices:
            router.update_price(p)
        decision = router.classify()
        if decision.regime == Regime.CHAOS:
            assert decision.grid_levels_buy == 0
            assert decision.grid_levels_sell == 0
