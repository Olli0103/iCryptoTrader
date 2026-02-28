"""Tests for PairManager — multi-pair diversification."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.pair_manager import PairManager, _pearson_correlation


class TestPairManager:
    def test_add_pair(self) -> None:
        pm = PairManager()
        pm.add_pair("XBT/USD", weight=0.6)
        pm.add_pair("ETH/USD", weight=0.4)
        assert pm.pair_count == 2
        assert "XBT/USD" in pm.pairs
        assert "ETH/USD" in pm.pairs

    def test_allocate_by_weight(self) -> None:
        pm = PairManager(total_capital_usd=Decimal("10000"))
        pm.add_pair("XBT/USD", weight=0.6)
        pm.add_pair("ETH/USD", weight=0.4)
        alloc = pm.allocate()
        assert alloc["XBT/USD"] == Decimal("6000")
        assert alloc["ETH/USD"] == Decimal("4000")

    def test_allocate_equal_weight(self) -> None:
        pm = PairManager(total_capital_usd=Decimal("10000"))
        pm.add_pair("XBT/USD", weight=1.0)
        pm.add_pair("ETH/USD", weight=1.0)
        alloc = pm.allocate()
        assert alloc["XBT/USD"] == Decimal("5000")
        assert alloc["ETH/USD"] == Decimal("5000")

    def test_allocate_single_pair(self) -> None:
        pm = PairManager(total_capital_usd=Decimal("5000"))
        pm.add_pair("XBT/USD", weight=1.0)
        alloc = pm.allocate()
        assert alloc["XBT/USD"] == Decimal("5000")

    def test_allocate_empty(self) -> None:
        pm = PairManager()
        alloc = pm.allocate()
        assert alloc == {}

    def test_update_pair(self) -> None:
        pm = PairManager()
        pm.add_pair("XBT/USD")
        pm.update_pair(
            "XBT/USD",
            current_value_usd=Decimal("5500"),
            drawdown_pct=0.02,
            price=Decimal("85000"),
        )
        state = pm.pairs["XBT/USD"]
        assert state.current_value_usd == Decimal("5500")
        assert state.drawdown_pct == 0.02
        assert state.last_price == Decimal("85000")

    def test_update_unknown_pair(self) -> None:
        pm = PairManager()
        pm.update_pair("UNKNOWN", Decimal("0"), 0.0, Decimal("0"))
        # Should not raise

    def test_returns_tracking(self) -> None:
        pm = PairManager()
        pm.add_pair("XBT/USD")
        pm.update_pair("XBT/USD", Decimal("5000"), 0.0, Decimal("100"))
        pm.update_pair("XBT/USD", Decimal("5100"), 0.0, Decimal("102"))
        state = pm.pairs["XBT/USD"]
        assert len(state.returns) == 1
        assert abs(state.returns[0] - 0.02) < 0.001

    def test_portfolio_risk_empty(self) -> None:
        pm = PairManager()
        risk = pm.portfolio_risk()
        assert risk.pair_count == 0
        assert risk.total_value_usd == Decimal("0")

    def test_portfolio_risk_combined_dd(self) -> None:
        pm = PairManager(total_capital_usd=Decimal("10000"))
        pm.add_pair("XBT/USD")
        pm.add_pair("ETH/USD")
        pm.update_pair("XBT/USD", Decimal("4500"), 0.10, Decimal("80000"))
        pm.update_pair("ETH/USD", Decimal("4000"), 0.20, Decimal("3000"))
        risk = pm.portfolio_risk()
        assert risk.pair_count == 2
        assert risk.total_value_usd == Decimal("8500")
        assert risk.max_pair_drawdown_pct == 0.20
        # Combined DD = (10000 - 8500) / 10000 = 15%
        assert abs(risk.combined_drawdown_pct - 0.15) < 0.01

    def test_position_limit(self) -> None:
        pm = PairManager(total_capital_usd=Decimal("10000"))
        pm.add_pair("XBT/USD", weight=0.6)
        pm.add_pair("ETH/USD", weight=0.4)
        assert pm.position_limit_usd("XBT/USD") == Decimal("6000")
        assert pm.position_limit_usd("ETH/USD") == Decimal("4000")

    def test_position_limit_unknown(self) -> None:
        pm = PairManager()
        assert pm.position_limit_usd("UNKNOWN") == Decimal("0")


class TestCorrelation:
    def test_perfect_correlation(self) -> None:
        xs = [0.01 * i for i in range(20)]
        corr = _pearson_correlation(xs, xs)
        assert corr is not None
        assert abs(corr - 1.0) < 0.001

    def test_negative_correlation(self) -> None:
        xs = [float(i) for i in range(20)]
        ys = [float(-i) for i in range(20)]
        corr = _pearson_correlation(xs, ys)
        assert corr is not None
        assert abs(corr - (-1.0)) < 0.001

    def test_too_few_points(self) -> None:
        corr = _pearson_correlation([1.0, 2.0], [1.0, 2.0])
        assert corr is None

    def test_constant_series(self) -> None:
        corr = _pearson_correlation([1.0] * 10, [2.0] * 10)
        assert corr is not None
        assert corr == 0.0
