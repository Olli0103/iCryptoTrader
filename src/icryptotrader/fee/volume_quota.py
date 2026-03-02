"""Volume Quota — prevents fee-tier death spiral from mark-out widening.

When the mark-out tracker detects toxic fills and widens adverse_selection_bps,
the grid engine widens spreads, reducing fill rate.  Lower fill rate reduces
30-day rolling volume.  If volume drops below a Kraken tier threshold, maker
fees increase, forcing even wider spreads — creating an unrecoverable death
spiral where the bot perpetually widens itself out of the market.

The Volume Quota tracks progress toward maintaining the current fee tier.
When volume is at risk of dropping, it overrides the minimum spacing to
take neutral-EV trades that maintain the tier.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from icryptotrader.fee.fee_model import FeeModel

logger = logging.getLogger(__name__)

# How close to the tier boundary (as a fraction) before activating quota.
# E.g., 0.20 = activate when within 20% of dropping to the lower tier.
_TIER_DEFENSE_ZONE_PCT = 0.20

# When in tier-defense mode, allow spacing down to 80% of normal minimum.
# This tolerates losing 20% of edge per trade to generate volume.
_TIER_DEFENSE_SPACING_MULT = Decimal("0.80")


@dataclass
class VolumeQuotaStatus:
    """Current volume quota assessment."""

    tier_at_risk: bool  # True if volume is close to dropping
    current_volume_usd: int
    volume_surplus_usd: int  # How much above the current tier threshold
    tier_threshold_usd: int  # Current tier's min volume
    defense_zone_usd: int  # Volume buffer zone where defense activates
    spacing_override_mult: Decimal  # Multiplier for min spacing (1.0 = no override)
    daily_volume_target_usd: int  # Recommended daily volume to maintain tier


class VolumeQuota:
    """Monitors fee tier stability and overrides spacing when tier is at risk.

    When 30-day volume approaches the current tier's minimum threshold, the
    quota system signals the strategy loop to tighten spreads and tolerate
    slightly lower edge in order to generate the volume needed to maintain
    the tier — breaking the death spiral before it starts.

    Usage:
        quota = VolumeQuota(fee_model=fee_model)
        status = quota.assess()
        if status.tier_at_risk:
            effective_spacing = min_spacing * status.spacing_override_mult
    """

    def __init__(
        self,
        fee_model: FeeModel,
        defense_zone_pct: float = _TIER_DEFENSE_ZONE_PCT,
        defense_spacing_mult: Decimal = _TIER_DEFENSE_SPACING_MULT,
        clock: object | None = None,
    ) -> None:
        self._fee = fee_model
        self._defense_zone_pct = defense_zone_pct
        self._defense_spacing_mult = defense_spacing_mult
        self._clock = clock

        # Track local fill volume for daily pacing
        self._daily_fills_usd: list[tuple[float, Decimal]] = []
        self._last_assessment: VolumeQuotaStatus | None = None

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()  # type: ignore[operator]
        return time.time()

    def record_fill_volume(self, notional_usd: Decimal) -> None:
        """Record a fill's notional value for daily pacing."""
        self._daily_fills_usd.append((self._now(), abs(notional_usd)))
        # Prune entries older than 24h
        cutoff = self._now() - 86400
        self._daily_fills_usd = [
            (t, v) for t, v in self._daily_fills_usd if t >= cutoff
        ]

    def daily_volume_usd(self) -> Decimal:
        """Total fill volume in the last 24 hours."""
        cutoff = self._now() - 86400
        return sum(
            (v for t, v in self._daily_fills_usd if t >= cutoff),
            Decimal("0"),
        )

    def assess(self) -> VolumeQuotaStatus:
        """Assess current tier stability and compute spacing override.

        Returns VolumeQuotaStatus with tier risk assessment and a spacing
        multiplier.  When tier_at_risk is True, the strategy loop should
        multiply its minimum spacing by spacing_override_mult (< 1.0) to
        tighten the grid and generate volume.
        """
        volume = self._fee.volume_30d_usd
        tier = self._fee.current_tier
        tier_threshold = tier.min_volume_usd

        # How much surplus volume we have above the current tier
        surplus = volume - tier_threshold

        # For the bottom tier (threshold=0), there's nothing to defend.
        if tier_threshold == 0:
            status = VolumeQuotaStatus(
                tier_at_risk=False,
                current_volume_usd=volume,
                volume_surplus_usd=surplus,
                tier_threshold_usd=tier_threshold,
                defense_zone_usd=0,
                spacing_override_mult=Decimal("1"),
                daily_volume_target_usd=0,
            )
            self._last_assessment = status
            return status

        # Defense zone in USD
        defense_zone = int(tier_threshold * self._defense_zone_pct)
        tier_at_risk = surplus < defense_zone

        # Daily volume target: spread deficit over 7 days (conservative)
        if tier_at_risk and surplus > 0:
            daily_target = max(0, (defense_zone - surplus)) // 7
        elif tier_at_risk:
            daily_target = defense_zone // 7
        else:
            daily_target = 0

        # Spacing multiplier: only override when tier is at risk
        mult = Decimal("1")
        if tier_at_risk:
            # Scale override by depth in the defense zone.
            # At zone boundary: mult approaches 1.0 (gentle).
            # Deep in zone (surplus near 0): mult = defense_spacing_mult (aggressive).
            if defense_zone > 0:
                depth = Decimal(str(1.0 - max(0, surplus) / defense_zone))
                reduction = (Decimal("1") - self._defense_spacing_mult) * depth
                mult = Decimal("1") - reduction
                mult = max(self._defense_spacing_mult, mult)
            else:
                mult = self._defense_spacing_mult

        status = VolumeQuotaStatus(
            tier_at_risk=tier_at_risk,
            current_volume_usd=volume,
            volume_surplus_usd=surplus,
            tier_threshold_usd=tier_threshold,
            defense_zone_usd=defense_zone,
            spacing_override_mult=mult,
            daily_volume_target_usd=daily_target,
        )
        self._last_assessment = status
        return status

    @property
    def last_assessment(self) -> VolumeQuotaStatus | None:
        return self._last_assessment
