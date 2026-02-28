"""Tests for the structured metrics registry."""

from __future__ import annotations

from icryptotrader.metrics import MetricsRegistry


class TestCounter:
    def test_counter_starts_zero(self) -> None:
        reg = MetricsRegistry(prefix="test")
        assert reg._counters.get("test_fills") is None

    def test_counter_increment(self) -> None:
        reg = MetricsRegistry(prefix="test")
        reg.counter_inc("fills")
        reg.counter_inc("fills")
        assert reg._counters["test_fills"] == 2.0

    def test_counter_with_labels(self) -> None:
        reg = MetricsRegistry(prefix="test")
        reg.counter_inc("fills", labels={"side": "buy"})
        reg.counter_inc("fills", labels={"side": "sell"})
        assert reg._counters['test_fills{side="buy"}'] == 1.0
        assert reg._counters['test_fills{side="sell"}'] == 1.0


class TestGauge:
    def test_gauge_set(self) -> None:
        reg = MetricsRegistry(prefix="test")
        reg.gauge_set("drawdown_pct", 0.05)
        assert reg._gauges["test_drawdown_pct"] == 0.05

    def test_gauge_overwrite(self) -> None:
        reg = MetricsRegistry(prefix="test")
        reg.gauge_set("drawdown_pct", 0.05)
        reg.gauge_set("drawdown_pct", 0.10)
        assert reg._gauges["test_drawdown_pct"] == 0.10


class TestHistogram:
    def test_histogram_observe(self) -> None:
        reg = MetricsRegistry(prefix="test")
        for val in [1.0, 2.0, 3.0, 4.0, 5.0]:
            reg.histogram_observe("tick_latency_ms", val)
        assert len(reg._histograms["test_tick_latency_ms"]) == 5

    def test_histogram_bounded(self) -> None:
        reg = MetricsRegistry(prefix="test")
        for i in range(1100):
            reg.histogram_observe("ticks", float(i))
        assert len(reg._histograms["test_ticks"]) == 1000


class TestPrometheusFormat:
    def test_format_counters(self) -> None:
        reg = MetricsRegistry(prefix="bot")
        reg.counter_inc("fills", 5)
        output = reg.format_prometheus()
        assert "bot_fills 5" in output

    def test_format_gauges(self) -> None:
        reg = MetricsRegistry(prefix="bot")
        reg.gauge_set("drawdown", 0.15)
        output = reg.format_prometheus()
        assert "bot_drawdown 0.15" in output

    def test_format_histograms(self) -> None:
        reg = MetricsRegistry(prefix="bot")
        for v in [1.0, 2.0, 3.0]:
            reg.histogram_observe("latency", v)
        output = reg.format_prometheus()
        assert "bot_latency_count 3" in output
        assert "bot_latency_sum" in output
        assert 'quantile="0.5"' in output

    def test_uptime_included(self) -> None:
        reg = MetricsRegistry(prefix="bot")
        output = reg.format_prometheus()
        assert "bot_uptime_seconds" in output


class TestSnapshot:
    def test_snapshot_dict(self) -> None:
        reg = MetricsRegistry(prefix="bot")
        reg.counter_inc("fills", 3)
        reg.gauge_set("dd", 0.1)
        snap = reg.snapshot()
        assert snap["counters"]["bot_fills"] == 3.0
        assert snap["gauges"]["bot_dd"] == 0.1
        assert snap["uptime_seconds"] >= 0
