"""Tests for the Inventory Arbiter."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.inventory.inventory_arbiter import (
    AllocationLimits,
    InventoryArbiter,
)
from icryptotrader.types import Regime


class TestBasicState:
    def test_initial_state(self) -> None:
        arb = InventoryArbiter()
        assert arb.btc_balance == Decimal("0")
        assert arb.usd_balance == Decimal("0")
        assert arb.regime == Regime.RANGE_BOUND

    def test_update_balances(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        assert arb.btc_balance == Decimal("0.03")
        assert arb.usd_balance == Decimal("2500")

    def test_portfolio_value(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        arb.update_price(Decimal("85000"))
        # 0.03 * 85000 = 2550 + 2500 = 5050
        assert arb.portfolio_value_usd == Decimal("5050")

    def test_btc_allocation(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        arb.update_price(Decimal("85000"))
        # BTC value = 2550, total = 5050, allocation = 50.5%
        alloc = arb.btc_allocation_pct
        assert 0.50 < alloc < 0.51


class TestSnapshot:
    def test_snapshot_fields(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        arb.update_price(Decimal("85000"))
        snap = arb.snapshot()
        assert snap.btc_balance == Decimal("0.03")
        assert snap.usd_balance == Decimal("2500")
        assert snap.btc_price_usd == Decimal("85000")
        assert snap.regime == Regime.RANGE_BOUND
        assert snap.can_buy is True
        assert snap.can_sell is True

    def test_snapshot_with_no_balance(self) -> None:
        arb = InventoryArbiter()
        arb.update_price(Decimal("85000"))
        snap = arb.snapshot()
        assert snap.btc_allocation_pct == 0.0
        # can_buy is True (allocation permits), but max_buy_btc is 0 (no USD)
        assert snap.max_buy_btc == Decimal("0")
        assert snap.can_sell is False  # Below min allocation


class TestAllocationEnforcement:
    def test_buy_blocked_at_max(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.06"), usd=Decimal("900"))
        arb.update_price(Decimal("85000"))
        # BTC value = 5100, total = 6000, alloc = 85% > max 60%
        allowed = arb.check_buy(Decimal("0.01"))
        assert allowed == Decimal("0")

    def test_sell_blocked_at_min(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.001"), usd=Decimal("9000"))
        arb.update_price(Decimal("85000"))
        # BTC value = 85, total = 9085, alloc = 0.9% < min 40%
        allowed = arb.check_sell(Decimal("0.001"))
        assert allowed == Decimal("0")

    def test_buy_allowed_below_max(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.025"), usd=Decimal("2875"))
        arb.update_price(Decimal("85000"))
        # BTC value = 2125, total = 5000, alloc = 42.5%
        allowed = arb.check_buy(Decimal("0.01"))
        assert allowed > Decimal("0")

    def test_buy_capped_to_max_allocation(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.025"), usd=Decimal("2875"))
        arb.update_price(Decimal("85000"))
        # alloc = 42.5%, max = 60%, headroom = 17.5%
        # But capped to 10% rebalance limit
        allowed = arb.check_buy(Decimal("1.0"))  # Way too much
        assert allowed < Decimal("1.0")
        assert allowed > Decimal("0")


class TestRegimeChange:
    def test_regime_changes_limits(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        arb.update_price(Decimal("85000"))

        arb.set_regime(Regime.TRENDING_UP)
        limits = arb.current_limits()
        assert limits.target_pct == 0.70
        assert limits.max_pct == 0.80

    def test_chaos_regime_zero_allocation(self) -> None:
        arb = InventoryArbiter()
        arb.set_regime(Regime.CHAOS)
        limits = arb.current_limits()
        assert limits.target_pct == 0.0
        assert limits.max_pct == 0.05

    def test_chaos_blocks_most_buys(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        arb.update_price(Decimal("85000"))
        arb.set_regime(Regime.CHAOS)
        # alloc = ~50%, max = 5% in chaos → buy blocked
        allowed = arb.check_buy(Decimal("0.01"))
        assert allowed == Decimal("0")


class TestMaxBuySell:
    def test_max_buy_limited_by_usd(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0"), usd=Decimal("100"))
        arb.update_price(Decimal("85000"))
        snap = arb.snapshot()
        # Max buy limited by $100 available
        assert snap.max_buy_btc <= Decimal("100") / Decimal("85000")

    def test_max_sell_limited_by_btc_balance(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("0.001"), usd=Decimal("0"))
        arb.update_price(Decimal("85000"))
        snap = arb.snapshot()
        assert snap.max_sell_btc <= Decimal("0.001")

    def test_max_sell_limited_by_rebalance_pct(self) -> None:
        arb = InventoryArbiter(max_single_rebalance_pct=0.10)
        arb.update_balances(btc=Decimal("1.0"), usd=Decimal("0"))
        arb.update_price(Decimal("85000"))
        snap = arb.snapshot()
        # Portfolio = $85000, max rebalance = 10% = $8500 = 0.1 BTC
        assert snap.max_sell_btc <= Decimal("0.11")


class TestCustomLimits:
    def test_custom_limits_override(self) -> None:
        custom = {
            Regime.RANGE_BOUND: AllocationLimits(
                target_pct=0.60, max_pct=0.70, min_pct=0.50,
            ),
        }
        arb = InventoryArbiter(limits=custom)
        limits = arb.current_limits()
        assert limits.target_pct == 0.60


class TestEdgeCases:
    def test_zero_price(self) -> None:
        arb = InventoryArbiter()
        arb.update_balances(btc=Decimal("1"), usd=Decimal("1000"))
        arb.update_price(Decimal("0"))
        assert arb.check_buy(Decimal("1")) == Decimal("0")
        assert arb.check_sell(Decimal("1")) == Decimal("0")

    def test_zero_balances(self) -> None:
        arb = InventoryArbiter()
        arb.update_price(Decimal("85000"))
        assert arb.btc_allocation_pct == 0.0
        assert arb.portfolio_value_usd == Decimal("0")
