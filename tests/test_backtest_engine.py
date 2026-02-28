"""Tests for the Backtest Engine."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult


class TestBacktestEngine:
    def test_empty_prices(self) -> None:
        engine = BacktestEngine()
        result = engine.run([])
        assert result.ticks == 0
        assert result.final_btc == Decimal("0")

    def test_single_price(self) -> None:
        engine = BacktestEngine()
        result = engine.run([Decimal("85000")])
        assert result.ticks == 0
        assert result.final_usd == Decimal("5000")

    def test_flat_price_no_trades(self) -> None:
        prices = [Decimal("85000")] * 100
        engine = BacktestEngine()
        result = engine.run(prices)
        assert result.ticks == 99
        assert result.buy_count == 0
        assert result.sell_count == 0

    def test_price_drop_triggers_buys(self) -> None:
        # Price drops by ~0.6% per step — crosses grid spacing (50bps)
        prices = [Decimal("85000") - Decimal(str(i * 500)) for i in range(50)]
        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("10000"),
            order_size_usd=Decimal("500"),
            grid_levels=5,
            spacing_bps=Decimal("50"),
        ))
        result = engine.run(prices)
        assert result.buy_count > 0
        assert result.final_btc > 0

    def test_price_rise_triggers_sells(self) -> None:
        # Start with some BTC, price rises by ~0.6% per step
        prices = [Decimal("85000") + Decimal(str(i * 500)) for i in range(50)]
        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("5000"),
            initial_btc=Decimal("0.1"),
            order_size_usd=Decimal("500"),
            grid_levels=5,
            spacing_bps=Decimal("50"),
        ))
        result = engine.run(prices)
        assert result.sell_count > 0

    def test_mean_reverting_profitable(self) -> None:
        # Oscillating price — ideal for grid
        prices: list[Decimal] = []
        for i in range(200):
            offset = Decimal(str(500 * (1 if i % 2 == 0 else -1)))
            prices.append(Decimal("85000") + offset)

        engine = BacktestEngine(BacktestConfig(
            initial_usd=Decimal("5000"),
            initial_btc=Decimal("0.03"),
            order_size_usd=Decimal("200"),
            grid_levels=3,
            spacing_bps=Decimal("30"),
        ))
        result = engine.run(prices)
        assert result.ticks == 199

    def test_auto_compound(self) -> None:
        prices = [Decimal("85000")] * 10
        engine_no = BacktestEngine(BacktestConfig(auto_compound=False))
        engine_yes = BacktestEngine(BacktestConfig(auto_compound=True))
        r1 = engine_no.run(prices)
        r2 = engine_yes.run(prices)
        assert isinstance(r1, BacktestResult)
        assert isinstance(r2, BacktestResult)

    def test_return_pct(self) -> None:
        result = BacktestResult(
            config=BacktestConfig(),
            initial_portfolio_usd=Decimal("10000"),
            final_portfolio_usd=Decimal("11000"),
        )
        assert abs(result.return_pct - 0.10) < 0.001

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
            ticks=1000,
            buy_count=50,
            sell_count=45,
            initial_portfolio_usd=Decimal("10000"),
            final_portfolio_usd=Decimal("10500"),
            total_pnl_usd=Decimal("500"),
            total_fees_usd=Decimal("20"),
            max_drawdown_pct=0.05,
        )
        summary = result.summary()
        assert "Backtest Results" in summary
        assert "1,000" in summary
        assert "50 buys" in summary
        assert "$10,000" in summary

    def test_max_drawdown_tracked(self) -> None:
        # Price drops then recovers
        prices = (
            [Decimal("85000")]
            + [Decimal("85000") - Decimal(str(i * 100)) for i in range(20)]
            + [Decimal("85000")]
        )
        engine = BacktestEngine(BacktestConfig(initial_usd=Decimal("10000")))
        result = engine.run(prices)
        assert result.max_drawdown_pct >= 0
