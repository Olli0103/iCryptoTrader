"""Tests for new strategy loop wiring — Bollinger, AI signals, SQLite persistence."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from pathlib import Path

from icryptotrader.fee.fee_model import FeeModel
from icryptotrader.inventory.inventory_arbiter import InventoryArbiter
from icryptotrader.order.order_manager import OrderManager
from icryptotrader.risk.delta_skew import DeltaSkew
from icryptotrader.risk.risk_manager import RiskManager
from icryptotrader.strategy.bollinger import BollingerSpacing
from icryptotrader.strategy.grid_engine import GridEngine
from icryptotrader.strategy.regime_router import RegimeRouter
from icryptotrader.strategy.strategy_loop import StrategyLoop
from icryptotrader.tax.fifo_ledger import FIFOLedger
from icryptotrader.tax.tax_agent import TaxAgent


def _make_loop(
    bollinger: BollingerSpacing | None = None,
    ai_signal: object = None,
    persistence_backend: str = "json",
    btc: Decimal = Decimal("0.03"),
    usd: Decimal = Decimal("2500"),
    btc_price: Decimal = Decimal("85000"),
) -> StrategyLoop:
    fee_model = FeeModel(volume_30d_usd=0)
    ledger = FIFOLedger()
    om = OrderManager(num_slots=10)
    grid = GridEngine(fee_model=fee_model)
    tax_agent = TaxAgent(ledger=ledger)
    risk_mgr = RiskManager(initial_portfolio_usd=btc * btc_price + usd)
    skew = DeltaSkew()
    inventory = InventoryArbiter()
    inventory.update_balances(btc=btc, usd=usd)
    inventory.update_price(btc_price)
    regime = RegimeRouter()

    return StrategyLoop(
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
        ai_signal=ai_signal,
        persistence_backend=persistence_backend,
    )


class TestBollingerWiring:
    def test_tick_with_bollinger(self) -> None:
        bb = BollingerSpacing(window=5)
        loop = _make_loop(bollinger=bb)
        # First few ticks fill the window
        for i in range(10):
            commands = loop.tick(mid_price=Decimal("85000") + Decimal(str(i * 10)))
            assert isinstance(commands, list)
        assert loop.ticks == 10
        # Price window should be populated
        assert len(loop._price_window) == 10

    def test_bollinger_influences_spacing(self) -> None:
        bb = BollingerSpacing(window=3, min_spacing_bps=Decimal("30"))
        loop = _make_loop(bollinger=bb)
        # Feed enough data to fill window
        for i in range(5):
            loop.tick(mid_price=Decimal("85000") + Decimal(str(i * 100)))
        # Bollinger should now be active
        assert bb.state is not None

    def test_tick_without_bollinger(self) -> None:
        loop = _make_loop(bollinger=None)
        commands = loop.tick(mid_price=Decimal("85000"))
        assert isinstance(commands, list)


class TestAISignalWiring:
    def test_tick_with_ai_signal(self) -> None:
        from icryptotrader.strategy.ai_signal import AISignal, SignalDirection

        mock_signal = AISignal(
            direction=SignalDirection.BUY,
            confidence=0.8,
            reasoning="test",
            suggested_bias_bps=Decimal("10"),
            regime_hint="range_bound",
            provider="test",
            model="test",
            latency_ms=50.0,
            timestamp=0.0,
            error="",
        )

        ai = MagicMock()
        ai.last_signal = mock_signal
        ai.weight = 0.3

        loop = _make_loop(ai_signal=ai)
        commands = loop.tick(mid_price=Decimal("85000"))
        assert isinstance(commands, list)
        assert loop.ticks == 1

    def test_tick_with_neutral_signal(self) -> None:
        from icryptotrader.strategy.ai_signal import AISignal, SignalDirection

        mock_signal = AISignal(
            direction=SignalDirection.NEUTRAL,
            confidence=0.0,
            reasoning="",
            suggested_bias_bps=Decimal("0"),
            regime_hint="none",
            provider="test",
            model="test",
            latency_ms=0.0,
            timestamp=0.0,
            error="",
        )

        ai = MagicMock()
        ai.last_signal = mock_signal
        ai.weight = 0.3

        loop = _make_loop(ai_signal=ai)
        commands = loop.tick(mid_price=Decimal("85000"))
        assert isinstance(commands, list)

    def test_build_ai_context(self) -> None:
        loop = _make_loop()
        ctx = loop.build_ai_context()
        assert "mid_price" in ctx
        assert "regime" in ctx
        assert "drawdown_pct" in ctx
        assert "ytd_taxable_gain_eur" in ctx


class TestSQLitePersistence:
    def test_save_load_sqlite(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.json"
        loop = _make_loop(persistence_backend="sqlite")
        loop._ledger_path = ledger_path

        # Add a lot
        loop._ledger.add_lot(
            quantity_btc=Decimal("0.01"),
            purchase_price_usd=Decimal("85000"),
            purchase_fee_usd=Decimal("1"),
            eur_usd_rate=Decimal("1.08"),
        )

        # Save as SQLite
        loop.save_ledger()
        db_path = ledger_path.with_suffix(".db")
        assert db_path.exists()

        # Load into new ledger
        loop2 = _make_loop(persistence_backend="sqlite")
        loop2._ledger_path = ledger_path
        loop2.load_ledger()
        assert loop2._ledger.total_btc() == Decimal("0.01")

    def test_save_load_json(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.json"
        loop = _make_loop(persistence_backend="json")
        loop._ledger_path = ledger_path

        loop._ledger.add_lot(
            quantity_btc=Decimal("0.02"),
            purchase_price_usd=Decimal("80000"),
            purchase_fee_usd=Decimal("1"),
            eur_usd_rate=Decimal("1.08"),
        )

        loop.save_ledger()
        assert ledger_path.exists()

        loop2 = _make_loop(persistence_backend="json")
        loop2._ledger_path = ledger_path
        loop2.load_ledger()
        assert loop2._ledger.total_btc() == Decimal("0.02")


class TestBotSnapshot:
    def test_snapshot_includes_ai_fields(self) -> None:
        from icryptotrader.strategy.ai_signal import AISignal, SignalDirection

        mock_signal = AISignal(
            direction=SignalDirection.STRONG_BUY,
            confidence=0.95,
            reasoning="test",
            suggested_bias_bps=Decimal("20"),
            regime_hint="trending_up",
            provider="gemini",
            model="gemini-2.0-flash",
            latency_ms=100.0,
            timestamp=0.0,
            error="",
        )

        ai = MagicMock()
        ai.last_signal = mock_signal
        ai._provider = "gemini"
        ai._call_count = 42
        ai._error_count = 2

        loop = _make_loop(ai_signal=ai)
        snap = loop.bot_snapshot()
        assert snap.ai_direction == "STRONG_BUY"
        assert snap.ai_confidence == 0.95
        assert snap.ai_provider == "gemini"
        assert snap.ai_call_count == 42

    def test_snapshot_no_ai(self) -> None:
        loop = _make_loop(ai_signal=None)
        snap = loop.bot_snapshot()
        assert snap.ai_direction == "NEUTRAL"
        assert snap.ai_confidence == 0.0
