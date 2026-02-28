"""Delta Skew — target allocation deviation + OBI → quote asymmetry.

Replaces the futures-bot's funding rate skew component. Instead of adjusting
quotes based on funding rates, we adjust based on deviation from the target
BTC allocation percentage and order book imbalance (OBI).

When BTC allocation is above target: skew quotes to sell more (widen buys, tighten sells)
When BTC allocation is below target: skew quotes to buy more (tighten buys, widen sells)

OBI provides microstructure signal: positive OBI (more bids) → tighten buys, widen sells.

The skew is expressed in basis points and applied as an offset to grid levels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)

# Maximum skew in basis points (prevent runaway asymmetry)
MAX_SKEW_BPS = Decimal("30")
# Default OBI sensitivity: OBI of 1.0 → 15 bps adjustment
DEFAULT_OBI_SENSITIVITY_BPS = Decimal("15")


@dataclass
class SkewResult:
    """Result of computing delta skew."""

    buy_offset_bps: Decimal  # Positive = widen buys (further from mid)
    sell_offset_bps: Decimal  # Positive = widen sells (further from mid)
    raw_skew_bps: Decimal  # Pre-clamp skew value (allocation component only)
    deviation_pct: float  # How far from target (signed)
    obi_adjustment_bps: Decimal  # OBI contribution to skew


class DeltaSkew:
    """Computes quote asymmetry from BTC allocation deviation and OBI.

    Usage:
        skew = DeltaSkew(sensitivity=Decimal("2.0"))
        result = skew.compute(
            btc_alloc_pct=0.60,
            target_pct=0.50,
            obi=0.3,
        )
        # result.buy_offset_bps = positive (widen buys, less aggressive buying)
        # result.sell_offset_bps = negative (tighten sells, more aggressive selling)
    """

    def __init__(
        self,
        sensitivity: Decimal = Decimal("2.0"),
        max_skew_bps: Decimal = MAX_SKEW_BPS,
        obi_sensitivity_bps: Decimal = DEFAULT_OBI_SENSITIVITY_BPS,
    ) -> None:
        self._sensitivity = sensitivity
        self._max_skew_bps = max_skew_bps
        self._obi_sensitivity_bps = obi_sensitivity_bps

    def compute(
        self,
        btc_alloc_pct: float,
        target_pct: float,
        obi: float = 0.0,
    ) -> SkewResult:
        """Compute buy/sell skew offsets based on allocation deviation and OBI.

        Uses convex (quadratic) scaling: skew = sign(dev) * (dev*100)^2 * 0.5
        This acts like a dial (gradual) instead of a light switch (binary).
        Small deviations produce minimal skew; large deviations ramp up fast.

        OBI (order book imbalance) provides a microstructure adjustment:
        - Positive OBI (more bid pressure, bullish) → tighten buys, widen sells
        - Negative OBI (more ask pressure, bearish) → widen buys, tighten sells
        The OBI component is additive to the allocation-based skew.

        Args:
            btc_alloc_pct: Current BTC allocation as fraction (0.0 to 1.0).
            target_pct: Target BTC allocation as fraction.
            obi: Order book imbalance, range [-1, 1]. Positive = buy pressure.

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

        # Convex quadratic scaling: gradual for small deviations, aggressive for large
        # E.g., 5% deviation → sign(0.05) * (5)^2 * 0.5 = 12.5 bps
        #        10% deviation → sign(0.10) * (10)^2 * 0.5 = 50 bps (clamped to 30)
        #        2% deviation → sign(0.02) * (2)^2 * 0.5 = 2 bps
        dev_bps = deviation * 100  # Convert to percentage points
        sign = Decimal("1") if deviation >= 0 else Decimal("-1")
        raw_skew_bps = sign * Decimal(str(dev_bps * dev_bps)) * Decimal("0.5") * self._sensitivity

        # OBI adjustment: positive OBI (bullish) → negative contribution (tighter buy)
        # This matches the convention: negative buy_offset = tighter buy spacing
        obi_clamped = max(-1.0, min(1.0, obi))
        obi_adjust = -Decimal(str(obi_clamped)) * self._obi_sensitivity_bps

        # Combine allocation skew + OBI adjustment, then clamp
        combined = raw_skew_bps + obi_adjust
        clamped = max(-self._max_skew_bps, min(self._max_skew_bps, combined))

        return SkewResult(
            buy_offset_bps=clamped,
            sell_offset_bps=-clamped,
            raw_skew_bps=raw_skew_bps,
            deviation_pct=deviation,
            obi_adjustment_bps=obi_adjust,
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
