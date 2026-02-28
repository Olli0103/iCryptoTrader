"""Hedge Manager — portfolio delta reduction during adverse conditions.

Monitors portfolio exposure and reduces BTC delta when drawdown exceeds
thresholds or regime enters chaos. On a spot exchange (no shorting), hedging
is implemented as exposure reduction:

Strategies:
  - reduce_exposure: Cancel buy orders, reduce grid levels, let sells fill.
  - inverse_grid: Place additional sell orders at tighter spacing to
    accelerate exposure reduction.

Works with the existing PauseState system — does not override risk manager
decisions, but can recommend grid modifications to tick().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from icryptotrader.types import PauseState, Regime

logger = logging.getLogger(__name__)


@dataclass
class HedgeAction:
    """Recommendation from the hedge manager for the current tick."""

    active: bool = False
    buy_level_cap: int | None = None  # Max buy levels (None = no change)
    sell_level_boost: int = 0  # Additional sell levels to add
    sell_spacing_tighten_pct: float = 0.0  # Tighten sell spacing by this %
    reason: str = ""


class HedgeManager:
    """Monitors drawdown and regime to recommend exposure reduction.

    Usage:
        hm = HedgeManager(trigger_drawdown_pct=0.10)
        action = hm.evaluate(
            drawdown_pct=0.12,
            regime=Regime.TRENDING_DOWN,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.65,
            target_allocation_pct=0.50,
        )
        if action.active:
            num_buy = min(num_buy, action.buy_level_cap or num_buy)
    """

    def __init__(
        self,
        trigger_drawdown_pct: float = 0.10,
        strategy: str = "reduce_exposure",
        max_reduction_pct: float = 0.50,
    ) -> None:
        self._trigger_dd = trigger_drawdown_pct
        self._strategy = strategy
        self._max_reduction = max_reduction_pct
        self._active = False
        self._activations: int = 0

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def activations(self) -> int:
        return self._activations

    def evaluate(
        self,
        drawdown_pct: float,
        regime: Regime,
        pause_state: PauseState,
        btc_allocation_pct: float,
        target_allocation_pct: float,
        current_buy_levels: int = 5,
        current_sell_levels: int = 5,
    ) -> HedgeAction:
        """Evaluate whether hedging is needed and return recommended action."""
        # Don't hedge if already paused
        if pause_state in (
            PauseState.RISK_PAUSE_ACTIVE,
            PauseState.DUAL_LOCK,
            PauseState.EMERGENCY_SELL,
        ):
            self._active = False
            return HedgeAction(reason="risk_paused")

        # Activation conditions
        should_hedge = (
            drawdown_pct >= self._trigger_dd
            or regime == Regime.CHAOS
            or (
                regime == Regime.TRENDING_DOWN
                and btc_allocation_pct > target_allocation_pct + 0.10
            )
        )

        # Deactivation with hysteresis
        if self._active and not should_hedge:
            # Only deactivate if drawdown recovered to 50% of trigger
            if drawdown_pct < self._trigger_dd * 0.5:
                self._active = False
                return HedgeAction(reason="hedge_deactivated")
            # Still active (hysteresis)
            should_hedge = True

        if not should_hedge:
            self._active = False
            return HedgeAction(reason="no_hedge_needed")

        if not self._active:
            self._active = True
            self._activations += 1
            logger.warning(
                "HedgeManager: ACTIVATED (dd=%.1f%%, regime=%s, alloc=%.1f%%)",
                drawdown_pct * 100, regime.value, btc_allocation_pct * 100,
            )

        if self._strategy == "reduce_exposure":
            return self._reduce_exposure(
                drawdown_pct, btc_allocation_pct,
                target_allocation_pct, current_buy_levels,
            )
        return self._inverse_grid(
            drawdown_pct, current_sell_levels,
        )

    def _reduce_exposure(
        self,
        drawdown_pct: float,
        btc_alloc: float,
        target_alloc: float,
        current_buy_levels: int,
    ) -> HedgeAction:
        """Reduce exposure by capping buy levels."""
        # Scale reduction with drawdown severity
        severity = min(1.0, drawdown_pct / (self._trigger_dd * 2))
        reduction = severity * self._max_reduction

        # Cap buy levels proportionally
        cap = max(0, int(current_buy_levels * (1.0 - reduction)))

        # If over-allocated, be more aggressive
        if btc_alloc > target_alloc + 0.15:
            cap = 0

        return HedgeAction(
            active=True,
            buy_level_cap=cap,
            reason=f"reduce_exposure(severity={severity:.1%}, cap={cap})",
        )

    def _inverse_grid(
        self,
        drawdown_pct: float,
        current_sell_levels: int,
    ) -> HedgeAction:
        """Add extra sell levels with tighter spacing."""
        severity = min(1.0, drawdown_pct / (self._trigger_dd * 2))

        # Add 1-3 extra sell levels based on severity
        extra_sells = max(1, int(severity * 3))

        # Tighten sell spacing by up to 30%
        tighten = severity * 0.30

        return HedgeAction(
            active=True,
            buy_level_cap=max(0, current_sell_levels - extra_sells),
            sell_level_boost=extra_sells,
            sell_spacing_tighten_pct=tighten,
            reason=f"inverse_grid(+{extra_sells} sells, tighten={tighten:.0%})",
        )
