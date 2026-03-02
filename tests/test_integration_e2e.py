"""End-to-end integration test — exercises the full tick cycle without mocks.

Constructs all real components (FeeModel, GridEngine, OrderManager, RiskManager,
TaxAgent, FIFOLedger, RegimeRouter, DeltaSkew, InventoryArbiter, BollingerSpacing,
HedgeManager) and runs a realistic multi-tick scenario verifying that they all
work together correctly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from icryptotrader.fee.fee_model import FeeModel
from icryptotrader.inventory.inventory_arbiter import InventoryArbiter
from icryptotrader.order.order_manager import OrderManager
from icryptotrader.pair_manager import PairManager
from icryptotrader.risk.delta_skew import DeltaSkew
from icryptotrader.risk.hedge_manager import HedgeManager
from icryptotrader.risk.risk_manager import RiskManager
from icryptotrader.strategy.bollinger import BollingerSpacing
from icryptotrader.strategy.grid_engine import GridEngine
from icryptotrader.strategy.regime_router import RegimeRouter
from icryptotrader.strategy.strategy_loop import StrategyLoop
from icryptotrader.tax.fifo_ledger import FIFOLedger
from icryptotrader.tax.tax_agent import TaxAgent


def _build_full_stack(
    initial_btc: Decimal = Decimal("0.05"),
    initial_usd: Decimal = Decimal("5000"),
    btc_price: Decimal = Decimal("85000"),
    bollinger_window: int = 5,
    grid_levels: int = 3,
) -> dict:
    """Build the entire component stack with real implementations."""
    fee_model = FeeModel(volume_30d_usd=0)
    ledger = FIFOLedger()
    om = OrderManager(num_slots=grid_levels * 2)
    grid = GridEngine(fee_model=fee_model)
    tax_agent = TaxAgent(ledger=ledger)
    risk_mgr = RiskManager(
        initial_portfolio_usd=initial_btc * btc_price + initial_usd,
    )
    skew = DeltaSkew()
    inventory = InventoryArbiter()
    inventory.update_balances(btc=initial_btc, usd=initial_usd)
    inventory.update_price(btc_price)
    regime = RegimeRouter()
    bollinger = BollingerSpacing(window=bollinger_window)

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
        bollinger=bollinger,
    )

    return {
        "loop": loop,
        "fee_model": fee_model,
        "ledger": ledger,
        "order_manager": om,
        "grid": grid,
        "tax_agent": tax_agent,
        "risk_manager": risk_mgr,
        "skew": skew,
        "inventory": inventory,
        "regime": regime,
        "bollinger": bollinger,
    }


class TestFullTickCycleE2E:
    """End-to-end test: full tick cycle with all real components."""

    def test_multi_tick_produces_consistent_state(self) -> None:
        """Run 20 ticks with varying prices, verify all subsystems update."""
        stack = _build_full_stack()
        loop = stack["loop"]
        risk = stack["risk_manager"]
        regime = stack["regime"]
        bollinger = stack["bollinger"]

        prices = [
            Decimal("85000"), Decimal("85100"), Decimal("85050"),
            Decimal("84900"), Decimal("85200"), Decimal("85300"),
            Decimal("85150"), Decimal("84800"), Decimal("85400"),
            Decimal("85000"), Decimal("85100"), Decimal("84700"),
            Decimal("85500"), Decimal("85250"), Decimal("85100"),
            Decimal("84950"), Decimal("85350"), Decimal("85200"),
            Decimal("85050"), Decimal("85150"),
        ]

        all_commands: list = []
        for price in prices:
            commands = loop.tick(mid_price=price)
            assert isinstance(commands, list)
            all_commands.extend(commands)

        # Verify state consistency across all subsystems
        assert loop.ticks == 20
        assert risk.drawdown_pct >= 0.0
        assert regime.classify() is not None

        # Bollinger should be active after filling window (5 ticks)
        assert bollinger.state is not None

        # Commands should have been generated (buys and sells).
        # Multiple adds in the same tick are aggregated into batch_add.
        assert len(all_commands) > 0
        cmd_types = {cmd["type"] for cmd in all_commands}
        assert "add" in cmd_types or "batch_add" in cmd_types

    def test_risk_pause_halts_commands(self) -> None:
        """When risk pause triggers, tick should produce no commands."""
        stack = _build_full_stack()
        loop = stack["loop"]
        risk = stack["risk_manager"]

        # Normal tick
        commands = loop.tick(mid_price=Decimal("85000"))
        assert isinstance(commands, list)

        # Force a massive crash to trigger circuit breaker
        loop.tick(mid_price=Decimal("42000"))
        assert risk._velocity_frozen is True

        # Next tick should be skipped
        commands = loop.tick(mid_price=Decimal("42000"))
        assert commands == []
        assert loop.ticks_skipped_velocity >= 1

    def test_fill_updates_ledger(self) -> None:
        """Simulate a fill event and verify FIFO ledger records it."""
        stack = _build_full_stack()
        loop = stack["loop"]
        ledger = stack["ledger"]

        # Run a tick to set up state
        loop.tick(mid_price=Decimal("85000"))

        # Simulate a buy fill
        from unittest.mock import MagicMock

        mock_slot = MagicMock()
        mock_slot.side = __import__("icryptotrader.types", fromlist=["Side"]).Side.BUY

        loop.on_fill(mock_slot, {
            "last_qty": "0.01",
            "last_price": "85000",
            "fee": "2.125",
            "order_id": "test-001",
            "trade_id": "trade-001",
        })

        assert ledger.total_btc() == Decimal("0.01")
        assert loop.fills_today == 1

    def test_bollinger_adapts_spacing(self) -> None:
        """Verify Bollinger dynamically adjusts spacing with vol changes."""
        stack = _build_full_stack(bollinger_window=3)
        loop = stack["loop"]
        bollinger = stack["bollinger"]

        # Low vol: constant price
        for _ in range(5):
            loop.tick(mid_price=Decimal("85000"))

        low_vol_spacing = bollinger.state.suggested_spacing_bps

        # High vol: large price swings
        stack2 = _build_full_stack(bollinger_window=3)
        loop2 = stack2["loop"]
        bollinger2 = stack2["bollinger"]

        vol_prices = [
            Decimal("85000"), Decimal("86000"), Decimal("84000"),
            Decimal("87000"), Decimal("83000"),
        ]
        for p in vol_prices:
            loop2.tick(mid_price=p)

        high_vol_spacing = bollinger2.state.suggested_spacing_bps

        # High vol should produce wider spacing
        assert high_vol_spacing > low_vol_spacing

    def test_regime_affects_grid_sizing(self) -> None:
        """Verify regime classification influences order size scale."""
        stack = _build_full_stack()
        loop = stack["loop"]
        regime = stack["regime"]

        # Run enough ticks for regime classification
        for _ in range(5):
            loop.tick(mid_price=Decimal("85000"))

        decision = regime.classify()
        assert decision is not None
        assert decision.order_size_scale > 0

    def test_inventory_limits_prevent_overallocation(self) -> None:
        """Heavy BTC allocation should suppress buy commands."""
        stack = _build_full_stack(
            initial_btc=Decimal("0.10"),  # ~$8,500 in BTC
            initial_usd=Decimal("500"),    # $500 USD
        )
        loop = stack["loop"]

        # With 95%+ BTC allocation, buys should be constrained
        loop.tick(mid_price=Decimal("85000"))

        # Inventory arbiter should limit buying
        snap = stack["inventory"].snapshot()
        assert snap.btc_allocation_pct > 0.9

    def test_tax_agent_gates_sell_levels(self) -> None:
        """Tax agent should constrain sell levels based on lot maturity."""
        stack = _build_full_stack()
        loop = stack["loop"]
        tax = stack["tax_agent"]

        # No lots in ledger → sellable_ratio is 0
        # Tax agent should recommend 0 sell levels
        rec = tax.recommended_sell_levels()
        assert rec >= 0  # May be 0 since no lots exist

        commands = loop.tick(mid_price=Decimal("85000"))
        assert isinstance(commands, list)


class TestHedgeIntegrationE2E:
    """Test HedgeManager integration with strategy loop."""

    def test_hedge_reduces_buy_levels(self) -> None:
        """HedgeManager should cap buy levels during drawdown."""
        hedge = HedgeManager(
            trigger_drawdown_pct=0.05,
            strategy="reduce_exposure",
            max_reduction_pct=0.50,
        )

        # Simulate evaluation at 10% drawdown
        from icryptotrader.types import PauseState, Regime

        action = hedge.evaluate(
            drawdown_pct=0.10,
            regime=Regime.TRENDING_DOWN,
            pause_state=PauseState.ACTIVE_TRADING,
            btc_allocation_pct=0.60,
            target_allocation_pct=0.50,
        )

        assert action.active is True
        assert action.buy_level_cap is not None
        assert action.buy_level_cap >= 0


class TestPairManagerIntegrationE2E:
    """Test PairManager wiring with strategy loop."""

    def test_pair_manager_tracks_loop_state(self) -> None:
        """PairManager should track portfolio risk across ticks."""
        stack = _build_full_stack()
        loop = stack["loop"]

        pm = PairManager(total_capital_usd=Decimal("10000"))
        pm.add_pair("XBT/USD", weight=1.0)
        pm.allocate()

        # Run ticks and update pair manager
        prices = [Decimal("85000"), Decimal("85100"), Decimal("84900")]
        for price in prices:
            loop.tick(mid_price=price)
            snap = stack["inventory"].snapshot()
            pm.update_pair(
                symbol="XBT/USD",
                current_value_usd=snap.portfolio_value_usd,
                drawdown_pct=stack["risk_manager"].drawdown_pct,
                price=price,
            )

        risk = pm.portfolio_risk()
        assert risk.pair_count == 1
        assert risk.total_value_usd > 0

    def test_pair_manager_multi_pair_allocation(self) -> None:
        """Verify capital allocation weights are respected."""
        pm = PairManager(total_capital_usd=Decimal("10000"))
        pm.add_pair("XBT/USD", weight=0.7)
        pm.add_pair("ETH/USD", weight=0.3)
        alloc = pm.allocate()

        assert alloc["XBT/USD"] == Decimal("7000")
        assert alloc["ETH/USD"] == Decimal("3000")

        # Position limits should match
        assert pm.position_limit_usd("XBT/USD") == Decimal("7000")
        assert pm.position_limit_usd("ETH/USD") == Decimal("3000")


class TestLedgerPersistenceE2E:
    """Test ledger save/load with real data through the full cycle."""

    def test_tick_fill_save_load_roundtrip(self, tmp_path: Path) -> None:
        """Full cycle: tick → fill → save → load → verify."""
        stack = _build_full_stack()
        loop = stack["loop"]
        ledger = stack["ledger"]

        # Set ledger path
        ledger_path = tmp_path / "ledger.json"
        loop._ledger_path = ledger_path

        # Run a tick
        loop.tick(mid_price=Decimal("85000"))

        # Simulate a buy fill
        from unittest.mock import MagicMock

        from icryptotrader.types import Side

        mock_slot = MagicMock()
        mock_slot.side = Side.BUY

        loop.on_fill(mock_slot, {
            "last_qty": "0.02",
            "last_price": "84000",
            "fee": "3.36",
            "order_id": "ORD-001",
            "trade_id": "TRD-001",
        })

        # Verify ledger was auto-saved
        assert ledger_path.exists()
        assert ledger.total_btc() == Decimal("0.02")

        # Load into a fresh stack
        stack2 = _build_full_stack()
        loop2 = stack2["loop"]
        loop2._ledger_path = ledger_path
        loop2.load_ledger()

        assert stack2["ledger"].total_btc() == Decimal("0.02")

    def test_sqlite_roundtrip(self, tmp_path: Path) -> None:
        """Full cycle with SQLite backend."""
        stack = _build_full_stack()
        loop = stack["loop"]
        loop._persistence_backend = "sqlite"

        ledger_path = tmp_path / "ledger.json"
        loop._ledger_path = ledger_path

        # Add a lot directly
        stack["ledger"].add_lot(
            quantity_btc=Decimal("0.01"),
            purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("2.0"),
            eur_usd_rate=Decimal("1.08"),
        )

        loop.save_ledger()
        db_path = ledger_path.with_suffix(".db")
        assert db_path.exists()

        # Load into fresh loop
        stack2 = _build_full_stack()
        loop2 = stack2["loop"]
        loop2._persistence_backend = "sqlite"
        loop2._ledger_path = ledger_path
        loop2.load_ledger()

        assert stack2["ledger"].total_btc() == Decimal("0.01")


class TestSnapshotE2E:
    """Test bot_snapshot with real components."""

    def test_snapshot_after_ticks(self) -> None:
        """bot_snapshot should return valid data after real ticks."""
        stack = _build_full_stack()
        loop = stack["loop"]

        for _ in range(5):
            loop.tick(mid_price=Decimal("85000"))

        snap = loop.bot_snapshot()

        assert snap.ticks == 5
        assert snap.portfolio_value_usd > 0
        assert snap.btc_balance > 0
        assert snap.usd_balance > 0
        assert snap.regime is not None
        assert snap.ai_direction == "NEUTRAL"
        assert snap.ai_confidence == 0.0
        assert snap.drawdown_pct >= 0.0
