"""Tests for the Risk Manager."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.risk.risk_manager import DrawdownLevel, RiskManager
from icryptotrader.types import PauseState, Regime


class TestDrawdownClassification:
    def test_healthy(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        snap = rm.update_portfolio(Decimal("4500"), Decimal("5500"))
        assert snap.drawdown_level == DrawdownLevel.HEALTHY

    def test_warning_at_5pct(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        snap = rm.update_portfolio(Decimal("4000"), Decimal("5500"))
        assert snap.drawdown_level == DrawdownLevel.WARNING

    def test_problem_at_10pct(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        snap = rm.update_portfolio(Decimal("3500"), Decimal("5500"))
        assert snap.drawdown_level == DrawdownLevel.PROBLEM

    def test_critical_at_15pct(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        snap = rm.update_portfolio(Decimal("3000"), Decimal("5500"))
        assert snap.drawdown_level == DrawdownLevel.CRITICAL

    def test_emergency_at_20pct(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        snap = rm.update_portfolio(Decimal("2500"), Decimal("5500"))
        assert snap.drawdown_level == DrawdownLevel.EMERGENCY


class TestPauseStateMachine:
    def test_starts_active(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        assert rm.pause_state == PauseState.ACTIVE_TRADING

    def test_critical_dd_triggers_risk_pause(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("3000"), Decimal("5500"))
        assert rm.pause_state == PauseState.RISK_PAUSE_ACTIVE

    def test_emergency_dd_triggers_emergency_sell(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("2500"), Decimal("5500"))
        assert rm.pause_state == PauseState.EMERGENCY_SELL

    def test_tax_lock_plus_critical_dd_gives_dual_lock(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.set_tax_locked(True)
        rm.update_portfolio(Decimal("3000"), Decimal("5500"))
        assert rm.pause_state == PauseState.DUAL_LOCK

    def test_tax_lock_alone_gives_tax_lock_state(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.set_tax_locked(True)
        rm.update_portfolio(Decimal("4500"), Decimal("5500"))
        assert rm.pause_state == PauseState.TAX_LOCK_ACTIVE

    def test_recovery_with_hysteresis(self) -> None:
        rm = RiskManager(
            initial_portfolio_usd=Decimal("10000"),
            recovery_hysteresis_pct=0.05,
        )
        # First go to critical
        rm.update_portfolio(Decimal("3000"), Decimal("5500"))
        assert rm.pause_state == PauseState.RISK_PAUSE_ACTIVE
        # Recover to 4% DD (below problem - hysteresis = 10% - 5% = 5%)
        rm.update_portfolio(Decimal("4600"), Decimal("5000"))
        assert rm.pause_state == PauseState.ACTIVE_TRADING


class TestTradingPermissions:
    def test_active_allows_all(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        assert rm.is_trading_allowed is True
        assert rm.is_buy_allowed is True
        assert rm.is_sell_allowed is True

    def test_tax_lock_allows_buy_only(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.set_tax_locked(True)
        rm.update_portfolio(Decimal("4500"), Decimal("5500"))
        assert rm.is_buy_allowed is True
        assert rm.is_sell_allowed is False

    def test_risk_pause_blocks_all(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("3000"), Decimal("5500"))
        assert rm.is_trading_allowed is False


class TestHighWaterMark:
    def test_hwm_increases(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("6000"), Decimal("5000"))  # 11000 total
        assert rm.high_water_mark == Decimal("11000")

    def test_hwm_does_not_decrease(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("6000"), Decimal("5000"))  # 11000
        rm.update_portfolio(Decimal("4000"), Decimal("5000"))  # 9000
        assert rm.high_water_mark == Decimal("11000")

    def test_drawdown_pct(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("4000"), Decimal("5000"))  # 9000
        assert abs(rm.drawdown_pct - 0.10) < 0.001


class TestPriceVelocity:
    def test_no_freeze_on_normal_move(self) -> None:
        rm = RiskManager(price_velocity_freeze_pct=0.03)
        assert rm.check_price_velocity(Decimal("85000")) is False
        assert rm.check_price_velocity(Decimal("85100")) is False

    def test_freeze_on_large_move(self) -> None:
        rm = RiskManager(
            price_velocity_freeze_pct=0.03,
            price_velocity_window_sec=60,
        )
        rm.check_price_velocity(Decimal("85000"))
        # Simulate 4% move (> 3% threshold) — same timestamp since we're testing logic
        frozen = rm.check_price_velocity(Decimal("81500"))
        # Note: this may not trigger because time diff is 0
        # The price entries are recorded at nearly the same time
        # so velocity = |81500-85000|/85000 = 4.1%
        assert frozen is True


class TestAllocationCheck:
    def test_within_bounds(self) -> None:
        rm = RiskManager()
        buy_ok, sell_ok = rm.check_allocation(
            btc_alloc_pct=0.50, target_pct=0.50, max_pct=0.60, min_pct=0.40,
        )
        assert buy_ok is True
        assert sell_ok is True

    def test_at_max_blocks_buy(self) -> None:
        rm = RiskManager()
        buy_ok, sell_ok = rm.check_allocation(
            btc_alloc_pct=0.60, target_pct=0.50, max_pct=0.60, min_pct=0.40,
        )
        assert buy_ok is False
        assert sell_ok is True

    def test_at_min_blocks_sell(self) -> None:
        rm = RiskManager()
        buy_ok, sell_ok = rm.check_allocation(
            btc_alloc_pct=0.40, target_pct=0.50, max_pct=0.60, min_pct=0.40,
        )
        assert buy_ok is True
        assert sell_ok is False


class TestRegimeSuggestion:
    def test_critical_suggests_chaos(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        snap = rm.update_portfolio(Decimal("3000"), Decimal("5500"))
        assert snap.suggested_regime == Regime.CHAOS

    def test_problem_suggests_trending_down(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        snap = rm.update_portfolio(Decimal("3500"), Decimal("5500"))
        assert snap.suggested_regime == Regime.TRENDING_DOWN

    def test_healthy_no_suggestion(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        snap = rm.update_portfolio(Decimal("4500"), Decimal("5500"))
        assert snap.suggested_regime is None


class TestForceRiskPause:
    def test_force_risk_pause(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        assert rm.pause_state == PauseState.ACTIVE_TRADING
        rm.force_risk_pause()
        assert rm.pause_state == PauseState.RISK_PAUSE_ACTIVE
        assert rm.risk_pauses == 1

    def test_force_risk_pause_idempotent(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.force_risk_pause()
        rm.force_risk_pause()
        assert rm.risk_pauses == 1  # Only counted once


class TestForceActive:
    def test_force_active_resets(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("3000"), Decimal("5500"))
        assert rm.pause_state == PauseState.RISK_PAUSE_ACTIVE
        rm.force_active()
        assert rm.pause_state == PauseState.ACTIVE_TRADING


class TestMetrics:
    def test_risk_pause_counter(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("3000"), Decimal("5500"))
        assert rm.risk_pauses == 1

    def test_emergency_counter(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("2500"), Decimal("5500"))
        assert rm.emergency_overrides == 1


class TestTrailingDrawdownStop:
    """Tests for dynamic trailing stop threshold tightening."""

    def test_initial_thresholds_match_base(self) -> None:
        rm = RiskManager(
            initial_portfolio_usd=Decimal("10000"),
            max_drawdown_pct=0.15,
            emergency_drawdown_pct=0.20,
            trailing_stop_enabled=True,
        )
        assert rm.effective_max_dd_pct == 0.15
        assert rm.effective_emergency_dd_pct == 0.20

    def test_thresholds_tighten_on_portfolio_growth(self) -> None:
        rm = RiskManager(
            initial_portfolio_usd=Decimal("10000"),
            max_drawdown_pct=0.15,
            emergency_drawdown_pct=0.20,
            trailing_stop_enabled=True,
            trailing_stop_tighten_pct=0.02,
        )
        # Portfolio doubles: growth = 100%, tighten = 100% * 0.02 = 0.02
        rm.update_portfolio(Decimal("15000"), Decimal("5000"))
        assert rm.effective_max_dd_pct < 0.15
        assert rm.effective_emergency_dd_pct < 0.20

    def test_thresholds_never_below_floor(self) -> None:
        """Thresholds should never tighten below 50% of base."""
        rm = RiskManager(
            initial_portfolio_usd=Decimal("10000"),
            max_drawdown_pct=0.15,
            emergency_drawdown_pct=0.20,
            trailing_stop_enabled=True,
            trailing_stop_tighten_pct=0.10,  # Very aggressive tightening
        )
        # Portfolio grows 10x: growth = 900%, tighten = 900% * 0.10 = 0.90
        rm.update_portfolio(Decimal("90000"), Decimal("10000"))
        # Floor is 50% of base: 0.075 and 0.10
        assert rm.effective_max_dd_pct >= 0.15 * 0.5
        assert rm.effective_emergency_dd_pct >= 0.20 * 0.5

    def test_emergency_never_below_max(self) -> None:
        """Emergency threshold should always be >= max threshold."""
        rm = RiskManager(
            initial_portfolio_usd=Decimal("10000"),
            max_drawdown_pct=0.15,
            emergency_drawdown_pct=0.20,
            trailing_stop_enabled=True,
            trailing_stop_tighten_pct=0.05,
        )
        rm.update_portfolio(Decimal("15000"), Decimal("5000"))
        assert rm.effective_emergency_dd_pct >= rm.effective_max_dd_pct

    def test_disabled_trailing_no_tightening(self) -> None:
        """When trailing is disabled, thresholds should not change."""
        rm = RiskManager(
            initial_portfolio_usd=Decimal("10000"),
            max_drawdown_pct=0.15,
            emergency_drawdown_pct=0.20,
            trailing_stop_enabled=False,
        )
        rm.update_portfolio(Decimal("15000"), Decimal("5000"))
        assert rm.effective_max_dd_pct == 0.15
        assert rm.effective_emergency_dd_pct == 0.20

    def test_no_tightening_without_growth(self) -> None:
        """No tightening when portfolio hasn't grown."""
        rm = RiskManager(
            initial_portfolio_usd=Decimal("10000"),
            max_drawdown_pct=0.15,
            emergency_drawdown_pct=0.20,
            trailing_stop_enabled=True,
            trailing_stop_tighten_pct=0.02,
        )
        # Portfolio at same level
        rm.update_portfolio(Decimal("5000"), Decimal("5000"))
        assert rm.effective_max_dd_pct == 0.15
        assert rm.effective_emergency_dd_pct == 0.20

    def test_progressive_tightening(self) -> None:
        """Thresholds should tighten progressively as portfolio grows."""
        rm = RiskManager(
            initial_portfolio_usd=Decimal("10000"),
            max_drawdown_pct=0.15,
            emergency_drawdown_pct=0.20,
            trailing_stop_enabled=True,
            trailing_stop_tighten_pct=0.02,
        )
        # Growth 1: 20% growth
        rm.update_portfolio(Decimal("7000"), Decimal("5000"))
        dd1 = rm.effective_max_dd_pct
        # Growth 2: 50% growth
        rm.update_portfolio(Decimal("10000"), Decimal("5000"))
        dd2 = rm.effective_max_dd_pct
        assert dd2 < dd1  # More growth = tighter threshold

    def test_velocity_hysteresis(self) -> None:
        """Price velocity should use hysteresis for unfreezing."""
        rm = RiskManager(
            price_velocity_freeze_pct=0.03,
            price_velocity_window_sec=60,
            price_velocity_cooldown_sec=0,  # Instant cooldown
        )
        rm.check_price_velocity(Decimal("85000"))
        # Trigger freeze with 4% move
        assert rm.check_price_velocity(Decimal("81500")) is True
        # After cooldown, check hysteresis — still volatile
        # The unfreeze check looks at current velocity vs 50% threshold
