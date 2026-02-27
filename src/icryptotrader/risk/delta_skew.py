"""Delta Skew — target allocation deviation → quote asymmetry.

Replaces the futures-bot's funding rate skew component. Instead of adjusting
quotes based on funding rates, we adjust based on deviation from the target
BTC allocation percentage.

When BTC allocation is above target: skew quotes to sell more (widen buys, tighten sells)
When BTC allocation is below target: skew quotes to buy more (tighten buys, widen sells)

The skew is expressed in basis points and applied as an offset to grid levels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)

# Maximum skew in basis points (prevent runaway asymmetry)
MAX_SKEW_BPS = Decimal("30")


@dataclass
class SkewResult:
    """Result of computing delta skew."""

    buy_offset_bps: Decimal  # Positive = widen buys (further from mid)
    sell_offset_bps: Decimal  # Positive = widen sells (further from mid)
    raw_skew_bps: Decimal  # Pre-clamp skew value
    deviation_pct: float  # How far from target (signed)


class DeltaSkew:
    """Computes quote asymmetry from BTC allocation deviation.

    Usage:
        skew = DeltaSkew(sensitivity=Decimal("2.0"))
        result = skew.compute(
            btc_alloc_pct=0.60,
            target_pct=0.50,
        )
        # result.buy_offset_bps = positive (widen buys, less aggressive buying)
        # result.sell_offset_bps = negative (tighten sells, more aggressive selling)
    """

    def __init__(
        self,
        sensitivity: Decimal = Decimal("2.0"),
        max_skew_bps: Decimal = MAX_SKEW_BPS,
    ) -> None:
        self._sensitivity = sensitivity
        self._max_skew_bps = max_skew_bps

    def compute(
        self,
        btc_alloc_pct: float,
        target_pct: float,
    ) -> SkewResult:
        """Compute buy/sell skew offsets based on allocation deviation.

        Args:
            btc_alloc_pct: Current BTC allocation as fraction (0.0 to 1.0).
            target_pct: Target BTC allocation as fraction.

        Returns:
            SkewResult with buy/sell offsets in basis points.

        When over-allocated (btc > target):
            - raw_skew_bps is positive
            - buy_offset_bps is positive (widen buys = less buying)
            - sell_offset_bps is negative (tighten sells = more selling)

        When under-allocated (btc < target):
            - raw_skew_bps is negative
            - buy_offset_bps is negative (tighten buys = more buying)
            - sell_offset_bps is positive (widen sells = less selling)
        """
        deviation = btc_alloc_pct - target_pct

        # Convert deviation to bps using sensitivity multiplier
        # deviation of 0.10 (10%) with sensitivity 2.0 = 20 bps skew
        raw_skew_bps = Decimal(str(deviation)) * Decimal("100") * self._sensitivity

        # Clamp to max
        clamped = max(-self._max_skew_bps, min(self._max_skew_bps, raw_skew_bps))

        return SkewResult(
            buy_offset_bps=clamped,
            sell_offset_bps=-clamped,
            raw_skew_bps=raw_skew_bps,
            deviation_pct=deviation,
        )

    def apply_to_spacing(
        self,
        base_spacing_bps: Decimal,
        skew: SkewResult,
    ) -> tuple[Decimal, Decimal]:
        """Apply skew to base grid spacing, returning (buy_spacing, sell_spacing).

        Both spacings are guaranteed to be >= 1 bps (never zero or negative).
        """
        buy_spacing = max(Decimal("1"), base_spacing_bps + skew.buy_offset_bps)
        sell_spacing = max(Decimal("1"), base_spacing_bps + skew.sell_offset_bps)
        return buy_spacing, sell_spacing
