"""Bollinger Band + ATR volatility-adaptive grid spacing.

Uses two complementary volatility measures:
  1. Bollinger Band width: statistical deviation from mean price
  2. ATR (Average True Range): actual price range per period

Wider bands/ATR → wider spacing (avoid adverse selection)
Narrower bands/ATR → tighter spacing (capture more round-trips)

The final spacing is a weighted blend of both signals, floored at
the fee-model minimum and capped at a configurable maximum.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class BollingerState:
    """Current Bollinger Band state."""

    sma: Decimal
    upper: Decimal
    lower: Decimal
    band_width_bps: Decimal
    std_dev: Decimal
    suggested_spacing_bps: Decimal
    atr_bps: Decimal | None = None  # ATR in basis points (if enabled)


class BollingerSpacing:
    """Computes volatility-adaptive grid spacing from Bollinger Bands + ATR.

    Each call to update() adds a mid-price observation. Once the rolling
    window is full, band width is computed and mapped to spacing.

    Spacing formula:
        bb_spacing = band_width_bps * spacing_scale
        atr_spacing = atr_bps * spacing_scale
        blended = (1 - atr_weight) * bb_spacing + atr_weight * atr_spacing
        final = clamp(blended, min_spacing_bps, max_spacing_bps)
    """

    def __init__(
        self,
        window: int = 20,
        multiplier: Decimal = Decimal("2.0"),
        spacing_scale: Decimal = Decimal("0.5"),
        min_spacing_bps: Decimal = Decimal("15"),
        max_spacing_bps: Decimal = Decimal("200"),
        atr_enabled: bool = True,
        atr_window: int = 14,
        atr_weight: float = 0.3,
    ) -> None:
        self._window = max(2, window)
        self._multiplier = multiplier
        self._spacing_scale = spacing_scale
        self._min_spacing_bps = min_spacing_bps
        self._max_spacing_bps = max_spacing_bps
        self._prices: deque[Decimal] = deque(maxlen=self._window)
        self._state: BollingerState | None = None

        # ATR state
        self._atr_enabled = atr_enabled
        self._atr_window = max(2, atr_window)
        self._atr_weight = max(0.0, min(1.0, atr_weight))
        self._highs: deque[Decimal] = deque(maxlen=self._atr_window + 1)
        self._lows: deque[Decimal] = deque(maxlen=self._atr_window + 1)
        self._closes: deque[Decimal] = deque(maxlen=self._atr_window + 1)
        self._atr_value: Decimal | None = None

    @property
    def state(self) -> BollingerState | None:
        """Current Bollinger state, or None if window not yet full."""
        return self._state

    @property
    def suggested_spacing_bps(self) -> Decimal | None:
        """Suggested grid spacing in bps, or None if not yet ready."""
        return self._state.suggested_spacing_bps if self._state else None

    @property
    def atr(self) -> Decimal | None:
        """Current ATR value in absolute terms, or None if not ready."""
        return self._atr_value

    def update(
        self,
        mid_price: Decimal,
        high: Decimal | None = None,
        low: Decimal | None = None,
    ) -> BollingerState | None:
        """Add a price observation and recompute bands + ATR.

        Args:
            mid_price: Current mid/close price.
            high: Period high (if available). Defaults to mid_price.
            low: Period low (if available). Defaults to mid_price.

        Returns the new state, or None if the window is not yet full.
        """
        self._prices.append(mid_price)

        # Track high/low/close for ATR
        if self._atr_enabled:
            self._highs.append(high if high is not None else mid_price)
            self._lows.append(low if low is not None else mid_price)
            self._closes.append(mid_price)
            self._compute_atr()

        if len(self._prices) < self._window:
            self._state = None
            return None

        # SMA
        sma = sum(self._prices, Decimal("0")) / len(self._prices)

        if sma <= 0:
            self._state = None
            return None

        # Standard deviation
        variance = sum(
            ((p - sma) ** 2 for p in self._prices), Decimal("0"),
        ) / len(self._prices)
        std_dev = Decimal(str(math.sqrt(float(variance))))

        # Bands
        band_offset = self._multiplier * std_dev
        upper = sma + band_offset
        lower = sma - band_offset

        # Band width in bps
        band_width_bps = (upper - lower) / sma * 10000

        # Bollinger-based spacing
        bb_spacing = band_width_bps * self._spacing_scale

        # Blend with ATR if available
        atr_bps = None
        if self._atr_enabled and self._atr_value is not None and sma > 0:
            atr_bps = (self._atr_value / sma) * 10000
            atr_spacing = atr_bps * self._spacing_scale
            w = Decimal(str(self._atr_weight))
            raw_spacing = (Decimal("1") - w) * bb_spacing + w * atr_spacing
        else:
            raw_spacing = bb_spacing

        spacing = max(
            self._min_spacing_bps,
            min(self._max_spacing_bps, raw_spacing),
        )

        self._state = BollingerState(
            sma=sma,
            upper=upper,
            lower=lower,
            band_width_bps=band_width_bps,
            std_dev=std_dev,
            suggested_spacing_bps=spacing,
            atr_bps=atr_bps,
        )
        return self._state

    def _compute_atr(self) -> None:
        """Compute Average True Range from high/low/close history."""
        n = len(self._closes)
        if n < 2:
            self._atr_value = None
            return

        true_ranges: list[Decimal] = []
        for i in range(1, n):
            high = self._highs[i]
            low = self._lows[i]
            prev_close = self._closes[i - 1]

            # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        if not true_ranges:
            self._atr_value = None
            return

        # Simple average of true ranges (SMA-based ATR)
        self._atr_value = sum(true_ranges, Decimal("0")) / len(true_ranges)

    def reset(self) -> None:
        """Clear all state."""
        self._prices.clear()
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._state = None
        self._atr_value = None
