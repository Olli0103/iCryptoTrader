"""Tests for HedgeManager — portfolio exposure reduction."""

from __future__ import annotations

from icryptotrader.risk.hedge_manager import HedgeManager
from icryptotrader.types import PauseState, Regime


class TestHedgeManager:
    def test_no_hedge_healthy(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10)
        action = hm.evaluate(
            drawdown_pct=0.03,
            regime=Regime.RANGE_BOUND,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.50,
            target_allocation_pct=0.50,
        )
        assert not action.active
        assert not hm.is_active

    def test_hedge_on_drawdown(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10)
        action = hm.evaluate(
            drawdown_pct=0.12,
            regime=Regime.RANGE_BOUND,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.50,
            target_allocation_pct=0.50,
        )
        assert action.active
        assert hm.is_active
        assert hm.activations == 1
        assert action.buy_level_cap is not None

    def test_hedge_on_chaos(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10)
        action = hm.evaluate(
            drawdown_pct=0.02,  # Low drawdown
            regime=Regime.CHAOS,  # But chaos regime
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.50,
            target_allocation_pct=0.50,
        )
        assert action.active

    def test_hedge_on_trending_down_overallocated(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10)
        action = hm.evaluate(
            drawdown_pct=0.02,
            regime=Regime.TRENDING_DOWN,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.70,  # Way above target
            target_allocation_pct=0.50,
        )
        assert action.active

    def test_no_hedge_when_risk_paused(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10)
        action = hm.evaluate(
            drawdown_pct=0.15,
            regime=Regime.CHAOS,
            pause_state=PauseState.RISK_PAUSE_ACTIVE,
            btc_allocation_pct=0.50,
            target_allocation_pct=0.50,
        )
        assert not action.active

    def test_hysteresis_deactivation(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10)
        # Activate
        hm.evaluate(
            drawdown_pct=0.12,
            regime=Regime.RANGE_BOUND,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.50,
            target_allocation_pct=0.50,
        )
        assert hm.is_active

        # Still active at 8% (> 5% = 50% of trigger)
        action = hm.evaluate(
            drawdown_pct=0.08,
            regime=Regime.RANGE_BOUND,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.50,
            target_allocation_pct=0.50,
        )
        assert action.active

        # Deactivates at 4% (< 5% = 50% of trigger)
        action = hm.evaluate(
            drawdown_pct=0.04,
            regime=Regime.RANGE_BOUND,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.50,
            target_allocation_pct=0.50,
        )
        assert not action.active
        assert not hm.is_active

    def test_reduce_exposure_caps_buys(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10, strategy="reduce_exposure")
        action = hm.evaluate(
            drawdown_pct=0.15,
            regime=Regime.TRENDING_DOWN,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.50,
            target_allocation_pct=0.50,
            current_buy_levels=5,
        )
        assert action.buy_level_cap is not None
        assert action.buy_level_cap < 5

    def test_reduce_exposure_zero_when_overallocated(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10, strategy="reduce_exposure")
        action = hm.evaluate(
            drawdown_pct=0.12,
            regime=Regime.RANGE_BOUND,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.70,  # >15% above target
            target_allocation_pct=0.50,
            current_buy_levels=5,
        )
        assert action.buy_level_cap == 0

    def test_inverse_grid_adds_sells(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10, strategy="inverse_grid")
        action = hm.evaluate(
            drawdown_pct=0.15,
            regime=Regime.RANGE_BOUND,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.50,
            target_allocation_pct=0.50,
            current_sell_levels=5,
        )
        assert action.sell_level_boost > 0
        assert action.sell_spacing_tighten_pct > 0

    def test_activation_count(self) -> None:
        hm = HedgeManager(trigger_drawdown_pct=0.10)
        assert hm.activations == 0

        hm.evaluate(drawdown_pct=0.12, regime=Regime.RANGE_BOUND,
                     pause_state=PauseState.ACTIVE_TRADING,
                     btc_allocation_pct=0.5, target_allocation_pct=0.5)
        assert hm.activations == 1

        # Same activation (still active)
        hm.evaluate(drawdown_pct=0.15, regime=Regime.RANGE_BOUND,
                     pause_state=PauseState.ACTIVE_TRADING,
                     btc_allocation_pct=0.5, target_allocation_pct=0.5)
        assert hm.activations == 1
