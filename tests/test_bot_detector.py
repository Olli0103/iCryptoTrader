"""Tests for the bot activity detector (analysis.bot_detector)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from icryptotrader.analysis.bot_detector import BotDetector


class _FakeClock:
    """Injectable monotonic clock for deterministic tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, sec: float) -> None:
        self._t += sec


# ---------------------------------------------------------------------------
# Grid bot detection
# ---------------------------------------------------------------------------


class TestGridBotDetection:
    def test_detects_arithmetic_grid(self) -> None:
        """Trades at regular $100 intervals should trigger grid bot detection."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        # Simulate grid bot: buys at 84000, 84100, 84200, ..., 84900
        # and sells at same levels — creating a regular price ladder
        for i in range(10):
            price = Decimal(str(84000 + i * 100))
            det.record_trade(side="buy", qty=Decimal("0.01"), price=price)
            clock.advance(5.0)

        for i in range(10):
            price = Decimal(str(84000 + i * 100))
            det.record_trade(side="sell", qty=Decimal("0.01"), price=price)
            clock.advance(5.0)

        report = det.analyze()
        assert report.grid_bot.detected
        assert report.grid_bot.confidence > 0.3
        assert report.grid_bot.num_grid_clusters >= 3

    def test_no_grid_in_random_trades(self) -> None:
        """Random prices should not trigger grid detection."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        import random
        rng = random.Random(42)
        for _ in range(50):
            price = Decimal(str(rng.uniform(83000, 87000)))
            side = rng.choice(["buy", "sell"])
            qty = Decimal(str(round(rng.uniform(0.001, 0.5), 4)))
            det.record_trade(side=side, qty=qty, price=price)
            clock.advance(rng.uniform(0.5, 10.0))

        report = det.analyze()
        # Should have low confidence (random trades don't form grids)
        assert report.grid_bot.confidence < 0.5

    def test_geometric_grid_detection(self) -> None:
        """Trades at geometric intervals (1% spacing) should be detected."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        price = Decimal("80000")
        for _ in range(15):
            det.record_trade(side="buy", qty=Decimal("0.01"), price=price)
            price = price * Decimal("1.01")  # 1% geometric spacing
            clock.advance(3.0)

        report = det.analyze()
        assert report.grid_bot.detected
        assert report.grid_bot.geometric


# ---------------------------------------------------------------------------
# TWAP bot detection
# ---------------------------------------------------------------------------


class TestTWAPBotDetection:
    def test_detects_regular_interval_trades(self) -> None:
        """Trades at exact 10s intervals with consistent sizes = TWAP."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        for i in range(30):
            price = Decimal("85000") + Decimal(str(i))  # Tiny drift
            det.record_trade(side="buy", qty=Decimal("0.010"), price=price)
            clock.advance(10.0)

        report = det.analyze()
        assert report.twap_bot.detected
        assert report.twap_bot.dominant_interval_sec == pytest.approx(10.0, abs=1)
        assert report.twap_bot.size_cv < 0.1  # Very consistent sizes

    def test_no_twap_with_irregular_intervals(self) -> None:
        """Irregular intervals and varied sizes should not trigger TWAP."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        import random
        rng = random.Random(99)
        for _ in range(30):
            price = Decimal(str(85000 + rng.randint(-100, 100)))
            qty = Decimal(str(round(rng.uniform(0.001, 1.0), 4)))
            det.record_trade(side="buy", qty=qty, price=price)
            clock.advance(rng.uniform(0.5, 60.0))

        report = det.analyze()
        assert report.twap_bot.confidence < 0.3


# ---------------------------------------------------------------------------
# Iceberg detection
# ---------------------------------------------------------------------------


class TestIcebergDetection:
    def test_detects_iceberg_fills(self) -> None:
        """Repeated fills at same price with consistent size = iceberg."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        # 20 fills at the same price, same clip size
        for _ in range(20):
            det.record_trade(
                side="buy", qty=Decimal("0.050"), price=Decimal("85000.0"),
            )
            clock.advance(2.0)

        # Some noise at other prices
        for i in range(10):
            det.record_trade(
                side="sell",
                qty=Decimal(str(round(0.01 + i * 0.005, 3))),
                price=Decimal(str(85100 + i * 10)),
            )
            clock.advance(1.0)

        report = det.analyze()
        assert report.iceberg.detected
        assert report.iceberg.num_iceberg_levels >= 1
        assert report.iceberg.avg_clip_size == pytest.approx(0.05, abs=0.01)

    def test_no_iceberg_with_varied_sizes(self) -> None:
        """Varied sizes at same price should not trigger iceberg."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        import random
        rng = random.Random(77)
        for _ in range(20):
            qty = Decimal(str(round(rng.uniform(0.001, 2.0), 4)))
            det.record_trade(side="buy", qty=qty, price=Decimal("85000"))
            clock.advance(1.0)

        report = det.analyze()
        # High CV means not iceberg
        assert not report.iceberg.detected


# ---------------------------------------------------------------------------
# Market maker detection
# ---------------------------------------------------------------------------


class TestMarketMakerDetection:
    def test_detects_alternating_buysell(self) -> None:
        """Rapid alternating buy-sell at tight spread = market maker."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        for _ in range(50):
            # Buy at bid
            det.record_trade(
                side="buy", qty=Decimal("0.01"), price=Decimal("84999"),
            )
            clock.advance(0.5)
            # Sell at ask (2 bps spread)
            det.record_trade(
                side="sell", qty=Decimal("0.01"), price=Decimal("85001"),
            )
            clock.advance(0.5)

        report = det.analyze()
        assert report.market_maker.detected
        assert report.market_maker.alternation_ratio > 0.8
        assert report.market_maker.avg_spread_bps < 5.0

    def test_no_mm_with_same_side_runs(self) -> None:
        """Long runs of same side should not trigger MM detection."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        for _ in range(50):
            det.record_trade(
                side="buy", qty=Decimal("0.01"), price=Decimal("85000"),
            )
            clock.advance(1.0)

        report = det.analyze()
        assert not report.market_maker.detected


# ---------------------------------------------------------------------------
# Momentum bot detection
# ---------------------------------------------------------------------------


class TestMomentumBotDetection:
    def test_detects_long_buy_run(self) -> None:
        """20 consecutive buy trades should trigger momentum detection."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        # Normal mixed trading
        for _ in range(10):
            det.record_trade(
                side="buy", qty=Decimal("0.01"), price=Decimal("85000"),
            )
            clock.advance(1.0)
            det.record_trade(
                side="sell", qty=Decimal("0.01"), price=Decimal("85000"),
            )
            clock.advance(1.0)

        # Momentum burst: 20 consecutive buys
        for i in range(20):
            det.record_trade(
                side="buy",
                qty=Decimal("0.02"),
                price=Decimal(str(85000 + i * 5)),
            )
            clock.advance(0.5)

        report = det.analyze()
        assert report.momentum_bot.detected
        assert report.momentum_bot.max_consecutive_same_side >= 20
        assert report.momentum_bot.burst_count >= 1


# ---------------------------------------------------------------------------
# Full analysis report
# ---------------------------------------------------------------------------


class TestFullAnalysis:
    def test_report_with_few_trades(self) -> None:
        """Report with < 5 trades should return safely with no detections."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        det.record_trade(side="buy", qty=Decimal("0.01"), price=Decimal("85000"))
        clock.advance(1.0)
        det.record_trade(side="sell", qty=Decimal("0.01"), price=Decimal("85001"))

        report = det.analyze()
        assert report.trade_count == 2
        assert not report.grid_bot.detected
        assert not report.twap_bot.detected

    def test_summary_output(self) -> None:
        """Summary should produce readable output."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        for i in range(20):
            det.record_trade(
                side="buy", qty=Decimal("0.01"), price=Decimal(str(85000 + i)),
            )
            clock.advance(2.0)

        report = det.analyze()
        summary = report.summary()
        assert "Kraken Bot Activity Analysis" in summary
        assert "Trades:" in summary

    def test_window_prunes_old_trades(self) -> None:
        """Trades older than window should be pruned."""
        clock = _FakeClock()
        det = BotDetector(window_sec=60, clock=clock)

        # Add trades
        for _ in range(10):
            det.record_trade(
                side="buy", qty=Decimal("0.01"), price=Decimal("85000"),
            )
            clock.advance(1.0)

        assert det.trade_count == 10

        # Advance past window
        clock.advance(100.0)

        report = det.analyze()
        assert report.trade_count == 0

    def test_estimated_bot_pct_range(self) -> None:
        """Bot percentage should be within [0, 95]."""
        clock = _FakeClock()
        det = BotDetector(window_sec=600, clock=clock)

        for i in range(50):
            det.record_trade(
                side="buy", qty=Decimal("0.01"),
                price=Decimal(str(85000 + i * 100)),
            )
            clock.advance(10.0)
            det.record_trade(
                side="sell", qty=Decimal("0.01"),
                price=Decimal(str(85000 + i * 100)),
            )
            clock.advance(10.0)

        report = det.analyze()
        assert 0 <= report.estimated_bot_pct <= 95
