"""Tests for the Strategy Loop integration wiring."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.fee.fee_model import FeeModel
from icryptotrader.inventory.inventory_arbiter import InventoryArbiter
from icryptotrader.order.order_manager import OrderManager
from icryptotrader.risk.delta_skew import DeltaSkew
from icryptotrader.risk.risk_manager import RiskManager
from icryptotrader.strategy.grid_engine import GridEngine
from icryptotrader.strategy.regime_router import RegimeRouter
from icryptotrader.strategy.strategy_loop import StrategyLoop
from icryptotrader.tax.fifo_ledger import FIFOLedger
from icryptotrader.tax.tax_agent import TaxAgent
from icryptotrader.types import Side


def _make_loop(
    num_slots: int = 10,
    btc: Decimal = Decimal("0.03"),
    usd: Decimal = Decimal("2500"),
    btc_price: Decimal = Decimal("85000"),
) -> StrategyLoop:
    """Create a fully wired strategy loop for testing."""
    fee_model = FeeModel(volume_30d_usd=0)
    ledger = FIFOLedger()
    om = OrderManager(num_slots=num_slots)
    grid = GridEngine(fee_model=fee_model)
    tax_agent = TaxAgent(ledger=ledger)
    risk_mgr = RiskManager(
        initial_portfolio_usd=btc * btc_price + usd,
    )
    skew = DeltaSkew()
    inventory = InventoryArbiter()
    inventory.update_balances(btc=btc, usd=usd)
    inventory.update_price(btc_price)
    regime = RegimeRouter()

    loop = StrategyLoop(
        fee_model=fee_model,
        order_manager=om,
        grid_engine=grid,
        tax_agent=tax_agent,
        risk_manager=risk_mgr,
        delta_skew=skew,
        inventory=inventory,
        regime_router=regime,
        ledger=ledger,
    )
    # Register fill callback
    om.on_fill(loop.on_fill)
    return loop


class TestBasicTick:
    def test_tick_returns_commands(self) -> None:
        loop = _make_loop()
        commands = loop.tick(mid_price=Decimal("85000"))
        assert isinstance(commands, list)
        # Should issue add_order commands for grid levels
        assert len(commands) > 0

    def test_tick_increments_counter(self) -> None:
        loop = _make_loop()
        loop.tick(mid_price=Decimal("85000"))
        loop.tick(mid_price=Decimal("85100"))
        assert loop.ticks == 2

    def test_commands_have_required_fields(self) -> None:
        loop = _make_loop()
        commands = loop.tick(mid_price=Decimal("85000"))
        for cmd in commands:
            assert "type" in cmd
            assert "params" in cmd
            if cmd["type"] == "batch_add":
                # batch_add uses slot_ids (plural) instead of slot_id
                assert "slot_ids" in cmd
            else:
                assert "slot_id" in cmd
            assert cmd["type"] in ("add", "amend", "cancel", "batch_add", "cancel_all")

    def test_first_tick_issues_add_orders(self) -> None:
        loop = _make_loop()
        commands = loop.tick(mid_price=Decimal("85000"))
        add_commands = [c for c in commands if c["type"] in ("add", "batch_add")]
        assert len(add_commands) > 0


class TestSecondTick:
    def test_second_tick_with_same_price_no_amends(self) -> None:
        loop = _make_loop()
        loop.tick(mid_price=Decimal("85000"))
        # Second tick with same price — slots are PENDING_NEW, so noop
        commands = loop.tick(mid_price=Decimal("85000"))
        # All slots are pending, should be noops
        assert len(commands) == 0

    def test_second_tick_after_price_change(self) -> None:
        loop = _make_loop()
        loop.tick(mid_price=Decimal("85000"))
        # Slots are still PENDING_NEW — no commands even with price change
        commands = loop.tick(mid_price=Decimal("86000"))
        assert len(commands) == 0  # Can't stack on pending


class TestBuySellBalance:
    def test_has_both_buy_and_sell(self) -> None:
        from datetime import UTC, datetime, timedelta

        fee_model = FeeModel(volume_30d_usd=0)
        ledger = FIFOLedger()
        # Add old tax-free lots so tax agent allows sell levels
        ledger.add_lot(
            quantity_btc=Decimal("0.03"),
            purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=400),
        )
        om = OrderManager(num_slots=10)
        grid = GridEngine(fee_model=fee_model)
        tax_agent = TaxAgent(ledger=ledger)
        risk_mgr = RiskManager(initial_portfolio_usd=Decimal("5000"))
        skew = DeltaSkew()
        inventory = InventoryArbiter()
        inventory.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        inventory.update_price(Decimal("85000"))
        regime = RegimeRouter()

        loop = StrategyLoop(
            fee_model=fee_model,
            order_manager=om,
            grid_engine=grid,
            tax_agent=tax_agent,
            risk_manager=risk_mgr,
            delta_skew=skew,
            inventory=inventory,
            regime_router=regime,
            ledger=ledger,
        )

        commands = loop.tick(mid_price=Decimal("85000"))
        sides = set()
        for cmd in commands:
            if cmd["type"] == "add":
                sides.add(cmd["params"].get("side"))
            elif cmd["type"] == "batch_add":
                for order in cmd["params"].get("orders", []):
                    sides.add(order.get("side"))
        assert "buy" in sides
        assert "sell" in sides


class TestTaxLockMode:
    def test_buy_only_when_all_locked(self) -> None:
        """When all BTC is tax-locked, only buy orders should be issued."""
        fee_model = FeeModel(volume_30d_usd=0)
        ledger = FIFOLedger()

        # No lots in ledger (empty) → sellable_ratio = 0 → buy-only
        # But TaxAgent.is_tax_locked() returns False when empty
        # So we need lots that are locked
        from datetime import UTC, datetime, timedelta

        ledger.add_lot(
            quantity_btc=Decimal("0.03"),
            purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("0"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC) - timedelta(days=10),
        )

        om = OrderManager(num_slots=10)
        grid = GridEngine(fee_model=fee_model)
        tax_agent = TaxAgent(ledger=ledger)
        risk_mgr = RiskManager(initial_portfolio_usd=Decimal("5000"))
        skew = DeltaSkew()
        inventory = InventoryArbiter()
        inventory.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        inventory.update_price(Decimal("85000"))
        regime = RegimeRouter()

        loop = StrategyLoop(
            fee_model=fee_model,
            order_manager=om,
            grid_engine=grid,
            tax_agent=tax_agent,
            risk_manager=risk_mgr,
            delta_skew=skew,
            inventory=inventory,
            regime_router=regime,
            ledger=ledger,
        )

        commands = loop.tick(mid_price=Decimal("85000"))
        for cmd in commands:
            if cmd["type"] == "add":
                # All adds should be buys (tax agent blocks sell levels)
                assert cmd["params"]["side"] == "buy"


class TestRiskPause:
    def test_no_commands_during_risk_pause(self) -> None:
        fee_model = FeeModel(volume_30d_usd=0)
        ledger = FIFOLedger()
        om = OrderManager(num_slots=10)
        grid = GridEngine(fee_model=fee_model)
        tax_agent = TaxAgent(ledger=ledger)
        # Disable velocity circuit breaker so risk pause is tested directly
        risk_mgr = RiskManager(
            initial_portfolio_usd=Decimal("5050"),
            price_velocity_freeze_pct=1.0,
        )
        skew = DeltaSkew()
        inventory = InventoryArbiter()
        inventory.update_balances(btc=Decimal("0.03"), usd=Decimal("2500"))
        inventory.update_price(Decimal("85000"))
        regime = RegimeRouter()

        loop = StrategyLoop(
            fee_model=fee_model,
            order_manager=om,
            grid_engine=grid,
            tax_agent=tax_agent,
            risk_manager=risk_mgr,
            delta_skew=skew,
            inventory=inventory,
            regime_router=regime,
            ledger=ledger,
        )

        # First tick — normal
        loop.tick(mid_price=Decimal("85000"))

        # Simulate massive drawdown by updating risk manager directly
        risk_mgr.update_portfolio(
            btc_value_usd=Decimal("1500"),
            usd_balance=Decimal("2000"),
        )

        # Tick with risk pause active (velocity breaker disabled)
        loop.tick(mid_price=Decimal("50000"))
        assert loop.ticks_skipped_risk >= 1


class TestDeltaSkewApplied:
    def test_skew_affects_grid_prices(self) -> None:
        """When BTC allocation deviates from target, buy/sell spacing should differ."""
        fee_model = FeeModel(volume_30d_usd=0)
        ledger = FIFOLedger()
        om_sym = OrderManager(num_slots=10)
        om_skew = OrderManager(num_slots=10)
        grid_sym = GridEngine(fee_model=fee_model)
        grid_skew = GridEngine(fee_model=fee_model)
        tax_agent = TaxAgent(ledger=ledger)
        risk_mgr_sym = RiskManager(initial_portfolio_usd=Decimal("5000"))
        risk_mgr_skew = RiskManager(initial_portfolio_usd=Decimal("5000"))
        regime_sym = RegimeRouter()
        regime_skew = RegimeRouter()

        # Symmetric: balanced allocation (50/50)
        inv_sym = InventoryArbiter()
        inv_sym.update_balances(btc=Decimal("0.03"), usd=Decimal("2550"))
        inv_sym.update_price(Decimal("85000"))

        skew_zero = DeltaSkew(sensitivity=Decimal("2.0"))
        loop_sym = StrategyLoop(
            fee_model=fee_model, order_manager=om_sym, grid_engine=grid_sym,
            tax_agent=tax_agent, risk_manager=risk_mgr_sym, delta_skew=skew_zero,
            inventory=inv_sym, regime_router=regime_sym, ledger=ledger,
        )

        # Skewed: heavily over-allocated to BTC (90% BTC)
        inv_skew = InventoryArbiter()
        inv_skew.update_balances(btc=Decimal("0.10"), usd=Decimal("500"))
        inv_skew.update_price(Decimal("85000"))

        skew_high = DeltaSkew(sensitivity=Decimal("2.0"))
        loop_skew = StrategyLoop(
            fee_model=fee_model, order_manager=om_skew, grid_engine=grid_skew,
            tax_agent=tax_agent, risk_manager=risk_mgr_skew, delta_skew=skew_high,
            inventory=inv_skew, regime_router=regime_skew, ledger=ledger,
        )

        cmds_sym = loop_sym.tick(mid_price=Decimal("85000"))
        cmds_skew = loop_skew.tick(mid_price=Decimal("85000"))

        # Extract buy prices from both
        buy_prices_sym = sorted(
            Decimal(c["params"]["price"]) for c in cmds_sym
            if c["type"] == "add" and c["params"]["side"] == "buy"
        )
        buy_prices_skew = sorted(
            Decimal(c["params"]["price"]) for c in cmds_skew
            if c["type"] == "add" and c["params"]["side"] == "buy"
        )

        # With high BTC allocation, skew should widen buy spacing (lower prices)
        if buy_prices_sym and buy_prices_skew:
            assert buy_prices_skew[0] < buy_prices_sym[0], (
                "Over-allocated BTC should widen buy levels (push further from mid)"
            )


class TestFillHandling:
    def test_buy_fill_adds_lot(self) -> None:
        loop = _make_loop()
        initial_lots = len(loop._ledger.lots)

        class FakeSlot:
            side = Side.BUY
            slot_id = 0

        loop.on_fill(FakeSlot(), {
            "last_qty": "0.005",
            "last_price": "85000",
            "fee": "1.00",
            "order_id": "O123",
            "trade_id": "T456",
        })
        assert len(loop._ledger.lots) == initial_lots + 1

    def test_sell_fill_disposes_lot(self) -> None:
        loop = _make_loop()
        # First add a lot
        from datetime import UTC, datetime

        loop._ledger.add_lot(
            quantity_btc=Decimal("0.01"),
            purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("0"),
            eur_usd_rate=Decimal("1.08"),
            purchase_timestamp=datetime.now(UTC),
        )

        class FakeSlot:
            side = Side.SELL
            slot_id = 0

        loop.on_fill(FakeSlot(), {
            "last_qty": "0.01",
            "last_price": "85000",
            "fee": "1.00",
        })
        assert loop._ledger.total_btc() == Decimal("0")


class TestFifoSellFailureEscalation:
    def test_sell_fifo_failure_triggers_risk_pause(self) -> None:
        """A FIFO sell failure must trigger risk pause to stop trading."""
        loop = _make_loop()
        # Ledger is empty — selling will raise ValueError

        class FakeSlot:
            side = Side.SELL
            slot_id = 0

        loop.on_fill(FakeSlot(), {
            "last_qty": "0.01",
            "last_price": "85000",
            "fee": "1.00",
            "order_id": "O123",
        })
        assert not loop._risk.is_trading_allowed


class TestRateLimiterGate:
    def test_throttled_add_not_dispatched(self) -> None:
        """When rate limiter is saturated, add commands should be skipped."""
        from icryptotrader.order.rate_limiter import RateLimiter

        rl = RateLimiter(max_counter=10, decay_rate=0.0, headroom_pct=0.5)
        # Fill the counter well above threshold (5)
        for _ in range(10):
            rl.record_send(1.0)

        loop = _make_loop()
        loop._om._rate_limiter = rl

        commands = loop.tick(mid_price=Decimal("85000"))
        # All add commands should be throttled
        assert len(commands) == 0
        assert rl.throttle_count > 0


class TestMetrics:
    def test_tick_duration_tracked(self) -> None:
        loop = _make_loop()
        loop.tick(mid_price=Decimal("85000"))
        assert loop.last_tick_duration_ms > 0

    def test_commands_counted(self) -> None:
        loop = _make_loop()
        commands = loop.tick(mid_price=Decimal("85000"))
        assert loop.commands_issued == len(commands)


class TestAutoCompounding:
    def test_compound_order_size_scales_with_portfolio(self) -> None:
        """Order size should scale with portfolio growth."""
        loop = _make_loop()
        loop._auto_compound = True
        loop._compound_base_usd = Decimal("5000")
        loop._base_order_size_usd = Decimal("500")
        # Simulate portfolio at $10000 (2x growth)
        loop._inv.update_balances(btc=Decimal("0.06"), usd=Decimal("5000"))
        loop._inv.update_price(Decimal("85000"))
        size = loop.compound_order_size()
        # Scale = 10100/5000 ≈ 2.02, so size ≈ 1010
        assert size > Decimal("500")

    def test_compound_disabled_returns_base(self) -> None:
        loop = _make_loop()
        loop._auto_compound = False
        loop._base_order_size_usd = Decimal("500")
        size = loop.compound_order_size()
        assert size == Decimal("500")

    def test_compound_zero_base_returns_base(self) -> None:
        loop = _make_loop()
        loop._auto_compound = True
        loop._compound_base_usd = Decimal("0")
        loop._base_order_size_usd = Decimal("500")
        size = loop.compound_order_size()
        assert size == Decimal("500")

    def test_compound_zero_portfolio_returns_base(self) -> None:
        loop = _make_loop(btc=Decimal("0"), usd=Decimal("0"))
        loop._auto_compound = True
        loop._compound_base_usd = Decimal("5000")
        loop._base_order_size_usd = Decimal("500")
        size = loop.compound_order_size()
        assert size == Decimal("500")


class TestBotSnapshot:
    def test_snapshot_returns_all_fields(self) -> None:
        loop = _make_loop()
        loop.tick(mid_price=Decimal("85000"))
        snap = loop.bot_snapshot()
        assert snap.portfolio_value_usd > Decimal("0")
        assert snap.ticks == 1
        assert snap.uptime_sec >= 0
        assert snap.eur_usd_rate == Decimal("1.08")

    def test_snapshot_tracks_fills(self) -> None:
        loop = _make_loop()
        from unittest.mock import MagicMock

        slot = MagicMock()
        slot.side = Side.BUY
        slot.slot_id = 0
        loop.on_fill(slot, {
            "last_qty": "0.01",
            "last_price": "85000",
            "fee": "1.00",
            "order_id": "O1",
            "trade_id": "T1",
        })
        snap = loop.bot_snapshot()
        assert snap.fills_today == 1
        assert snap.open_lots == 1


class TestLedgerPersistence:
    def test_auto_save_on_buy_fill(self, tmp_path) -> None:
        """Ledger is saved to disk after a buy fill."""
        ledger_file = tmp_path / "ledger.json"
        loop = _make_loop()
        loop._ledger_path = ledger_file

        # Simulate a buy fill
        from unittest.mock import MagicMock

        slot = MagicMock()
        slot.side = Side.BUY
        slot.slot_id = 0

        loop.on_fill(slot, {
            "last_qty": "0.01",
            "last_price": "85000",
            "fee": "1.00",
            "order_id": "O1",
            "trade_id": "T1",
        })

        assert ledger_file.exists()
        assert loop._ledger.total_btc() == Decimal("0.01")

    def test_auto_save_on_sell_fill(self, tmp_path) -> None:
        """Ledger is saved to disk after a sell fill."""
        ledger_file = tmp_path / "ledger.json"
        loop = _make_loop()
        loop._ledger_path = ledger_file

        # Add a lot first
        loop._ledger.add_lot(
            quantity_btc=Decimal("0.01"),
            purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("1"),
            eur_usd_rate=Decimal("1.08"),
        )

        from unittest.mock import MagicMock

        slot = MagicMock()
        slot.side = Side.SELL
        slot.slot_id = 0

        loop.on_fill(slot, {
            "last_qty": "0.005",
            "last_price": "85000",
            "fee": "0.50",
            "order_id": "O2",
            "trade_id": "T2",
        })

        assert ledger_file.exists()
        assert loop._ledger.total_btc() == Decimal("0.005")

    def test_no_save_without_ledger_path(self) -> None:
        """No save attempted when ledger_path is None."""
        loop = _make_loop()
        assert loop._ledger_path is None

        from unittest.mock import MagicMock

        slot = MagicMock()
        slot.side = Side.BUY
        slot.slot_id = 0

        # Should not raise even without a path
        loop.on_fill(slot, {
            "last_qty": "0.01",
            "last_price": "85000",
            "fee": "0",
            "order_id": "O3",
            "trade_id": "T3",
        })
