"""Regime Router — classifies market regime and gates capital allocation.

Combines multiple signals to classify the current market regime:
  - RANGE_BOUND: Low volatility, mean-reverting → full grid
  - TRENDING_UP: Rising prices, above moving average → asymmetric buy-heavy grid
  - TRENDING_DOWN: Falling prices, below MA → asymmetric sell-heavy grid
  - CHAOS: Extreme volatility or flash crash → cancel all, full pause

Inputs: EWMA volatility, price momentum, order book imbalance, flow toxicity.
Output: Regime enum + allocation parameters for the Inventory Arbiter.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal  # noqa: TC003

from icryptotrader.types import Regime

logger = logging.getLogger(__name__)

# Default regime thresholds
DEFAULT_HIGH_VOL_THRESHOLD = 0.04  # 4% daily vol → chaos candidate
DEFAULT_MOMENTUM_THRESHOLD = 0.02  # 2% price change → trending
DEFAULT_CHAOS_VOL_THRESHOLD = 0.08  # 8% daily vol → hard chaos


@dataclass
class RegimeSignals:
    """Raw signals for regime classification."""

    ewma_volatility: float = 0.0
    price_momentum: float = 0.0  # Signed: positive = up, negative = down
    order_book_imbalance: float = 0.0  # -1 to +1, positive = buy pressure
    flow_toxicity: float = 0.0  # 0 to 1, high = toxic flow


@dataclass
class RegimeDecision:
    """Result of regime classification."""

    regime: Regime
    confidence: float  # 0 to 1
    signals: RegimeSignals
    reason: str
    grid_levels_buy: int
    grid_levels_sell: int


class RegimeRouter:
    """Classifies market regime from multiple signal sources.

    Usage:
        router = RegimeRouter()
        router.update_price(Decimal("85000"))
        decision = router.classify()
    """

    def __init__(
        self,
        high_vol_threshold: float = DEFAULT_HIGH_VOL_THRESHOLD,
        chaos_vol_threshold: float = DEFAULT_CHAOS_VOL_THRESHOLD,
        momentum_threshold: float = DEFAULT_MOMENTUM_THRESHOLD,
        toxicity_threshold: float = 0.8,
        ewma_span: int = 20,
        momentum_window: int = 60,
        default_buy_levels: int = 5,
        default_sell_levels: int = 5,
    ) -> None:
        self._high_vol = high_vol_threshold
        self._chaos_vol = chaos_vol_threshold
        self._momentum_threshold = momentum_threshold
        self._toxicity_threshold = toxicity_threshold
        self._ewma_span = ewma_span
        self._momentum_window = momentum_window
        self._default_buy = default_buy_levels
        self._default_sell = default_sell_levels

        # EWMA state
        self._ewma_var: float = 0.0
        self._ewma_alpha: float = 2.0 / (ewma_span + 1)
        self._last_price: Decimal | None = None
        self._price_initialized = False

        # Price history for momentum
        self._price_history: deque[tuple[float, Decimal]] = deque(maxlen=momentum_window)

        # External signals
        self._obi: float = 0.0
        self._toxicity: float = 0.0

        # Current regime
        self._regime = Regime.RANGE_BOUND
        self._regime_since: float = time.monotonic()

        # Metrics
        self.regime_changes: int = 0

    @property
    def regime(self) -> Regime:
        return self._regime

    @property
    def ewma_volatility(self) -> float:
        """Annualized EWMA volatility estimate."""
        return self._ewma_var ** 0.5

    def update_price(self, price: Decimal) -> None:
        """Update with a new price observation. Call on every tick."""
        now = time.monotonic()
        self._price_history.append((now, price))

        if self._last_price is not None and self._last_price > 0:
            ret = float((price - self._last_price) / self._last_price)
            if self._price_initialized:
                self._ewma_var = (
                    (1 - self._ewma_alpha) * self._ewma_var
                    + self._ewma_alpha * ret * ret
                )
            else:
                self._ewma_var = ret * ret
                self._price_initialized = True

        self._last_price = price

    def update_order_book_imbalance(self, obi: float) -> None:
        """Update order book imbalance signal. Range: -1 to +1."""
        self._obi = max(-1.0, min(1.0, obi))

    def update_flow_toxicity(self, toxicity: float) -> None:
        """Update flow toxicity signal. Range: 0 to 1."""
        self._toxicity = max(0.0, min(1.0, toxicity))

    def classify(self) -> RegimeDecision:
        """Classify current market regime based on all signals.

        Call after updating price and other signals.
        """
        vol = self.ewma_volatility
        momentum = self._compute_momentum()

        signals = RegimeSignals(
            ewma_volatility=vol,
            price_momentum=momentum,
            order_book_imbalance=self._obi,
            flow_toxicity=self._toxicity,
        )

        # 1. Chaos: extreme volatility or high toxicity during high vol
        if vol >= self._chaos_vol:
            return self._set_regime(
                Regime.CHAOS, signals, 0.9,
                f"Extreme volatility ({vol:.3f})",
                grid_buy=0, grid_sell=0,
            )

        if vol >= self._high_vol and self._toxicity >= self._toxicity_threshold:
            return self._set_regime(
                Regime.CHAOS, signals, 0.8,
                f"High vol ({vol:.3f}) + toxic flow ({self._toxicity:.2f})",
                grid_buy=0, grid_sell=0,
            )

        # 2. Trending: significant momentum
        if momentum > self._momentum_threshold:
            confidence = min(1.0, momentum / (self._momentum_threshold * 2))
            return self._set_regime(
                Regime.TRENDING_UP, signals, confidence,
                f"Upward momentum ({momentum:.3f})",
                grid_buy=self._default_buy, grid_sell=max(1, self._default_sell - 2),
            )

        if momentum < -self._momentum_threshold:
            confidence = min(1.0, abs(momentum) / (self._momentum_threshold * 2))
            return self._set_regime(
                Regime.TRENDING_DOWN, signals, confidence,
                f"Downward momentum ({momentum:.3f})",
                grid_buy=max(1, self._default_buy - 2), grid_sell=self._default_sell,
            )

        # 3. Range-bound: default
        return self._set_regime(
            Regime.RANGE_BOUND, signals, 0.6,
            "Low volatility, no strong trend",
            grid_buy=self._default_buy, grid_sell=self._default_sell,
        )

    def override_regime(self, regime: Regime, reason: str = "manual") -> None:
        """Force a regime (e.g., from risk manager suggestion)."""
        if regime != self._regime:
            old = self._regime
            self._regime = regime
            self._regime_since = time.monotonic()
            self.regime_changes += 1
            logger.warning("Regime override: %s → %s (%s)", old.value, regime.value, reason)

    def _compute_momentum(self) -> float:
        """Compute price momentum from recent price history."""
        if len(self._price_history) < 2:
            return 0.0
        oldest = self._price_history[0][1]
        newest = self._price_history[-1][1]
        if oldest <= 0:
            return 0.0
        return float((newest - oldest) / oldest)

    def _set_regime(
        self,
        regime: Regime,
        signals: RegimeSignals,
        confidence: float,
        reason: str,
        grid_buy: int,
        grid_sell: int,
    ) -> RegimeDecision:
        if regime != self._regime:
            old = self._regime
            self._regime = regime
            self._regime_since = time.monotonic()
            self.regime_changes += 1
            logger.info("Regime change: %s → %s (%s)", old.value, regime.value, reason)

        return RegimeDecision(
            regime=regime,
            confidence=confidence,
            signals=signals,
            reason=reason,
            grid_levels_buy=grid_buy,
            grid_levels_sell=grid_sell,
        )
