"""Tests for the Process Watchdog."""

from __future__ import annotations

from unittest.mock import MagicMock

from icryptotrader.watchdog import Watchdog


def _make_watchdog(
    ticks: int = 100,
    ws_connected: bool = True,
) -> Watchdog:
    """Create a watchdog with mocked dependencies."""
    import time

    strategy = MagicMock()
    strategy.ticks = ticks
    strategy._start_time = time.time() - 10  # Started 10s ago (healthy rate)

    ws = MagicMock()
    ws.is_connected = ws_connected

    return Watchdog(
        strategy_loop=strategy,
        ws_private=ws,
        max_failures=3,
    )


class TestWatchdog:
    def test_healthy_no_failures(self) -> None:
        wd = _make_watchdog(ticks=1000, ws_connected=True)
        wd._check_health()
        assert wd.failures == 0
        assert wd.checks == 1

    def test_ws_disconnected_counts_failure(self) -> None:
        wd = _make_watchdog(ws_connected=False)
        wd._check_health()
        assert wd.failures == 1
        assert wd._consecutive_failures == 1

    def test_recovery_resets_counter(self) -> None:
        wd = _make_watchdog(ws_connected=False)
        wd._check_health()  # fail
        assert wd._consecutive_failures == 1

        # Fix the issue
        wd._ws.is_connected = True
        wd._strategy.ticks = 1000
        wd._check_health()  # recover
        assert wd._consecutive_failures == 0
        assert wd.recoveries == 1

    def test_stop(self) -> None:
        wd = _make_watchdog()
        assert not wd._running
        wd.stop()
        assert not wd._running

    def test_initial_state(self) -> None:
        wd = _make_watchdog()
        assert wd.checks == 0
        assert wd.failures == 0
        assert wd.recoveries == 0
