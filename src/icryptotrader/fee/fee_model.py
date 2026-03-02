"""Fee model service — tier-aware fee calculations for Kraken spot trading.

Central place for all fee-related decisions. Every component that needs to know
"is this trade worth it?" calls expected_net_edge_bps() before placing an order.
"""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.types import FeeTier

# Kraken Spot fee schedule for crypto pairs (as of 2025).
# https://www.kraken.com/features/fee-schedule
KRAKEN_SPOT_TIERS: list[FeeTier] = [
    FeeTier(min_volume_usd=0, maker_bps=Decimal("25"), taker_bps=Decimal("40")),
    FeeTier(min_volume_usd=10_000, maker_bps=Decimal("20"), taker_bps=Decimal("35")),
    FeeTier(min_volume_usd=50_000, maker_bps=Decimal("14"), taker_bps=Decimal("24")),
    FeeTier(min_volume_usd=100_000, maker_bps=Decimal("12"), taker_bps=Decimal("20")),
    FeeTier(min_volume_usd=250_000, maker_bps=Decimal("8"), taker_bps=Decimal("18")),
    FeeTier(min_volume_usd=500_000, maker_bps=Decimal("6"), taker_bps=Decimal("16")),
    FeeTier(min_volume_usd=1_000_000, maker_bps=Decimal("4"), taker_bps=Decimal("14")),
    FeeTier(min_volume_usd=5_000_000, maker_bps=Decimal("2"), taker_bps=Decimal("12")),
    FeeTier(min_volume_usd=10_000_000, maker_bps=Decimal("0"), taker_bps=Decimal("10")),
]


class FeeModel:
    """Tier-aware fee calculator for Kraken spot trading.

    The fee tier is determined by 30-day rolling trade volume across all crypto
    pairs (stablecoin/FX pairs excluded). The tier can be set from the Kraken
    account info REST endpoint or manually overridden.
    """

    def __init__(
        self,
        tiers: list[FeeTier] | None = None,
        volume_30d_usd: int = 0,
    ) -> None:
        self._tiers = tiers or KRAKEN_SPOT_TIERS
        self._volume_30d_usd = volume_30d_usd
        self._current_tier = self._resolve_tier(volume_30d_usd)

    @property
    def current_tier(self) -> FeeTier:
        return self._current_tier

    @property
    def volume_30d_usd(self) -> int:
        return self._volume_30d_usd

    def update_volume(self, volume_30d_usd: int) -> None:
        """Update 30-day volume (from Kraken TradeVolume endpoint or local tracking)."""
        self._volume_30d_usd = volume_30d_usd
        self._current_tier = self._resolve_tier(volume_30d_usd)

    def maker_fee_bps(self) -> Decimal:
        # Clamp to zero: some exchanges offer negative maker rebates,
        # but our fee math assumes non-negative fees throughout.
        return max(Decimal("0"), self._current_tier.maker_bps)

    def taker_fee_bps(self) -> Decimal:
        return max(Decimal("0"), self._current_tier.taker_bps)

    def rt_cost_bps(self, maker_both_sides: bool = True) -> Decimal:
        """Round-trip cost in bps. Default assumes maker on both buy and sell.

        Returns 0 for the top Kraken tier (0% maker fee). Callers that
        use this as a divisor MUST guard against zero.
        """
        maker = max(Decimal("0"), self._current_tier.maker_bps)
        if maker_both_sides:
            return maker * 2
        return maker + max(Decimal("0"), self._current_tier.taker_bps)

    def expected_net_edge_bps(
        self,
        grid_spacing_bps: Decimal,
        adverse_selection_bps: Decimal = Decimal("10"),
        maker_both_sides: bool = True,
    ) -> Decimal:
        """Net edge per round-trip after fees and adverse selection.

        This is THE gate function. If this returns <= 0, the trade is not worth it.

        Args:
            grid_spacing_bps: Distance between buy and sell level in bps.
            adverse_selection_bps: Expected adverse selection cost per RT.
            maker_both_sides: Whether both legs are maker (post_only).

        Returns:
            Net edge in bps. Positive = profitable, negative = losing.
        """
        return grid_spacing_bps - self.rt_cost_bps(maker_both_sides) - adverse_selection_bps

    def min_profitable_spacing_bps(
        self,
        adverse_selection_bps: Decimal = Decimal("10"),
        min_edge_bps: Decimal = Decimal("5"),
        maker_both_sides: bool = True,
    ) -> Decimal:
        """Minimum grid spacing that yields at least min_edge_bps net profit.

        Used by the Quant Agent / Grid Engine to auto-calibrate grid spacing.
        Always returns a positive value even at the zero-fee top tier.
        """
        result = self.rt_cost_bps(maker_both_sides) + adverse_selection_bps + min_edge_bps
        # Ensure positive: at the zero-fee tier, rt_cost=0, but
        # adverse_selection + min_edge should keep this positive.
        return max(Decimal("1"), result)

    def fee_for_notional(self, notional_usd: Decimal, is_maker: bool = True) -> Decimal:
        """Absolute fee in USD for a given notional trade size.

        Returns 0 for zero-fee tiers (top Kraken tier has 0% maker fee).
        """
        rate = max(
            Decimal("0"),
            self._current_tier.maker_bps if is_maker else self._current_tier.taker_bps,
        )
        return notional_usd * rate / Decimal("10000")

    def would_cross_spread(
        self,
        order_price: Decimal,
        side: str,
        best_bid: Decimal,
        best_ask: Decimal,
    ) -> bool:
        """Check if a limit order would cross the spread (become a taker).

        If post_only is set and this returns True, the order will be rejected.
        The caller should widen the price or skip the order.
        """
        if side == "buy":
            return order_price >= best_ask
        return order_price <= best_bid

    def taker_penalty_bps(self) -> Decimal:
        """Extra cost in bps if an order accidentally becomes a taker.

        taker_fee - maker_fee swing = the full cost difference. When maker
        fee is 0 (top tier), the full taker fee is the penalty.
        """
        return max(Decimal("0"), self._current_tier.taker_bps - self._current_tier.maker_bps)

    def volume_to_next_tier(self) -> int | None:
        """USD volume needed to reach the next fee tier, or None if at max."""
        for tier in self._tiers:
            if tier.min_volume_usd > self._volume_30d_usd:
                return tier.min_volume_usd - self._volume_30d_usd
        return None

    def next_tier(self) -> FeeTier | None:
        """The next fee tier above current, or None if at max."""
        for tier in self._tiers:
            if tier.min_volume_usd > self._volume_30d_usd:
                return tier
        return None

    def _resolve_tier(self, volume_usd: int) -> FeeTier:
        """Find the highest tier for which volume meets the minimum threshold."""
        resolved = self._tiers[0]
        for tier in self._tiers:
            if volume_usd >= tier.min_volume_usd:
                resolved = tier
        return resolved
