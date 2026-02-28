"""Bollinger Band volatility-adaptive grid spacing.

Uses a rolling window of mid-prices to compute Bollinger Band width,
then maps that width to a grid spacing in basis points. Wider bands
(higher volatility) → wider spacing to avoid adverse selection; narrower
bands (lower volatility) → tighter spacing to capture more round-trips.

The spacing is floored at the fee-model minimum and capped at a
configurable maximum.
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


class BollingerSpacing:
    """Computes volatility-adaptive grid spacing from Bollinger Bands.

    Each call to update() adds a mid-price observation. Once the rolling
    window is full, band width is computed and mapped to spacing.

    Spacing formula:
        band_width_bps = (upper - lower) / sma * 10000
        raw_spacing = band_width_bps * spacing_scale
        spacing = clamp(raw_spacing, min_spacing_bps, max_spacing_bps)
    """

    def __init__(
        self,
        window: int = 20,
        multiplier: Decimal = Decimal("2.0"),
        spacing_scale: Decimal = Decimal("0.5"),
        min_spacing_bps: Decimal = Decimal("15"),
        max_spacing_bps: Decimal = Decimal("200"),
    ) -> None:
        self._window = max(2, window)
        self._multiplier = multiplier
        self._spacing_scale = spacing_scale
        self._min_spacing_bps = min_spacing_bps
        self._max_spacing_bps = max_spacing_bps
        self._prices: deque[Decimal] = deque(maxlen=self._window)
        self._state: BollingerState | None = None

    @property
    def state(self) -> BollingerState | None:
        """Current Bollinger state, or None if window not yet full."""
        return self._state

    @property
    def suggested_spacing_bps(self) -> Decimal | None:
        """Suggested grid spacing in bps, or None if not yet ready."""
        return self._state.suggested_spacing_bps if self._state else None

    def update(self, mid_price: Decimal) -> BollingerState | None:
        """Add a price observation and recompute bands.

        Returns the new state, or None if the window is not yet full.
        """
        self._prices.append(mid_price)

        if len(self._prices) < self._window:
            self._state = None
            return None

        # SMA
        sma = sum(self._prices) / len(self._prices)

        if sma <= 0:
            self._state = None
            return None

        # Standard deviation
        variance = sum((p - sma) ** 2 for p in self._prices) / len(self._prices)
        std_dev = Decimal(str(math.sqrt(float(variance))))

        # Bands
        band_offset = self._multiplier * std_dev
        upper = sma + band_offset
        lower = sma - band_offset

        # Band width in bps
        band_width_bps = (upper - lower) / sma * 10000

        # Map to spacing
        raw_spacing = band_width_bps * self._spacing_scale
        spacing = max(self._min_spacing_bps, min(self._max_spacing_bps, raw_spacing))

        self._state = BollingerState(
            sma=sma,
            upper=upper,
            lower=lower,
            band_width_bps=band_width_bps,
            std_dev=std_dev,
            suggested_spacing_bps=spacing,
        )
        return self._state

    def reset(self) -> None:
        """Clear all state."""
        self._prices.clear()
        self._state = None
