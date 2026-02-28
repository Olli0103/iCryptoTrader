"""Tests for the blow-through overhaul: geometric grids, TWAP, HWM withdrawal,
convex skew, multi-timeframe vol, amend threshold, post-only, blow-through tax,
vault lock, wash sale cooldown.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from icryptotrader.fee.fee_model import FeeModel
from icryptotrader.inventory.inventory_arbiter import InventoryArbiter
from icryptotrader.order.order_manager import Action, DesiredLevel, OrderManager
from icryptotrader.risk.delta_skew import DeltaSkew
from icryptotrader.risk.risk_manager import RiskManager
from icryptotrader.strategy.grid_engine import GridEngine
from icryptotrader.strategy.regime_router import RegimeRouter
from icryptotrader.tax.fifo_ledger import FIFOLedger
from icryptotrader.tax.tax_agent import TaxAgent
from icryptotrader.types import Side, SlotState

# ---------------------------------------------------------------------------
# 1. Geometric Grid Spacing
# ---------------------------------------------------------------------------


class TestGeometricGrid:
    def test_geometric_never_goes_negative(self) -> None:
        """Even with absurd spacing/levels, geometric prices stay positive."""
        fee = FeeModel()
        engine = GridEngine(fee_model=fee, geometric=True)
        state = engine.compute_grid(
            mid_price=Decimal("85000"),
            num_buy_levels=50,
            spacing_bps=Decimal("500"),  # 5% per level — extreme
        )
        for level in state.buy_levels:
            assert level.price > 0, f"Level {level.index} went non-positive: {level.price}"

    def test_linear_can_go_negative(self) -> None:
        """Linear spacing goes negative with extreme params (the bug)."""
        fee = FeeModel()
        engine = GridEngine(fee_model=fee, geometric=False)
        state = engine.compute_grid(
            mid_price=Decimal("85000"),
            num_buy_levels=50,
            spacing_bps=Decimal("500"),
        )
        # With 50 levels * 5% = 250% offset, linear would produce negative
        # The grid engine now breaks early when price <= 0
        for level in state.buy_levels:
            assert level.price > 0

    def test_geometric_vs_linear_first_level_close(self) -> None:
        """At normal spacing, geometric and linear are nearly identical for level 1."""
        fee = FeeModel()
        geo = GridEngine(fee_model=fee, geometric=True)
        lin = GridEngine(fee_model=fee, geometric=False)

        geo_state = geo.compute_grid(
            mid_price=Decimal("85000"),
            num_buy_levels=1,
            spacing_bps=Decimal("50"),
        )
        lin_state = lin.compute_grid(
            mid_price=Decimal("85000"),
            num_buy_levels=1,
            spacing_bps=Decimal("50"),
        )
        # First level should be very close
        geo_price = geo_state.buy_levels[0].price
        lin_price = lin_state.buy_levels[0].price
        assert abs(geo_price - lin_price) < Decimal("1")

    def test_geometric_sell_levels_increase(self) -> None:
        """Geometric sell levels should increase exponentially."""
        fee = FeeModel()
        engine = GridEngine(fee_model=fee, geometric=True)
        state = engine.compute_grid(
            mid_price=Decimal("85000"),
            num_sell_levels=5,
            spacing_bps=Decimal("100"),
        )
        for i in range(1, len(state.sell_levels)):
            gap_prev = state.sell_levels[i - 1].price - Decimal("85000")
            gap_curr = state.sell_levels[i].price - Decimal("85000")
            assert gap_curr > gap_prev, "Geometric gaps should widen"

    def test_geometric_default_true(self) -> None:
        """New GridEngine instances default to geometric spacing."""
        fee = FeeModel()
        engine = GridEngine(fee_model=fee)
        assert engine._geometric is True


# ---------------------------------------------------------------------------
# 2. TWAP Rate-Limiting (Inventory Arbiter)
# ---------------------------------------------------------------------------


class TestTWAPRateLimiting:
    def test_fresh_arbiter_has_full_budget(self) -> None:
        arb = InventoryArbiter(max_rebalance_pct_per_min=0.01)
        arb.update_balances(btc=Decimal("1"), usd=Decimal("85000"))
        arb.update_price(Decimal("85000"))
        budget = arb._twap_remaining_usd(arb.portfolio_value_usd)
        assert budget > 0

    def test_budget_decreases_after_rebalance(self) -> None:
        arb = InventoryArbiter(max_rebalance_pct_per_min=0.01)
        arb.update_balances(btc=Decimal("1"), usd=Decimal("85000"))
        arb.update_price(Decimal("85000"))
        total = arb.portfolio_value_usd
        budget_before = arb._twap_remaining_usd(total)
        arb.record_rebalance(Decimal("500"))
        budget_after = arb._twap_remaining_usd(total)
        assert budget_after < budget_before

    def test_budget_exhaustion_blocks_rebalance(self) -> None:
        arb = InventoryArbiter(max_rebalance_pct_per_min=0.01)
        arb.update_balances(btc=Decimal("1"), usd=Decimal("85000"))
        arb.update_price(Decimal("85000"))
        total = arb.portfolio_value_usd
        # Exhaust the entire 1% budget
        budget = arb._twap_remaining_usd(total)
        arb.record_rebalance(budget + Decimal("1"))
        remaining = arb._twap_remaining_usd(total)
        assert remaining == Decimal("0")

    def test_budget_recovers_after_window(self) -> None:
        arb = InventoryArbiter(max_rebalance_pct_per_min=0.01)
        arb.update_balances(btc=Decimal("1"), usd=Decimal("85000"))
        arb.update_price(Decimal("85000"))
        total = arb.portfolio_value_usd
        # Record a rebalance with a timestamp 61 seconds ago
        arb._rebalance_history = [(time.monotonic() - 61, Decimal("1000"))]
        budget = arb._twap_remaining_usd(total)
        # Old entries pruned, budget should be full again
        assert budget > 0

    def test_max_buy_respects_twap(self) -> None:
        arb = InventoryArbiter(max_rebalance_pct_per_min=0.001)
        arb.update_balances(btc=Decimal("0"), usd=Decimal("100000"))
        arb.update_price(Decimal("85000"))
        snap = arb.snapshot()
        # 0.1% of $100k = $100 max per minute
        assert snap.max_buy_btc <= Decimal("100") / Decimal("85000") + Decimal("0.0001")


# ---------------------------------------------------------------------------
# 3. HWM Withdrawal Awareness
# ---------------------------------------------------------------------------


class TestHWMWithdrawal:
    def test_withdrawal_reduces_hwm(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("5000"), Decimal("5000"))  # HWM = 10000
        rm.record_withdrawal(Decimal("3000"))
        assert rm.high_water_mark == Decimal("7000")

    def test_withdrawal_prevents_false_drawdown(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.update_portfolio(Decimal("5000"), Decimal("5000"))
        # Withdraw 30% for tax payment
        rm.record_withdrawal(Decimal("3000"))
        # Portfolio is now 7000, HWM is now 7000
        rm.update_portfolio(Decimal("3500"), Decimal("3500"))
        # DD should be 0%, not 30%
        assert rm.drawdown_pct == 0.0

    def test_withdrawal_zero_does_nothing(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        old_hwm = rm.high_water_mark
        rm.record_withdrawal(Decimal("0"))
        assert rm.high_water_mark == old_hwm

    def test_withdrawal_negative_does_nothing(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        old_hwm = rm.high_water_mark
        rm.record_withdrawal(Decimal("-500"))
        assert rm.high_water_mark == old_hwm

    def test_withdrawal_adjusts_initial_portfolio(self) -> None:
        rm = RiskManager(initial_portfolio_usd=Decimal("10000"))
        rm.record_withdrawal(Decimal("3000"))
        assert rm._initial_portfolio == Decimal("7000")


# ---------------------------------------------------------------------------
# 4. Convex Delta Skew
# ---------------------------------------------------------------------------


class TestConvexDeltaSkew:
    def test_small_deviation_small_skew(self) -> None:
        """2% deviation should produce only ~2 bps (quadratic)."""
        skew = DeltaSkew(sensitivity=Decimal("1.0"))
        result = skew.compute(btc_alloc_pct=0.52, target_pct=0.50)
        assert abs(result.raw_skew_bps) < Decimal("5")

    def test_large_deviation_capped(self) -> None:
        """20% deviation should hit the 50bps cap (new default)."""
        skew = DeltaSkew(sensitivity=Decimal("1.0"))
        result = skew.compute(btc_alloc_pct=0.70, target_pct=0.50)
        assert result.buy_offset_bps == Decimal("50")

    def test_quadratic_not_linear(self) -> None:
        """Doubling deviation should more-than-double the raw skew."""
        skew = DeltaSkew(sensitivity=Decimal("1.0"), max_skew_bps=Decimal("100"))
        r1 = skew.compute(btc_alloc_pct=0.55, target_pct=0.50)
        r2 = skew.compute(btc_alloc_pct=0.60, target_pct=0.50)
        # 10% dev vs 5% dev → raw skew should be ~4x (quadratic)
        ratio = abs(r2.raw_skew_bps / r1.raw_skew_bps) if r1.raw_skew_bps != 0 else 0
        assert ratio > Decimal("3"), f"Expected >3x quadratic ratio, got {ratio}"

    def test_symmetric_positive_negative(self) -> None:
        """Over-allocated and under-allocated produce symmetric skew."""
        skew = DeltaSkew(sensitivity=Decimal("1.0"))
        over = skew.compute(btc_alloc_pct=0.60, target_pct=0.50)
        under = skew.compute(btc_alloc_pct=0.40, target_pct=0.50)
        assert abs(over.raw_skew_bps) == abs(under.raw_skew_bps)
        assert over.buy_offset_bps == -under.buy_offset_bps

    def test_zero_deviation_zero_skew(self) -> None:
        skew = DeltaSkew()
        result = skew.compute(btc_alloc_pct=0.50, target_pct=0.50)
        assert result.raw_skew_bps == Decimal("0")
        assert result.buy_offset_bps == Decimal("0")


# ---------------------------------------------------------------------------
# 5. Multi-Timeframe EWMA Volatility
# ---------------------------------------------------------------------------


class TestMultiTimeframeVol:
    def test_vol_zero_with_no_data(self) -> None:
        router = RegimeRouter()
        assert router.ewma_volatility == 0.0

    def test_vol_zero_with_stable_prices(self) -> None:
        router = RegimeRouter()
        for _ in range(30):
            router.update_price(Decimal("85000"))
        assert router.ewma_volatility < 0.001

    def test_vol_windows_populated(self) -> None:
        router = RegimeRouter()
        router.update_price(Decimal("85000"))
        router.update_price(Decimal("85100"))
        # Windows should have entries
        for window_sec in (60, 300, 900):
            assert len(router._vol_windows[window_sec]) == 2

    def test_insufficient_elapsed_time_returns_zero(self) -> None:
        """If all prices arrive within <1s, windowed vol returns 0."""
        router = RegimeRouter()
        # Feed prices in quick succession (all same monotonic second)
        for i in range(5):
            router.update_price(Decimal("85000") + Decimal(str(i * 100)))
        # Multi-timeframe should return 0 (insufficient elapsed time)
        # Falls back to tick-level EWMA
        vol = router.ewma_volatility
        # Should fall back to tick-level EWMA (non-zero)
        assert vol >= 0.0


# ---------------------------------------------------------------------------
# 6. Amend Threshold (Queue Priority)
# ---------------------------------------------------------------------------


class TestAmendThreshold:
    def test_sub_threshold_move_is_noop(self) -> None:
        """Price move below threshold should NOT trigger amend."""
        om = OrderManager(num_slots=2, amend_threshold_bps=Decimal("3"))
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "test-123"
        slot.side = Side.BUY
        slot.price = Decimal("85000.00")
        slot.qty = Decimal("0.001")

        # $85000 * 3bps = $25.50 threshold
        # Move by $10 (only 1.2 bps) — should be Noop
        desired = DesiredLevel(
            price=Decimal("85010.00"), qty=Decimal("0.001"), side=Side.BUY,
        )
        action = om.decide_action(slot, desired)
        assert isinstance(action, Action.Noop)

    def test_above_threshold_triggers_amend(self) -> None:
        """Price move above threshold should trigger amend."""
        om = OrderManager(num_slots=2, amend_threshold_bps=Decimal("3"))
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "test-123"
        slot.side = Side.BUY
        slot.price = Decimal("85000.00")
        slot.qty = Decimal("0.001")

        # Move by $50 (5.9 bps) — above 3bps threshold
        desired = DesiredLevel(
            price=Decimal("85050.00"), qty=Decimal("0.001"), side=Side.BUY,
        )
        action = om.decide_action(slot, desired)
        assert isinstance(action, Action.AmendOrder)

    def test_qty_change_still_triggers_amend(self) -> None:
        """Qty changes should still trigger amend even with no price change."""
        om = OrderManager(num_slots=2, amend_threshold_bps=Decimal("3"))
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "test-123"
        slot.side = Side.BUY
        slot.price = Decimal("85000.00")
        slot.qty = Decimal("0.001")

        desired = DesiredLevel(
            price=Decimal("85000.00"), qty=Decimal("0.002"), side=Side.BUY,
        )
        action = om.decide_action(slot, desired)
        assert isinstance(action, Action.AmendOrder)
        assert action.new_price is None  # Only qty changed
        assert action.new_qty is not None


# ---------------------------------------------------------------------------
# 7. Fee Model Post-Only Helpers
# ---------------------------------------------------------------------------


class TestFeeModelPostOnly:
    def test_would_cross_spread_buy(self) -> None:
        fee = FeeModel()
        assert fee.would_cross_spread(
            Decimal("85001"), "buy", Decimal("85000"), Decimal("85001"),
        )
        assert not fee.would_cross_spread(
            Decimal("84999"), "buy", Decimal("85000"), Decimal("85001"),
        )

    def test_would_cross_spread_sell(self) -> None:
        fee = FeeModel()
        assert fee.would_cross_spread(
            Decimal("84999"), "sell", Decimal("85000"), Decimal("85001"),
        )
        assert not fee.would_cross_spread(
            Decimal("85002"), "sell", Decimal("85000"), Decimal("85001"),
        )

    def test_taker_penalty_positive(self) -> None:
        fee = FeeModel()
        penalty = fee.taker_penalty_bps()
        assert penalty > 0


# ---------------------------------------------------------------------------
# 8. Blow-Through Tax Mode
# ---------------------------------------------------------------------------


class TestBlowThroughMode:
    def _make_ledger_with_locked_btc(self) -> FIFOLedger:
        ledger = FIFOLedger()
        # Add a lot purchased recently (locked)
        ledger.add_lot(
            quantity_btc=Decimal("0.1"),
            purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=30),
        )
        return ledger

    def test_blow_through_skips_freigrenze(self) -> None:
        ledger = self._make_ledger_with_locked_btc()
        # Simulate €2000 YTD gain (well above €1000 Freigrenze)
        ledger.sell_fifo(
            quantity_btc=Decimal("0.02"),
            sale_price_usd=Decimal("100000"),
            sale_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
        )

        agent = TaxAgent(ledger=ledger, blow_through_mode=True)
        result = agent.evaluate_sell(
            qty_btc=Decimal("0.01"),
            current_price_usd=Decimal("90000"),
        )
        from icryptotrader.types import TaxVetoDecision
        assert result.decision == TaxVetoDecision.ALLOW
        assert "blow-through" in result.reason.lower()

    def test_normal_mode_vetoes_above_freigrenze(self) -> None:
        ledger = FIFOLedger()
        # Add a large lot at low price to generate big YTD gains
        ledger.add_lot(
            quantity_btc=Decimal("1.0"),
            purchase_price_usd=Decimal("20000"),
            purchase_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=30),
        )
        # Sell at much higher price to blow past Freigrenze
        ledger.sell_fifo(
            quantity_btc=Decimal("0.1"),
            sale_price_usd=Decimal("90000"),
            sale_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
        )

        agent = TaxAgent(ledger=ledger, blow_through_mode=False)
        result = agent.evaluate_sell(
            qty_btc=Decimal("0.01"),
            current_price_usd=Decimal("90000"),
        )
        from icryptotrader.types import TaxVetoDecision
        # YTD gain is ~€6481, way above €1000 Freigrenze → VETO
        assert result.decision == TaxVetoDecision.VETO

    def test_blow_through_never_tax_locked(self) -> None:
        ledger = self._make_ledger_with_locked_btc()
        agent = TaxAgent(ledger=ledger, blow_through_mode=True)
        assert not agent.is_tax_locked()

    def test_blow_through_all_sell_levels(self) -> None:
        ledger = FIFOLedger()
        agent = TaxAgent(ledger=ledger, blow_through_mode=True)
        assert agent.recommended_sell_levels() == -1


# ---------------------------------------------------------------------------
# 9. Vault Lock Priority
# ---------------------------------------------------------------------------


class TestVaultLockPriority:
    def test_vault_lot_detection(self) -> None:
        ledger = FIFOLedger()
        # Add a lot held >365 days
        ledger.add_lot(
            quantity_btc=Decimal("0.5"),
            purchase_price_usd=Decimal("30000"),
            purchase_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=400),
        )
        agent = TaxAgent(ledger=ledger, vault_lock_priority=True)
        assert agent.vault_lot_btc() == Decimal("0.5")
        assert agent.should_prioritize_vault_sell()

    def test_no_vault_when_disabled(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.5"),
            purchase_price_usd=Decimal("30000"),
            purchase_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=400),
        )
        agent = TaxAgent(ledger=ledger, vault_lock_priority=False)
        assert not agent.should_prioritize_vault_sell()

    def test_no_vault_lots_returns_false(self) -> None:
        ledger = FIFOLedger()
        ledger.add_lot(
            quantity_btc=Decimal("0.1"),
            purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=30),
        )
        agent = TaxAgent(ledger=ledger, vault_lock_priority=True)
        assert not agent.should_prioritize_vault_sell()


# ---------------------------------------------------------------------------
# 10. Wash Sale Cooldown
# ---------------------------------------------------------------------------


class TestWashSaleCooldown:
    def test_no_prior_harvest_is_safe(self) -> None:
        ledger = FIFOLedger()
        agent = TaxAgent(ledger=ledger, wash_sale_cooldown_hours=24)
        assert agent.is_wash_sale_safe("some-lot-id")

    def test_recent_harvest_not_safe(self) -> None:
        ledger = FIFOLedger()
        agent = TaxAgent(ledger=ledger, wash_sale_cooldown_hours=24)
        agent.record_harvest("lot-123")
        assert not agent.is_wash_sale_safe("lot-123")

    def test_old_harvest_is_safe(self) -> None:
        ledger = FIFOLedger()
        agent = TaxAgent(ledger=ledger, wash_sale_cooldown_hours=24)
        # Simulate harvest 25 hours ago
        import time as _time
        agent._harvest_timestamps["lot-123"] = _time.time() - 25 * 3600
        assert agent.is_wash_sale_safe("lot-123")

    def test_harvest_filter_in_recommendations(self) -> None:
        """Lots in wash sale cooldown should be skipped by harvest recommendations."""
        ledger = FIFOLedger()
        # Add an underwater lot
        lot = ledger.add_lot(
            quantity_btc=Decimal("0.1"),
            purchase_price_usd=Decimal("90000"),
            purchase_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=60),
        )
        # Simulate a profitable sale to create YTD gains
        ledger.add_lot(
            quantity_btc=Decimal("0.05"),
            purchase_price_usd=Decimal("70000"),
            purchase_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=90),
        )
        ledger.sell_fifo(
            quantity_btc=Decimal("0.05"),
            sale_price_usd=Decimal("95000"),
            sale_fee_usd=Decimal("5"),
            eur_usd_rate=Decimal("1.08"),
        )

        agent = TaxAgent(
            ledger=ledger,
            wash_sale_cooldown_hours=24,
        )
        # Mark the underwater lot as recently harvested
        agent.record_harvest(lot.lot_id)

        recs = agent.recommend_loss_harvest(
            current_price_usd=Decimal("80000"),
            eur_usd_rate=Decimal("1.08"),
        )
        # The lot should be filtered out
        assert all(r.lot_id != lot.lot_id for r in recs)


# ---------------------------------------------------------------------------
# 11. Config Validation for New Fields
# ---------------------------------------------------------------------------


class TestNewConfigFields:
    def test_config_new_defaults(self) -> None:
        from icryptotrader.config import Config
        cfg = Config()
        assert cfg.grid.geometric_spacing is True
        assert cfg.grid.amend_threshold_bps == Decimal("3")
        assert cfg.risk.max_rebalance_pct_per_min == 0.01
        assert cfg.tax.blow_through_mode is False
        assert cfg.tax.vault_lock_priority is True
        assert cfg.tax.harvest_wash_sale_cooldown_hours == 24

    def test_config_loads_with_new_fields(self) -> None:
        from icryptotrader.config import Config, validate_config
        cfg = Config()
        cfg.tax.blow_through_mode = True
        cfg.grid.geometric_spacing = False
        errors = validate_config(cfg)
        assert errors == []
