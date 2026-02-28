"""Inventory Arbiter — global inventory management across Grid + Signal engines.

The Grid Engine and (future) Signal Engine share a single BTC/USD balance.
The Inventory Arbiter:
  - Tracks total BTC and USD balances
  - Enforces allocation limits per regime
  - NETs conflicting desires (grid wants to buy, signal wants to sell)
  - Reports allocation metrics for risk management and delta skew
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from icryptotrader.types import Regime

logger = logging.getLogger(__name__)


@dataclass
class AllocationLimits:
    """Allocation limits for a given regime."""

    target_pct: float
    max_pct: float
    min_pct: float


# Default allocation limits per regime (from config/plan)
DEFAULT_LIMITS: dict[Regime, AllocationLimits] = {
    Regime.RANGE_BOUND: AllocationLimits(target_pct=0.50, max_pct=0.60, min_pct=0.40),
    Regime.TRENDING_UP: AllocationLimits(target_pct=0.70, max_pct=0.80, min_pct=0.55),
    Regime.TRENDING_DOWN: AllocationLimits(target_pct=0.30, max_pct=0.40, min_pct=0.15),
    Regime.CHAOS: AllocationLimits(target_pct=0.00, max_pct=0.05, min_pct=0.00),
}


@dataclass
class InventorySnapshot:
    """Current inventory state."""

    btc_balance: Decimal
    usd_balance: Decimal
    btc_price_usd: Decimal
    btc_value_usd: Decimal
    portfolio_value_usd: Decimal
    btc_allocation_pct: float
    regime: Regime
    limits: AllocationLimits
    can_buy: bool
    can_sell: bool
    max_buy_btc: Decimal
    max_sell_btc: Decimal


class InventoryArbiter:
    """Manages global BTC/USD inventory and allocation enforcement.

    Usage:
        arbiter = InventoryArbiter()
        arbiter.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        arbiter.update_price(Decimal("85000"))
        snap = arbiter.snapshot()
    """

    def __init__(
        self,
        limits: dict[Regime, AllocationLimits] | None = None,
        max_single_rebalance_pct: float = 0.10,
        max_rebalance_pct_per_min: float = 0.01,
    ) -> None:
        self._limits = limits or dict(DEFAULT_LIMITS)
        self._max_rebalance_pct = max_single_rebalance_pct

        # TWAP rate-limiting: track USD rebalanced per minute window
        self._max_rebalance_pct_per_min = max_rebalance_pct_per_min
        self._rebalance_window_sec = 60.0
        self._rebalance_history: list[tuple[float, Decimal]] = []

        self._btc_balance = Decimal("0")
        self._usd_balance = Decimal("0")
        self._btc_price = Decimal("0")
        self._regime = Regime.RANGE_BOUND

        # BTC reserved by pending orders (not yet filled)
        self._btc_reserved_buy = Decimal("0")  # USD committed to pending buys
        self._btc_reserved_sell = Decimal("0")  # BTC committed to pending sells

    @property
    def btc_balance(self) -> Decimal:
        return self._btc_balance

    @property
    def usd_balance(self) -> Decimal:
        return self._usd_balance

    @property
    def btc_price(self) -> Decimal:
        return self._btc_price

    @property
    def regime(self) -> Regime:
        return self._regime

    @property
    def portfolio_value_usd(self) -> Decimal:
        return self._btc_balance * self._btc_price + self._usd_balance

    @property
    def btc_allocation_pct(self) -> float:
        total = self.portfolio_value_usd
        if total <= 0:
            return 0.0
        return float((self._btc_balance * self._btc_price) / total)

    def update_balances(self, btc: Decimal, usd: Decimal) -> None:
        """Update balances from exchange account data."""
        self._btc_balance = btc
        self._usd_balance = usd

    def update_price(self, btc_price_usd: Decimal) -> None:
        """Update BTC price from market data."""
        self._btc_price = btc_price_usd

    def set_regime(self, regime: Regime) -> None:
        """Update current regime (from regime router or risk manager)."""
        if regime != self._regime:
            logger.info("Inventory: regime change %s → %s", self._regime.value, regime.value)
            self._regime = regime

    def current_limits(self) -> AllocationLimits:
        """Get allocation limits for current regime."""
        return self._limits.get(self._regime, DEFAULT_LIMITS[Regime.RANGE_BOUND])

    def snapshot(self) -> InventorySnapshot:
        """Compute full inventory snapshot for decision-making."""
        btc_value = self._btc_balance * self._btc_price
        total = btc_value + self._usd_balance
        alloc = float(btc_value / total) if total > 0 else 0.0
        limits = self.current_limits()

        can_buy = alloc < limits.max_pct
        can_sell = alloc > limits.min_pct

        max_buy_btc = self._max_buy_btc(alloc, limits, total)
        max_sell_btc = self._max_sell_btc(alloc, limits, total)

        return InventorySnapshot(
            btc_balance=self._btc_balance,
            usd_balance=self._usd_balance,
            btc_price_usd=self._btc_price,
            btc_value_usd=btc_value,
            portfolio_value_usd=total,
            btc_allocation_pct=alloc,
            regime=self._regime,
            limits=limits,
            can_buy=can_buy,
            can_sell=can_sell,
            max_buy_btc=max_buy_btc,
            max_sell_btc=max_sell_btc,
        )

    def check_buy(self, qty_btc: Decimal) -> Decimal:
        """Check how much of a buy order is allowed. Returns clamped quantity."""
        if self._btc_price <= 0:
            return Decimal("0")

        limits = self.current_limits()
        alloc = self.btc_allocation_pct

        if alloc >= limits.max_pct:
            return Decimal("0")

        max_allowed = self._max_buy_btc(alloc, limits, self.portfolio_value_usd)
        return min(qty_btc, max_allowed)

    def check_sell(self, qty_btc: Decimal) -> Decimal:
        """Check how much of a sell order is allowed. Returns clamped quantity."""
        if self._btc_price <= 0:
            return Decimal("0")

        limits = self.current_limits()
        alloc = self.btc_allocation_pct

        if alloc <= limits.min_pct:
            return Decimal("0")

        max_allowed = self._max_sell_btc(alloc, limits, self.portfolio_value_usd)
        return min(qty_btc, max_allowed, self._btc_balance)

    def _twap_remaining_usd(self, total_usd: Decimal) -> Decimal:
        """USD budget remaining in the current TWAP window (1 minute).

        Prevents sweeping the book: max rebalance_pct_per_min of portfolio
        can be rebalanced in any rolling 60-second window.
        """
        now = time.monotonic()
        cutoff = now - self._rebalance_window_sec
        self._rebalance_history = [
            (t, amt) for t, amt in self._rebalance_history if t >= cutoff
        ]
        used = sum((amt for _, amt in self._rebalance_history), Decimal("0"))
        budget = total_usd * Decimal(str(self._max_rebalance_pct_per_min))
        return max(Decimal("0"), budget - used)

    def record_rebalance(self, usd_amount: Decimal) -> None:
        """Record a rebalance for TWAP tracking. Call after order placement."""
        self._rebalance_history.append((time.monotonic(), abs(usd_amount)))

    def _max_buy_btc(
        self, alloc: float, limits: AllocationLimits, total_usd: Decimal,
    ) -> Decimal:
        """Max BTC that can be bought before hitting max allocation."""
        if self._btc_price <= 0 or total_usd <= 0:
            return Decimal("0")

        headroom_pct = limits.max_pct - alloc
        if headroom_pct <= 0:
            return Decimal("0")

        # Cap to single-tick rebalance limit
        effective_pct = Decimal(str(min(headroom_pct, self._max_rebalance_pct)))
        max_usd = total_usd * effective_pct

        # TWAP rate-limit: cap to remaining budget in rolling window
        twap_budget = self._twap_remaining_usd(total_usd)
        max_usd = min(max_usd, twap_budget)

        # Also cap to available USD
        max_usd = min(max_usd, self._usd_balance)

        return max_usd / self._btc_price

    def _max_sell_btc(
        self, alloc: float, limits: AllocationLimits, total_usd: Decimal,
    ) -> Decimal:
        """Max BTC that can be sold before hitting min allocation."""
        if self._btc_price <= 0 or total_usd <= 0:
            return Decimal("0")

        excess_pct = alloc - limits.min_pct
        if excess_pct <= 0:
            return Decimal("0")

        effective_pct = Decimal(str(min(excess_pct, self._max_rebalance_pct)))
        max_usd = total_usd * effective_pct

        # TWAP rate-limit
        twap_budget = self._twap_remaining_usd(total_usd)
        max_usd = min(max_usd, twap_budget)

        return min(max_usd / self._btc_price, self._btc_balance)
