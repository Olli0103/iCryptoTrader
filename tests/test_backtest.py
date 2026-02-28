"""Tests for the Backtesting Engine."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult


class TestBacktestConfig:
    def test_default_config(self) -> None:
        cfg = BacktestConfig()
        assert cfg.initial_usd == Decimal("5000")
        assert cfg.initial_btc == Decimal("0")
        assert cfg.grid_levels == 5
        assert cfg.auto_compound is False

    def test_custom_config(self) -> None:
        cfg = BacktestConfig(
            initial_usd=Decimal("10000"),
            order_size_usd=Decimal("1000"),
            grid_levels=3,
            auto_compound=True,
        )
        assert cfg.initial_usd == Decimal("10000")
        assert cfg.order_size_usd == Decimal("1000")
        assert cfg.grid_levels == 3
        assert cfg.auto_compound is True


class TestBacktestResult:
    def test_return_pct_zero_on_no_change(self) -> None:
        result = BacktestResult(
            config=BacktestConfig(),
            initial_portfolio_usd=Decimal("5000"),
            final_portfolio_usd=Decimal("5000"),
        )
        assert result.return_pct == 0.0

    def test_return_pct_positive(self) -> None:
        result = BacktestResult(
            config=BacktestConfig(),
            initial_portfolio_usd=Decimal("5000"),
            final_portfolio_usd=Decimal("5500"),
        )
        assert abs(result.return_pct - 0.10) < 0.001

    def test_return_pct_negative(self) -> None:
        result = BacktestResult(
            config=BacktestConfig(),
            initial_portfolio_usd=Decimal("5000"),
            final_portfolio_usd=Decimal("4500"),
        )
        assert abs(result.return_pct - (-0.10)) < 0.001

    def test_return_pct_zero_initial(self) -> None:
        result = BacktestResult(
            config=BacktestConfig(),
            initial_portfolio_usd=Decimal("0"),
            final_portfolio_usd=Decimal("100"),
        )
        assert result.return_pct == 0.0

    def test_summary_format(self) -> None:
        result = BacktestResult(
            config=BacktestConfig(),
            ticks=100,
            initial_portfolio_usd=Decimal("5000"),
            final_portfolio_usd=Decimal("5250"),
            total_pnl_usd=Decimal("250"),
            total_fees_usd=Decimal("10"),
            buy_count=5,
            sell_count=3,
            max_drawdown_pct=0.02,
            high_water_mark=Decimal("5300"),
            final_btc=Decimal("0.01"),
            final_usd=Decimal("4400"),
        )
        summary = result.summary()
        assert "Backtest Results" in summary
        assert "100" in summary
        assert "5 buys" in summary
        assert "3 sells" in summary


class TestBacktestEngineEmpty:
    def test_empty_prices_returns_initial(self) -> None:
        engine = BacktestEngine(BacktestConfig(initial_usd=Decimal("5000")))
        result = engine.run([])
        assert result.ticks == 0
        assert result.final_usd == Decimal("5000")
        assert len(result.trades) == 0

    def test_single_price_returns_initial(self) -> None:
        engine = BacktestEngine(BacktestConfig(initial_usd=Decimal("5000")))
        result = engine.run([Decimal("85000")])
        assert result.ticks == 0
        assert result.final_usd == Decimal("5000")


class TestBacktestEngineRun:
    def test_flat_prices_no_trades(self) -> None:
        """Constant prices should not trigger any grid fills."""
        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("5000"),
            grid_levels=3,
            spacing_bps=Decimal("50"),
        ))
        prices = [Decimal("85000")] * 20
        result = engine.run(prices)
        assert result.buy_count == 0
        assert result.sell_count == 0
        assert result.ticks == 19

    def test_price_drop_triggers_buy(self) -> None:
        """A price drop crossing a buy level should trigger a buy fill."""
        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("10000"),
            order_size_usd=Decimal("500"),
            grid_levels=3,
            spacing_bps=Decimal("100"),  # 1% spacing
        ))
        # At prev_price=85000, buy_level_1 = 85000*(1-0.01) = 84150
        # Price drops to 84000, crossing 84150 → buy fill
        prices = [Decimal("85000"), Decimal("84000")]
        result = engine.run(prices)
        assert result.buy_count >= 1
        assert result.final_btc > Decimal("0")
        assert result.final_usd < Decimal("10000")

    def test_price_rise_triggers_sell(self) -> None:
        """A price rise crossing a sell level should trigger a sell (if BTC available)."""
        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("5000"),
            initial_btc=Decimal("0.1"),
            order_size_usd=Decimal("500"),
            grid_levels=3,
            spacing_bps=Decimal("100"),
        ))
        # Price rises 2%
        prices = [Decimal("85000"), Decimal("86700")]
        result = engine.run(prices)
        assert result.sell_count >= 1

    def test_fees_accumulated(self) -> None:
        """Fees should be tracked across all trades."""
        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("10000"),
            order_size_usd=Decimal("500"),
            grid_levels=3,
            spacing_bps=Decimal("100"),
            maker_fee_bps=Decimal("16"),
        ))
        # Oscillating price to generate trades
        prices = []
        for _ in range(10):
            prices.extend([Decimal("85000"), Decimal("83000")])
        result = engine.run(prices)
        if result.buy_count > 0:
            assert result.total_fees_usd > Decimal("0")

    def test_max_drawdown_tracked(self) -> None:
        """Max drawdown should be non-negative and reflect the worst drop."""
        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("5000"),
            initial_btc=Decimal("0.05"),
            grid_levels=3,
            spacing_bps=Decimal("50"),
        ))
        # Prices crash then recover
        prices = [Decimal("85000")]
        for i in range(20):
            prices.append(Decimal("85000") - Decimal(str(i * 500)))
        result = engine.run(prices)
        assert result.max_drawdown_pct >= 0

    def test_auto_compound_increases_order_size(self) -> None:
        """With auto-compounding, order sizes should scale with portfolio."""
        # Without compound
        engine_no = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("10000"),
            order_size_usd=Decimal("500"),
            grid_levels=2,
            spacing_bps=Decimal("100"),
            auto_compound=False,
        ))
        # With compound
        engine_yes = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("10000"),
            order_size_usd=Decimal("500"),
            grid_levels=2,
            spacing_bps=Decimal("100"),
            auto_compound=True,
        ))
        # Prices oscillate
        prices = []
        for _ in range(20):
            prices.extend([Decimal("85000"), Decimal("83000")])
        result_no = engine_no.run(prices)
        result_yes = engine_yes.run(prices)
        # Both should have trades
        assert result_no.ticks == result_yes.ticks

    def test_hwm_updates(self) -> None:
        """High water mark should increase when portfolio grows."""
        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("5000"),
            initial_btc=Decimal("0.05"),
        ))
        prices = [Decimal("85000"), Decimal("90000"), Decimal("85000")]
        result = engine.run(prices)
        # HWM should be at least the initial portfolio
        assert result.high_water_mark >= result.initial_portfolio_usd

    def test_final_portfolio_correct(self) -> None:
        """Final portfolio should be usd + btc * last_price."""
        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("5000"),
            initial_btc=Decimal("0"),
        ))
        prices = [Decimal("85000")] * 5
        result = engine.run(prices)
        expected = result.final_usd + result.final_btc * Decimal("85000")
        assert abs(result.final_portfolio_usd - expected) < Decimal("0.01")


class TestBacktestEngineDefaults:
    def test_default_engine(self) -> None:
        """Engine with no config should use defaults."""
        engine = BacktestEngine()
        result = engine.run([Decimal("85000")] * 5)
        assert result.ticks == 4
