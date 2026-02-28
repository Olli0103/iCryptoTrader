"""Risk Manager — drawdown classification, allocation enforcement, pause states.

Implements the pause state machine from the architecture plan:
  ACTIVE_TRADING → TAX_LOCK_ACTIVE (buy-only)
  ACTIVE_TRADING → RISK_PAUSE_ACTIVE (no trading)
  TAX_LOCK_ACTIVE → DUAL_LOCK (full stop)
  DUAL_LOCK → EMERGENCY_SELL (DD > 20%)

Drawdown classification:
  0-5%: Healthy
  5-10%: Warning (reduce grid)
  10-15%: Problem (trending_down regime)
  >= 15%: Critical (chaos regime, cancel all)
  >= 20%: Emergency (tax override, force sell)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto

from icryptotrader.types import PauseState, Regime

logger = logging.getLogger(__name__)


class DrawdownLevel(Enum):
    """Classification of portfolio drawdown severity."""

    HEALTHY = auto()
    WARNING = auto()
    PROBLEM = auto()
    CRITICAL = auto()
    EMERGENCY = auto()


@dataclass
class RiskSnapshot:
    """Current risk state snapshot for telemetry and decision-making."""

    portfolio_value_usd: Decimal
    high_water_mark_usd: Decimal
    drawdown_pct: float
    drawdown_level: DrawdownLevel
    btc_allocation_pct: float
    pause_state: PauseState
    suggested_regime: Regime | None
    suggested_grid_levels: int | None
    price_velocity_frozen: bool


class RiskManager:
    """Portfolio risk management with drawdown tracking and pause states.

    Tracks portfolio value, computes drawdown from high-water mark,
    classifies risk level, and manages the pause state machine.
    """

    def __init__(
        self,
        initial_portfolio_usd: Decimal = Decimal("5000"),
        max_drawdown_pct: float = 0.15,
        emergency_drawdown_pct: float = 0.20,
        warning_drawdown_pct: float = 0.05,
        problem_drawdown_pct: float = 0.10,
        recovery_hysteresis_pct: float = 0.05,
        price_velocity_freeze_pct: float = 0.03,
        price_velocity_window_sec: int = 60,
        price_velocity_cooldown_sec: int = 30,
        trailing_stop_enabled: bool = True,
        trailing_stop_tighten_pct: float = 0.02,
    ) -> None:
        self._base_max_dd_pct = max_drawdown_pct
        self._max_dd_pct = max_drawdown_pct
        self._base_emergency_dd_pct = emergency_drawdown_pct
        self._emergency_dd_pct = emergency_drawdown_pct
        self._warning_dd_pct = warning_drawdown_pct
        self._problem_dd_pct = problem_drawdown_pct
        self._recovery_hysteresis_pct = recovery_hysteresis_pct
        self._velocity_freeze_pct = price_velocity_freeze_pct
        self._velocity_window_sec = price_velocity_window_sec
        self._velocity_cooldown_sec = price_velocity_cooldown_sec

        # Trailing stop — tightens thresholds as portfolio grows
        self._trailing_enabled = trailing_stop_enabled
        self._trailing_tighten_pct = trailing_stop_tighten_pct
        self._initial_portfolio = initial_portfolio_usd

        # Portfolio tracking
        self._hwm = initial_portfolio_usd
        self._portfolio_value = initial_portfolio_usd

        # Pause state machine
        self._pause_state = PauseState.ACTIVE_TRADING
        self._tax_locked = False

        # Price velocity circuit breaker — deque avoids O(n) list rebuild on prune
        self._price_history: deque[tuple[float, Decimal]] = deque()
        self._velocity_frozen = False
        self._velocity_unfreeze_at: float = 0.0

        # Metrics
        self.risk_pauses: int = 0
        self.emergency_overrides: int = 0
        self.velocity_freezes: int = 0

    @property
    def pause_state(self) -> PauseState:
        return self._pause_state

    @property
    def high_water_mark(self) -> Decimal:
        return self._hwm

    @property
    def drawdown_pct(self) -> float:
        if self._hwm <= 0:
            return 0.0
        return float((self._hwm - self._portfolio_value) / self._hwm)

    @property
    def is_trading_allowed(self) -> bool:
        """True if any trading (buy or sell) is allowed."""
        return self._pause_state in (
            PauseState.ACTIVE_TRADING,
            PauseState.TAX_LOCK_ACTIVE,
        )

    @property
    def is_sell_allowed(self) -> bool:
        """True if sell orders are allowed."""
        return self._pause_state == PauseState.ACTIVE_TRADING

    @property
    def is_buy_allowed(self) -> bool:
        """True if buy orders are allowed."""
        return self._pause_state in (
            PauseState.ACTIVE_TRADING,
            PauseState.TAX_LOCK_ACTIVE,
        )

    def update_portfolio(
        self,
        btc_value_usd: Decimal,
        usd_balance: Decimal,
    ) -> RiskSnapshot:
        """Update portfolio value and compute risk metrics.

        Called on every strategy tick with current valuations.
        Returns a RiskSnapshot for decision-making.
        """
        total = btc_value_usd + usd_balance
        self._portfolio_value = total

        if total > self._hwm:
            self._hwm = total
            self._update_trailing_thresholds()

        dd_pct = self.drawdown_pct
        dd_level = self._classify_drawdown(dd_pct)

        # Update pause state based on drawdown
        self._update_pause_state(dd_level)

        # Compute BTC allocation
        btc_alloc = float(btc_value_usd / total) if total > 0 else 0.0

        # Suggest regime based on drawdown
        suggested_regime = self._suggest_regime(dd_level)
        suggested_levels = self._suggest_grid_levels(dd_level)

        return RiskSnapshot(
            portfolio_value_usd=total,
            high_water_mark_usd=self._hwm,
            drawdown_pct=dd_pct,
            drawdown_level=dd_level,
            btc_allocation_pct=btc_alloc,
            pause_state=self._pause_state,
            suggested_regime=suggested_regime,
            suggested_grid_levels=suggested_levels,
            price_velocity_frozen=self._velocity_frozen,
        )

    def check_allocation(
        self,
        btc_alloc_pct: float,
        target_pct: float,
        max_pct: float,
        min_pct: float,
    ) -> tuple[bool, bool]:
        """Check if BTC allocation is within bounds.

        Returns (buy_allowed, sell_allowed) based on allocation limits.
        """
        buy_allowed = btc_alloc_pct < max_pct
        sell_allowed = btc_alloc_pct > min_pct
        return buy_allowed, sell_allowed

    def check_price_velocity(self, price: Decimal) -> bool:
        """Check if price velocity exceeds circuit breaker threshold.

        Returns True if trading should be frozen due to rapid price movement.
        """
        now = time.monotonic()

        # Check if we're in cooldown
        if self._velocity_frozen:
            if now >= self._velocity_unfreeze_at:
                # Hysteresis: only unfreeze if current velocity is below
                # recovery threshold (50% of freeze threshold)
                if len(self._price_history) >= 2:
                    oldest_price = self._price_history[0][1]
                    if oldest_price > 0:
                        current_vel = abs(float((price - oldest_price) / oldest_price))
                        if current_vel >= self._velocity_freeze_pct * 0.5:
                            # Still too volatile — extend cooldown
                            self._velocity_unfreeze_at = now + self._velocity_cooldown_sec
                            return True
                self._velocity_frozen = False
                logger.info("Price velocity circuit breaker: unfrozen (hysteresis passed)")
            else:
                return True

        # Record price
        self._price_history.append((now, price))

        # Prune old entries from the left (deque is ordered by time)
        cutoff = now - self._velocity_window_sec
        while self._price_history and self._price_history[0][0] < cutoff:
            self._price_history.popleft()

        if len(self._price_history) < 2:
            return False

        oldest_price = self._price_history[0][1]
        if oldest_price <= 0:
            return False

        velocity = abs(float((price - oldest_price) / oldest_price))

        if velocity >= self._velocity_freeze_pct:
            self._velocity_frozen = True
            self._velocity_unfreeze_at = now + self._velocity_cooldown_sec
            self.velocity_freezes += 1
            logger.warning(
                "Price velocity circuit breaker: FROZEN (%.2f%% in %ds)",
                velocity * 100, self._velocity_window_sec,
            )
            return True

        return False

    def set_tax_locked(self, locked: bool) -> None:
        """Update tax-lock status from Tax Agent."""
        self._tax_locked = locked
        self._reconcile_pause_state()

    def force_risk_pause(self) -> None:
        """Force into risk pause state (e.g., ledger mismatch)."""
        if self._pause_state != PauseState.RISK_PAUSE_ACTIVE:
            self.risk_pauses += 1
        self._pause_state = PauseState.RISK_PAUSE_ACTIVE
        logger.warning("Risk: forced RISK_PAUSE (external trigger)")

    def force_active(self) -> None:
        """Force return to active trading (manual override)."""
        self._pause_state = PauseState.ACTIVE_TRADING
        self._tax_locked = False
        logger.warning("Risk: forced return to ACTIVE_TRADING")

    def record_withdrawal(self, amount_usd: Decimal) -> None:
        """Adjust HWM down after an external withdrawal (e.g., tax payment).

        Without this, withdrawing 30% to pay the Finanzamt would register
        as a 30% drawdown and permanently freeze the bot.
        """
        if amount_usd <= 0:
            return
        old_hwm = self._hwm
        self._hwm = max(Decimal("0"), self._hwm - amount_usd)
        self._initial_portfolio = max(
            Decimal("0"), self._initial_portfolio - amount_usd,
        )
        logger.info(
            "HWM adjusted for withdrawal: %.2f → %.2f (withdrew %.2f USD)",
            old_hwm, self._hwm, amount_usd,
        )

    @property
    def effective_max_dd_pct(self) -> float:
        """Current effective max drawdown threshold (may be tightened)."""
        return self._max_dd_pct

    @property
    def effective_emergency_dd_pct(self) -> float:
        """Current effective emergency drawdown threshold."""
        return self._emergency_dd_pct

    def _update_trailing_thresholds(self) -> None:
        """Tighten drawdown thresholds as portfolio grows above initial.

        As the portfolio reaches new highs, the stop thresholds tighten
        proportionally. E.g., if portfolio doubled and tighten_pct=0.02,
        thresholds tighten by 2% (0.15 → 0.13, 0.20 → 0.18).

        Thresholds never tighten below 50% of their base value.
        """
        if not self._trailing_enabled or self._initial_portfolio <= 0:
            return

        growth = float(
            (self._hwm - self._initial_portfolio) / self._initial_portfolio,
        )
        if growth <= 0:
            return

        tighten = growth * self._trailing_tighten_pct
        floor_factor = 0.5  # Never tighten below 50% of base

        self._max_dd_pct = max(
            self._base_max_dd_pct * floor_factor,
            self._base_max_dd_pct - tighten,
        )
        self._emergency_dd_pct = max(
            self._base_emergency_dd_pct * floor_factor,
            self._base_emergency_dd_pct - tighten,
        )

        # Ensure emergency always >= max
        if self._emergency_dd_pct < self._max_dd_pct:
            self._emergency_dd_pct = self._max_dd_pct

    def _classify_drawdown(self, dd_pct: float) -> DrawdownLevel:
        if dd_pct >= self._emergency_dd_pct:
            return DrawdownLevel.EMERGENCY
        if dd_pct >= self._max_dd_pct:
            return DrawdownLevel.CRITICAL
        if dd_pct >= self._problem_dd_pct:
            return DrawdownLevel.PROBLEM
        if dd_pct >= self._warning_dd_pct:
            return DrawdownLevel.WARNING
        return DrawdownLevel.HEALTHY

    def _update_pause_state(self, dd_level: DrawdownLevel) -> None:
        if dd_level == DrawdownLevel.EMERGENCY:
            if self._pause_state != PauseState.EMERGENCY_SELL:
                self.emergency_overrides += 1
                logger.warning("Risk: EMERGENCY_SELL (DD >= %.0f%%)", self._emergency_dd_pct * 100)
            self._pause_state = PauseState.EMERGENCY_SELL

        elif dd_level == DrawdownLevel.CRITICAL:
            if self._tax_locked:
                self._pause_state = PauseState.DUAL_LOCK
            else:
                if self._pause_state != PauseState.RISK_PAUSE_ACTIVE:
                    self.risk_pauses += 1
                    logger.warning("Risk: RISK_PAUSE (DD >= %.0f%%)", self._max_dd_pct * 100)
                self._pause_state = PauseState.RISK_PAUSE_ACTIVE

        elif dd_level in (DrawdownLevel.HEALTHY, DrawdownLevel.WARNING):
            # Recovery — use hysteresis to avoid flapping
            self._reconcile_pause_state()

        # Problem level: don't change pause state, but suggest regime change

    def _reconcile_pause_state(self) -> None:
        """Reconcile pause state based on current DD and tax lock."""
        dd_pct = self.drawdown_pct
        dd_level = self._classify_drawdown(dd_pct)

        if dd_level in (DrawdownLevel.EMERGENCY, DrawdownLevel.CRITICAL):
            return  # Don't relax during high DD

        recovery_threshold = self._problem_dd_pct - self._recovery_hysteresis_pct

        if dd_pct <= recovery_threshold:
            if self._tax_locked:
                self._pause_state = PauseState.TAX_LOCK_ACTIVE
            else:
                self._pause_state = PauseState.ACTIVE_TRADING
        elif self._tax_locked and self._pause_state == PauseState.ACTIVE_TRADING:
            self._pause_state = PauseState.TAX_LOCK_ACTIVE

    def _suggest_regime(self, dd_level: DrawdownLevel) -> Regime | None:
        """Suggest a regime based on drawdown level."""
        if dd_level == DrawdownLevel.CRITICAL or dd_level == DrawdownLevel.EMERGENCY:
            return Regime.CHAOS
        if dd_level == DrawdownLevel.PROBLEM:
            return Regime.TRENDING_DOWN
        return None  # No suggestion — let regime router decide

    def _suggest_grid_levels(self, dd_level: DrawdownLevel) -> int | None:
        """Suggest grid level count based on drawdown."""
        if dd_level == DrawdownLevel.CRITICAL or dd_level == DrawdownLevel.EMERGENCY:
            return 0  # Cancel all
        if dd_level == DrawdownLevel.PROBLEM:
            return 3  # Reduced
        if dd_level == DrawdownLevel.WARNING:
            return 3  # Reduced
        return None  # Full grid
