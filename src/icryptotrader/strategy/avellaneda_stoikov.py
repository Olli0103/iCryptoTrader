"""Avellaneda-Stoikov inspired optimal market making model.

Adapts the core A-S insights for a crypto grid bot:
1. Spread scales with volatility (σ term) — higher vol → wider spread
2. Inventory risk creates asymmetric spacing (reservation price offset) —
   long inventory → wider buy / tighter sell to incentivize mean-reversion
3. OBI provides microstructure signal — bullish book → tighten buys, widen sells
4. Risk aversion (γ) controls the trade-off between fill rate and adverse selection

The skew is proportional to volatility (the key A-S contribution):
when vol is high AND you hold inventory, the skew is much larger than
when vol is low — unlike fixed allocation-based skew.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ASResult:
    """Result of Avellaneda-Stoikov optimal spacing computation."""

    half_spread_bps: Decimal  # Symmetric volatility-proportional spread per side
    inventory_skew_bps: Decimal  # Asymmetric inventory-driven offset
    obi_skew_bps: Decimal  # OBI-driven microstructure adjustment
    buy_spacing_bps: Decimal  # Final buy spacing = half_spread + inv_skew + obi
    sell_spacing_bps: Decimal  # Final sell spacing = half_spread - inv_skew - obi


class AvellanedaStoikov:
    """Computes optimal bid/ask spacing using A-S model principles.

    The spread has three components:

    1. Half-spread: ``max(fee_floor, gamma * volatility_bps)`` per side.
       This captures the A-S insight that optimal spread is proportional to σ.

    2. Inventory skew: ``gamma * volatility_bps * inventory_delta``.
       This is the reservation price offset from A-S, scaled by volatility.
       When long (delta > 0), bid moves down (wider) and ask moves up (tighter),
       incentivizing sells. The key difference from static delta skew: the
       inventory adjustment scales with volatility.

    3. OBI skew: ``obi * obi_sensitivity_bps``.
       Positive OBI (bullish book) → tighter buys, wider sells.

    Usage::

        as_model = AvellanedaStoikov(gamma=Decimal("0.3"))
        result = as_model.compute(
            volatility_bps=Decimal("150"),
            inventory_delta=Decimal("0.1"),
            fee_floor_bps=Decimal("33"),
        )
        # result.buy_spacing_bps, result.sell_spacing_bps
    """

    def __init__(
        self,
        gamma: Decimal = Decimal("0.3"),
        max_spread_bps: Decimal = Decimal("500"),
        max_skew_bps: Decimal = Decimal("50"),
        obi_sensitivity_bps: Decimal = Decimal("10"),
    ) -> None:
        """Initialize A-S model.

        Args:
            gamma: Risk aversion parameter. Higher → wider spreads, larger
                inventory skew. Range [0.01, 2.0]. Typical: 0.1-0.5.
            max_spread_bps: Hard cap on half-spread per side.
            max_skew_bps: Hard cap on inventory + OBI skew combined.
            obi_sensitivity_bps: OBI of ±1.0 maps to this many bps adjustment.
        """
        if gamma <= 0:
            msg = "gamma must be positive"
            raise ValueError(msg)
        self._gamma = gamma
        self._max_spread = max_spread_bps
        self._max_skew = max_skew_bps
        self._obi_sensitivity = obi_sensitivity_bps

    def compute(
        self,
        volatility_bps: Decimal,
        inventory_delta: Decimal,
        fee_floor_bps: Decimal,
        obi: float = 0.0,
        time_decay_mult: float = 1.0,
    ) -> ASResult:
        """Compute optimal bid/ask spacing.

        Args:
            volatility_bps: Recent realized volatility in basis points.
                E.g., 0.5% vol = 50 bps. From ``RegimeRouter.ewma_volatility * 10000``.
            inventory_delta: ``(btc_alloc_pct - target_pct)``, range ~ [-0.5, 0.5].
                Positive = overweight BTC. Negative = underweight.
            fee_floor_bps: Minimum profitable spacing from fee model
                (``GridEngine.optimal_spacing_bps()``).
            obi: Order book imbalance [-1, 1]. Positive = bid pressure (bullish).
            time_decay_mult: Time-decay urgency multiplier from InventoryArbiter.
                >= 1.0. Scales inventory_delta to increase urgency for
                long-held deviations (T-to-Liquidation risk).

        Returns:
            ASResult with buy_spacing_bps and sell_spacing_bps.
        """
        # 1. Symmetric half-spread: proportional to volatility
        raw_half = self._gamma * abs(volatility_bps)
        half_spread = max(fee_floor_bps, min(self._max_spread, raw_half))

        # 2. Inventory skew: proportional to both σ and inventory position.
        #    When long (delta > 0): positive skew → wider buy, tighter sell.
        #    Time-decay: multiply delta by time_decay_mult so that long-held
        #    deviations produce increasingly aggressive mean-reversion skew.
        effective_delta = inventory_delta * Decimal(str(max(1.0, time_decay_mult)))
        raw_inv_skew = self._gamma * abs(volatility_bps) * effective_delta
        inv_skew = max(-self._max_skew, min(self._max_skew, raw_inv_skew))

        # 3. OBI adjustment: positive OBI (bullish) → tighter buy, wider sell
        #    Convention: negative OBI skew = tighter buy offset
        obi_clamped = max(-1.0, min(1.0, obi))
        obi_skew = -Decimal(str(obi_clamped)) * self._obi_sensitivity

        # Combined asymmetric skew (inventory + OBI)
        total_skew = inv_skew + obi_skew
        total_skew = max(-self._max_skew, min(self._max_skew, total_skew))

        buy_spacing = max(Decimal("1"), half_spread + total_skew)
        sell_spacing = max(Decimal("1"), half_spread - total_skew)

        return ASResult(
            half_spread_bps=half_spread,
            inventory_skew_bps=inv_skew,
            obi_skew_bps=obi_skew,
            buy_spacing_bps=buy_spacing,
            sell_spacing_bps=sell_spacing,
        )
