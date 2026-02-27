"""Grid Engine — fixed-level grid state machine for spot mean-reversion.

Computes buy and sell grid levels centered around a reference price (mid-price).
Each level maps to an OrderSlot in the OrderManager. The engine:
  - Computes symmetric grid levels (N buy below mid, N sell above mid)
  - Adjusts spacing based on fee tier (tighter grids at lower fees)
  - Respects regime-based level counts (e.g., 5 in range_bound, 3 in trending)
  - Provides desired levels per slot for the order manager's decide_action()
  - Tracks grid profitability metrics

The grid is recalculated on every strategy tick. The OrderManager handles
the actual amend/add/cancel decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from icryptotrader.order.order_manager import DesiredLevel
from icryptotrader.types import Side

if TYPE_CHECKING:
    from icryptotrader.fee.fee_model import FeeModel

logger = logging.getLogger(__name__)

# Minimum BTC order size on Kraken
MIN_ORDER_BTC = Decimal("0.0001")


@dataclass
class GridLevel:
    """A single level in the grid."""

    index: int
    side: Side
    price: Decimal = Decimal("0")
    qty: Decimal = Decimal("0")
    active: bool = True


@dataclass
class GridState:
    """Snapshot of the current grid configuration."""

    mid_price: Decimal = Decimal("0")
    spacing_bps: Decimal = Decimal("0")
    buy_levels: list[GridLevel] = field(default_factory=list)
    sell_levels: list[GridLevel] = field(default_factory=list)
    total_levels: int = 0


class GridEngine:
    """Computes grid levels and maps them to order manager slots.

    Usage:
        engine = GridEngine(fee_model=fee_model, order_size_usd=Decimal("500"))
        state = engine.compute_grid(
            mid_price=Decimal("85000"),
            num_buy_levels=5,
            num_sell_levels=5,
        )
        desired = engine.desired_for_slot(slot_index=0)
    """

    def __init__(
        self,
        fee_model: FeeModel,
        order_size_usd: Decimal = Decimal("500"),
        min_spacing_bps: Decimal = Decimal("20"),
        adverse_selection_bps: Decimal = Decimal("10"),
        min_edge_bps: Decimal = Decimal("5"),
    ) -> None:
        self._fee_model = fee_model
        self._order_size_usd = order_size_usd
        self._min_spacing_bps = min_spacing_bps
        self._adverse_selection_bps = adverse_selection_bps
        self._min_edge_bps = min_edge_bps

        self._state: GridState = GridState()

        # Metrics
        self.ticks: int = 0
        self.round_trips: int = 0
        self.total_profit_usd: Decimal = Decimal("0")

    @property
    def state(self) -> GridState:
        return self._state

    @property
    def spacing_bps(self) -> Decimal:
        return self._state.spacing_bps

    def optimal_spacing_bps(self) -> Decimal:
        """Compute optimal grid spacing based on current fee tier.

        Uses fee_model.min_profitable_spacing_bps() and ensures
        spacing is at least min_spacing_bps.
        """
        fee_based = self._fee_model.min_profitable_spacing_bps(
            adverse_selection_bps=self._adverse_selection_bps,
            min_edge_bps=self._min_edge_bps,
        )
        return max(fee_based, self._min_spacing_bps)

    def compute_grid(
        self,
        mid_price: Decimal,
        num_buy_levels: int = 5,
        num_sell_levels: int = 5,
        spacing_bps: Decimal | None = None,
        buy_spacing_bps: Decimal | None = None,
        sell_spacing_bps: Decimal | None = None,
        buy_qty_scale: Decimal = Decimal("1"),
        sell_qty_scale: Decimal = Decimal("1"),
    ) -> GridState:
        """Recompute grid levels around mid_price.

        Args:
            mid_price: Current mid-price (or reference price).
            num_buy_levels: Number of buy levels below mid.
            num_sell_levels: Number of sell levels above mid.
            spacing_bps: Base spacing (None = auto from fee model).
                Used for both sides unless overridden by buy/sell_spacing_bps.
            buy_spacing_bps: Override spacing for buy levels (from delta skew).
            sell_spacing_bps: Override spacing for sell levels (from delta skew).
            buy_qty_scale: Multiplier for buy quantities (for asymmetric grids).
            sell_qty_scale: Multiplier for sell quantities.

        Returns:
            GridState with computed levels.
        """
        self.ticks += 1
        base_spacing = spacing_bps if spacing_bps is not None else self.optimal_spacing_bps()
        buy_spacing = buy_spacing_bps if buy_spacing_bps is not None else base_spacing
        sell_spacing = sell_spacing_bps if sell_spacing_bps is not None else base_spacing
        buy_factor = buy_spacing / Decimal("10000")
        sell_factor = sell_spacing / Decimal("10000")

        buy_levels: list[GridLevel] = []
        sell_levels: list[GridLevel] = []

        for i in range(num_buy_levels):
            offset = (i + 1) * buy_factor
            price = (mid_price * (1 - offset)).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP,
            )
            qty = self._qty_for_price(price, buy_qty_scale)
            if qty >= MIN_ORDER_BTC:
                buy_levels.append(GridLevel(
                    index=i, side=Side.BUY, price=price, qty=qty,
                ))

        for i in range(num_sell_levels):
            offset = (i + 1) * sell_factor
            price = (mid_price * (1 + offset)).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP,
            )
            qty = self._qty_for_price(price, sell_qty_scale)
            if qty >= MIN_ORDER_BTC:
                sell_levels.append(GridLevel(
                    index=i, side=Side.SELL, price=price, qty=qty,
                ))

        self._state = GridState(
            mid_price=mid_price,
            spacing_bps=base_spacing,
            buy_levels=buy_levels,
            sell_levels=sell_levels,
            total_levels=len(buy_levels) + len(sell_levels),
        )
        return self._state

    def desired_levels(self) -> list[DesiredLevel | None]:
        """Map grid levels to a flat list of DesiredLevel for order slots.

        Convention: slots 0..N-1 are buy levels, N..2N-1 are sell levels.
        Returns None for unused slots.
        """
        result: list[DesiredLevel | None] = []
        for level in self._state.buy_levels:
            if level.active:
                result.append(DesiredLevel(
                    price=level.price, qty=level.qty, side=Side.BUY,
                ))
            else:
                result.append(None)
        for level in self._state.sell_levels:
            if level.active:
                result.append(DesiredLevel(
                    price=level.price, qty=level.qty, side=Side.SELL,
                ))
            else:
                result.append(None)
        return result

    def deactivate_sell_levels(self, keep: int = 0) -> None:
        """Deactivate sell levels (for tax-lock buy-only mode).

        Args:
            keep: Number of sell levels to keep active (0 = all deactivated).
        """
        for i, level in enumerate(self._state.sell_levels):
            level.active = i < keep

    def expected_net_edge_bps(self) -> Decimal:
        """Net edge per round-trip at current spacing."""
        return self._fee_model.expected_net_edge_bps(
            grid_spacing_bps=self._state.spacing_bps,
            adverse_selection_bps=self._adverse_selection_bps,
        )

    def record_round_trip(self, profit_usd: Decimal) -> None:
        """Record a completed round-trip for metrics."""
        self.round_trips += 1
        self.total_profit_usd += profit_usd

    def _qty_for_price(self, price: Decimal, scale: Decimal = Decimal("1")) -> Decimal:
        """Compute BTC quantity for a level at the given price."""
        if price <= 0:
            return Decimal("0")
        qty = (self._order_size_usd * scale / price).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP,
        )
        return qty
