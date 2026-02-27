"""Tests for the grid engine."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.fee.fee_model import FeeModel
from icryptotrader.strategy.grid_engine import GridEngine
from icryptotrader.types import Side


class TestOptimalSpacing:
    def test_uses_fee_model_min_profitable(self) -> None:
        fm = FeeModel(volume_30d_usd=0)  # Base tier: 25 bps maker
        engine = GridEngine(fee_model=fm)
        # min_profitable = rt_cost(50) + adverse(10) + min_edge(5) = 65 bps
        assert engine.optimal_spacing_bps() == Decimal("65")

    def test_respects_min_spacing(self) -> None:
        fm = FeeModel(volume_30d_usd=10_000_000)  # 0 bps maker
        engine = GridEngine(fee_model=fm, min_spacing_bps=Decimal("25"))
        # fee-based = 0 + 10 + 5 = 15, but min is 25
        assert engine.optimal_spacing_bps() == Decimal("25")

    def test_lower_fees_produce_tighter_spacing(self) -> None:
        base = GridEngine(fee_model=FeeModel(volume_30d_usd=0))
        pro = GridEngine(fee_model=FeeModel(volume_30d_usd=1_000_000))
        assert base.optimal_spacing_bps() > pro.optimal_spacing_bps()


class TestComputeGrid:
    def test_symmetric_grid(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        state = engine.compute_grid(
            mid_price=Decimal("85000"),
            num_buy_levels=3,
            num_sell_levels=3,
        )
        assert len(state.buy_levels) == 3
        assert len(state.sell_levels) == 3
        assert state.total_levels == 6

    def test_buy_levels_below_mid(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        state = engine.compute_grid(mid_price=Decimal("85000"), num_buy_levels=3, num_sell_levels=0)
        for level in state.buy_levels:
            assert level.price < Decimal("85000")
            assert level.side == Side.BUY

    def test_sell_levels_above_mid(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        state = engine.compute_grid(mid_price=Decimal("85000"), num_buy_levels=0, num_sell_levels=3)
        for level in state.sell_levels:
            assert level.price > Decimal("85000")
            assert level.side == Side.SELL

    def test_levels_sorted_by_distance(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        state = engine.compute_grid(mid_price=Decimal("85000"), num_buy_levels=5, num_sell_levels=5)
        buy_prices = [lv.price for lv in state.buy_levels]
        sell_prices = [lv.price for lv in state.sell_levels]
        # Buy levels should decrease (further from mid)
        assert buy_prices == sorted(buy_prices, reverse=True)
        # Sell levels should increase
        assert sell_prices == sorted(sell_prices)

    def test_custom_spacing(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        state = engine.compute_grid(
            mid_price=Decimal("100000"),
            num_buy_levels=1,
            num_sell_levels=1,
            spacing_bps=Decimal("100"),  # 1%
        )
        assert state.spacing_bps == Decimal("100")
        # 1% of 100k = 1000
        assert state.buy_levels[0].price == Decimal("99000.0")
        assert state.sell_levels[0].price == Decimal("101000.0")

    def test_qty_based_on_order_size(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm, order_size_usd=Decimal("1000"))
        state = engine.compute_grid(
            mid_price=Decimal("100000"),
            num_buy_levels=1,
            num_sell_levels=0,
            spacing_bps=Decimal("100"),
        )
        # Buy at 99000, qty = 1000/99000 ≈ 0.01010101
        assert state.buy_levels[0].qty > Decimal("0.01")
        assert state.buy_levels[0].qty < Decimal("0.011")

    def test_zero_buy_levels(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        state = engine.compute_grid(mid_price=Decimal("85000"), num_buy_levels=0, num_sell_levels=3)
        assert len(state.buy_levels) == 0
        assert len(state.sell_levels) == 3

    def test_tick_counter_increments(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        engine.compute_grid(mid_price=Decimal("85000"))
        engine.compute_grid(mid_price=Decimal("85100"))
        assert engine.ticks == 2


class TestDesiredLevels:
    def test_maps_to_desired_level(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        engine.compute_grid(mid_price=Decimal("85000"), num_buy_levels=2, num_sell_levels=2)
        desired = engine.desired_levels()
        assert len(desired) == 4
        # First 2 are buys, last 2 are sells
        assert desired[0] is not None
        assert desired[0].side == Side.BUY
        assert desired[2] is not None
        assert desired[2].side == Side.SELL

    def test_deactivated_levels_return_none(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        engine.compute_grid(mid_price=Decimal("85000"), num_buy_levels=2, num_sell_levels=2)
        engine.deactivate_sell_levels(keep=0)
        desired = engine.desired_levels()
        # Sells should be None
        assert desired[2] is None
        assert desired[3] is None
        # Buys should still be active
        assert desired[0] is not None
        assert desired[1] is not None

    def test_partial_sell_deactivation(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        engine.compute_grid(mid_price=Decimal("85000"), num_buy_levels=2, num_sell_levels=3)
        engine.deactivate_sell_levels(keep=1)
        desired = engine.desired_levels()
        # First sell kept, rest deactivated
        assert desired[2] is not None  # sell level 0 kept
        assert desired[3] is None
        assert desired[4] is None


class TestMetrics:
    def test_record_round_trip(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        engine.record_round_trip(Decimal("1.50"))
        engine.record_round_trip(Decimal("2.00"))
        assert engine.round_trips == 2
        assert engine.total_profit_usd == Decimal("3.50")

    def test_expected_net_edge(self) -> None:
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm)
        engine.compute_grid(mid_price=Decimal("85000"))
        edge = engine.expected_net_edge_bps()
        assert edge > 0  # Grid spacing should be profitable


class TestMinOrderSize:
    def test_tiny_price_filtered(self) -> None:
        """At extremely high prices, small USD orders produce sub-minimum qty."""
        fm = FeeModel(volume_30d_usd=0)
        engine = GridEngine(fee_model=fm, order_size_usd=Decimal("1"))  # Very small
        state = engine.compute_grid(
            mid_price=Decimal("1000000"),  # $1M per BTC
            num_buy_levels=1,
            num_sell_levels=0,
            spacing_bps=Decimal("100"),
        )
        # $1 / ~$990000 = 0.00000101 < MIN_ORDER_BTC
        assert len(state.buy_levels) == 0
