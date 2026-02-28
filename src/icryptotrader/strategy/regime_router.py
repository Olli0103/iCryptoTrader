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
import math
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
    order_size_scale: float = 1.0  # Multiplier for order_size_usd


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

        # EWMA state (tick-level, kept for backward compat)
        self._ewma_var: float = 0.0
        self._ewma_alpha: float = 2.0 / (ewma_span + 1)
        self._last_price: Decimal | None = None
        self._price_initialized = False

        # Multi-timeframe volatility: 1m, 5m, 15m rolling windows
        # Stores (timestamp, price) and computes true time-weighted returns
        self._vol_windows: dict[int, deque[tuple[float, Decimal]]] = {
            60: deque(maxlen=600),    # 1-min samples (keep 10 min of ticks)
            300: deque(maxlen=600),   # 5-min samples
            900: deque(maxlen=600),   # 15-min samples
        }
        self._vol_estimates: dict[int, float] = {60: 0.0, 300: 0.0, 900: 0.0}

        # Price history for momentum
        self._price_history: deque[tuple[float, Decimal]] = deque(maxlen=momentum_window)

        # VWAP tracking
        self._trade_history: deque[tuple[Decimal, Decimal]] = deque(maxlen=500)
        self._vwap_value: Decimal | None = None

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
        """Multi-timeframe volatility: weighted blend of 1m/5m/15m windows.

        Falls back to tick-level EWMA if insufficient data in windows.
        """
        v1 = self._vol_estimates.get(60, 0.0)
        v5 = self._vol_estimates.get(300, 0.0)
        v15 = self._vol_estimates.get(900, 0.0)

        # Use multi-timeframe if we have at least 1-minute data
        if v1 > 0:
            # Weight: 50% 1-min, 30% 5-min, 20% 15-min
            blended = 0.5 * v1 + 0.3 * v5 + 0.2 * v15
            return blended

        # Fallback to tick-level EWMA
        return float(self._ewma_var ** 0.5)

    def update_price(self, price: Decimal) -> None:
        """Update with a new price observation. Call on every tick."""
        now = time.monotonic()
        self._price_history.append((now, price))

        # Tick-level EWMA (backward compat)
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

        # Feed multi-timeframe volatility windows
        for window_sec, buf in self._vol_windows.items():
            buf.append((now, price))
            self._vol_estimates[window_sec] = self._compute_windowed_vol(
                buf, window_sec,
            )

    def _compute_windowed_vol(
        self,
        buf: deque[tuple[float, Decimal]],
        window_sec: int,
    ) -> float:
        """Compute realized volatility over a rolling time window.

        Uses the actual elapsed time for scaling (not the nominal window),
        so fast-ticking tests and slow-ticking prod both get correct values.
        """
        if len(buf) < 2:
            return 0.0

        now = buf[-1][0]
        cutoff = now - window_sec
        # Find the oldest entry within the window
        oldest_idx = 0
        for i, (t, _) in enumerate(buf):
            if t >= cutoff:
                oldest_idx = i
                break

        if oldest_idx >= len(buf) - 1:
            return 0.0

        oldest_t = buf[oldest_idx][0]
        oldest_price = buf[oldest_idx][1]
        newest_price = buf[-1][1]
        if oldest_price <= 0:
            return 0.0

        elapsed = now - oldest_t
        # Need at least 1 second of real elapsed time for meaningful vol
        if elapsed < 1.0:
            return 0.0

        # Simple return over the elapsed period, scaled to daily equivalent
        ret = abs(float((newest_price - oldest_price) / oldest_price))
        daily_scale = math.sqrt(86400.0 / elapsed)
        return ret * daily_scale

    def update_order_book_imbalance(self, obi: float) -> None:
        """Update order book imbalance signal. Range: -1 to +1."""
        self._obi = max(-1.0, min(1.0, obi))

    def update_flow_toxicity(self, toxicity: float) -> None:
        """Update flow toxicity signal. Range: 0 to 1."""
        self._toxicity = max(0.0, min(1.0, toxicity))

    def update_trade(self, price: Decimal, quantity: Decimal) -> None:
        """Record a trade for VWAP calculation."""
        self._trade_history.append((price, quantity))
        # Recompute VWAP
        total_pq = sum((p * q for p, q in self._trade_history), Decimal("0"))
        total_q = sum((q for _, q in self._trade_history), Decimal("0"))
        if total_q > 0:
            self._vwap_value = total_pq / total_q

    @property
    def vwap(self) -> Decimal | None:
        """Volume-weighted average price from recent trades. None if no trades."""
        return self._vwap_value

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
                grid_buy=0, grid_sell=0, size_scale=0.5,
            )

        if vol >= self._high_vol and self._toxicity >= self._toxicity_threshold:
            return self._set_regime(
                Regime.CHAOS, signals, 0.8,
                f"High vol ({vol:.3f}) + toxic flow ({self._toxicity:.2f})",
                grid_buy=0, grid_sell=0, size_scale=0.5,
            )

        # 2. Trending: significant momentum
        if momentum > self._momentum_threshold:
            confidence = min(1.0, momentum / (self._momentum_threshold * 2))
            return self._set_regime(
                Regime.TRENDING_UP, signals, confidence,
                f"Upward momentum ({momentum:.3f})",
                grid_buy=self._default_buy, grid_sell=max(1, self._default_sell - 2),
                size_scale=0.75,
            )

        if momentum < -self._momentum_threshold:
            confidence = min(1.0, abs(momentum) / (self._momentum_threshold * 2))
            return self._set_regime(
                Regime.TRENDING_DOWN, signals, confidence,
                f"Downward momentum ({momentum:.3f})",
                grid_buy=max(1, self._default_buy - 2), grid_sell=self._default_sell,
                size_scale=0.75,
            )

        # 3. Range-bound: default
        return self._set_regime(
            Regime.RANGE_BOUND, signals, 0.6,
            "Low volatility, no strong trend",
            grid_buy=self._default_buy, grid_sell=self._default_sell,
            size_scale=1.0,
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
        """Compute price momentum over a fixed time window.

        Uses the ``momentum_window`` (seconds) to find the oldest price
        within that horizon, ensuring a consistent time base regardless
        of tick rate.  Previously used a fixed tick count, which made
        the horizon expand/contract with market activity.
        """
        if len(self._price_history) < 2:
            return 0.0

        newest_t, newest_price = self._price_history[-1]
        cutoff = newest_t - self._momentum_window

        # Find the oldest entry within the momentum window
        ref_price: Decimal | None = None
        for t, p in self._price_history:
            if t >= cutoff:
                ref_price = p
                break

        if ref_price is None or ref_price <= 0:
            return 0.0
        return float((newest_price - ref_price) / ref_price)

    def _set_regime(
        self,
        regime: Regime,
        signals: RegimeSignals,
        confidence: float,
        reason: str,
        grid_buy: int,
        grid_sell: int,
        size_scale: float = 1.0,
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
            order_size_scale=size_scale,
        )
