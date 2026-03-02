"""Hedge Manager — portfolio delta reduction during adverse conditions.

Monitors portfolio exposure and reduces BTC delta when drawdown exceeds
thresholds or regime enters chaos. On a spot exchange (no shorting), hedging
is implemented as exposure reduction:

Strategies:
  - reduce_exposure: Cancel buy orders, reduce grid levels, let sells fill.
  - inverse_grid: Place additional sell orders at tighter spacing to
    accelerate exposure reduction.

Collateral Segregation (§23 EStG Tax Protection):
  If futures hedging is ever enabled, the hedge account MUST use Strict
  Isolated Margin funded exclusively with fiat (USD/EUR) or stablecoins
  (USDT/USDC).  Cross-margining spot BTC as collateral for derivatives
  creates a catastrophic tax risk: if a futures position is liquidated,
  the exchange seizes and sells spot BTC, resetting the 365-day Haltefrist
  on those lots and triggering unexpected taxable events at up to 45%.

Works with the existing PauseState system — does not override risk manager
decisions, but can recommend grid modifications to tick().
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from icryptotrader.types import PauseState, Regime

logger = logging.getLogger(__name__)


class MarginMode(Enum):
    """Margin mode for derivative hedge positions.

    ISOLATED: Each position has its own margin pool — liquidation cannot
              touch other positions or spot holdings.
    CROSS:    All positions share a single margin pool — liquidation of
              any position can seize spot crypto as collateral.

    CROSS margin is PROHIBITED because it allows the exchange to
    involuntarily sell spot BTC lots to cover derivative losses,
    resetting the §23 EStG Haltefrist on those lots.
    """

    ISOLATED = "isolated"
    CROSS = "cross"


# Collateral types that are safe for derivative hedging.
# Only fiat and stablecoins — never spot crypto holdings.
SAFE_COLLATERAL_TYPES = frozenset({"USD", "EUR", "USDT", "USDC", "DAI"})


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
        margin_mode: MarginMode = MarginMode.ISOLATED,
    ) -> None:
        self._trigger_dd = trigger_drawdown_pct
        self._strategy = strategy
        self._max_reduction = max_reduction_pct
        self._active = False
        self._activations: int = 0
        self._margin_mode = margin_mode
        self._collateral_violations: int = 0

        # Enforce isolated margin — cross margin is a tax catastrophe
        if margin_mode == MarginMode.CROSS:
            raise ValueError(
                "CROSS margin is prohibited: derivative liquidation would "
                "seize spot BTC lots and reset the §23 EStG Haltefrist. "
                "Use MarginMode.ISOLATED with fiat/stablecoin collateral."
            )

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def activations(self) -> int:
        return self._activations

    @property
    def margin_mode(self) -> MarginMode:
        return self._margin_mode

    @property
    def collateral_violations(self) -> int:
        return self._collateral_violations

    def validate_collateral(self, collateral_type: str) -> bool:
        """Check if the proposed collateral type is safe for hedging.

        Only fiat and stablecoins are permitted.  Using spot crypto
        (BTC, ETH, etc.) as derivative collateral creates a tax bomb:
        forced liquidation sells the spot lots and resets Haltefrist.

        Args:
            collateral_type: Currency code (e.g., "USD", "BTC", "USDT").

        Returns:
            True if the collateral is safe, False if it would create
            a cross-collateral tax risk.
        """
        is_safe = collateral_type.upper() in SAFE_COLLATERAL_TYPES
        if not is_safe:
            self._collateral_violations += 1
            logger.critical(
                "COLLATERAL VIOLATION: %s is not a safe collateral type "
                "for derivative hedging — would risk §23 EStG Haltefrist "
                "reset on forced liquidation. Use fiat or stablecoins only. "
                "(violation #%d)",
                collateral_type, self._collateral_violations,
            )
        return is_safe

    @staticmethod
    def hedge_contracts(
        delta_btc: Decimal,
        contract_size_btc: Decimal,
    ) -> int:
        """Compute the integer number of contracts to hedge spot delta.

        Uses math.floor() to avoid the sliver deadlock: spot crypto has
        8 decimal places but derivative contracts have quantized integer
        multipliers.  Attempting to perfectly balance fractional deltas
        causes an infinite loop of failed fractional orders.

        The remaining un-hedgeable sliver (``delta_btc % contract_size_btc``)
        is accepted as standard structural risk.

        Args:
            delta_btc: Spot delta to hedge (in BTC). Always positive.
            contract_size_btc: Size of one derivative contract (in BTC).
                E.g., Decimal("0.01") for a 0.01 BTC contract.

        Returns:
            Number of integer contracts to execute (always >= 0).
        """
        if contract_size_btc <= 0 or delta_btc <= 0:
            return 0
        return math.floor(delta_btc / contract_size_btc)

    @staticmethod
    def unhedgeable_sliver(
        delta_btc: Decimal,
        contract_size_btc: Decimal,
    ) -> Decimal:
        """Return the un-hedgeable BTC sliver after integer floor.

        This is the residual exposure that cannot be offset because
        derivative contracts have quantized (integer) multipliers.
        """
        if contract_size_btc <= 0 or delta_btc <= 0:
            return Decimal("0")
        contracts = math.floor(delta_btc / contract_size_btc)
        hedged = contract_size_btc * contracts
        return delta_btc - hedged

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
