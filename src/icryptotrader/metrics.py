"""Structured Metrics Export — Prometheus-compatible metrics for observability.

Provides a lightweight metrics registry that can be scraped by Prometheus
or exported to any monitoring system. No external dependency required —
exposes metrics via a simple HTTP endpoint using asyncio.

Metrics tracked:
  - Tick latency (histogram)
  - Fill count by side
  - Drawdown percentage (gauge)
  - Rate limiter utilization (gauge)
  - Regime distribution (counter)
  - AI signal latency and direction (gauge/counter)
  - FIFO ledger stats (gauge)
  - Order slot states (gauge)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)


class MetricsRegistry:
    """Lightweight Prometheus-compatible metrics registry.

    Supports counters, gauges, and histograms. Thread-safe for single-writer,
    multi-reader patterns (Python GIL provides atomicity for simple ops).
    """

    def __init__(self, prefix: str = "icryptotrader") -> None:
        self._prefix = prefix
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = defaultdict(float)
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._labels: dict[str, dict[str, str]] = {}
        self._start_time = time.time()

    def counter_inc(
        self, name: str, value: float = 1.0, labels: dict[str, str] | None = None,
    ) -> None:
        key = self._make_key(name, labels)
        self._counters[key] += value

    def gauge_set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._make_key(name, labels)
        self._gauges[key] = value

    def histogram_observe(
        self, name: str, value: float, labels: dict[str, str] | None = None,
    ) -> None:
        key = self._make_key(name, labels)
        self._histograms[key].append(value)
        # Keep bounded (last 1000 observations)
        if len(self._histograms[key]) > 1000:
            self._histograms[key] = self._histograms[key][-1000:]

    def _make_key(self, name: str, labels: dict[str, str] | None) -> str:
        key = f"{self._prefix}_{name}"
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            key = f"{key}{{{label_str}}}"
            self._labels[key] = labels
        return key

    def format_prometheus(self) -> str:
        """Format all metrics in Prometheus text exposition format."""
        lines: list[str] = []

        # Counters
        for key, value in sorted(self._counters.items()):
            lines.append(f"{key} {value}")

        # Gauges
        for key, value in sorted(self._gauges.items()):
            lines.append(f"{key} {value}")

        # Histograms (emit count, sum, and quantiles)
        for key, values in sorted(self._histograms.items()):
            if not values:
                continue
            count = len(values)
            total = sum(values)
            sorted_vals = sorted(values)
            lines.append(f"{key}_count {count}")
            lines.append(f"{key}_sum {total:.6f}")
            for q in (0.5, 0.9, 0.99):
                idx = int(q * count)
                lines.append(f'{key}{{quantile="{q}"}} {sorted_vals[min(idx, count - 1)]:.6f}')

        # Uptime
        uptime = time.time() - self._start_time
        lines.append(f"{self._prefix}_uptime_seconds {uptime:.1f}")

        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict[str, Any]:
        """Return all metrics as a dict for JSON export."""
        result: dict[str, Any] = {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "uptime_seconds": time.time() - self._start_time,
        }
        for key, values in self._histograms.items():
            if values:
                result.setdefault("histograms", {})[key] = {
                    "count": len(values),
                    "sum": sum(values),
                    "p50": sorted(values)[len(values) // 2],
                    "p99": sorted(values)[int(len(values) * 0.99)],
                }
        return result


class MetricsServer:
    """Simple async HTTP server for Prometheus scraping."""

    def __init__(self, registry: MetricsRegistry, port: int = 9090) -> None:
        self._registry = registry
        self._port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_request, "0.0.0.0", self._port,
        )
        logger.info("Metrics server listening on port %d", self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Metrics server stopped")

    async def _handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readline()  # Read HTTP request line
            body = self._registry.format_prometheus()
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
                f"{body}"
            )
            writer.write(response.encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
