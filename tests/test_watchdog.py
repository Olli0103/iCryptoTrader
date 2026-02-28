"""Tests for the Process Watchdog."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

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
    @pytest.mark.asyncio
    async def test_healthy_no_failures(self) -> None:
        wd = _make_watchdog(ticks=1000, ws_connected=True)
        await wd._check_health()
        assert wd.failures == 0
        assert wd.checks == 1

    @pytest.mark.asyncio
    async def test_ws_disconnected_counts_failure(self) -> None:
        wd = _make_watchdog(ws_connected=False)
        await wd._check_health()
        assert wd.failures == 1
        assert wd._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_recovery_resets_counter(self) -> None:
        wd = _make_watchdog(ws_connected=False)
        await wd._check_health()  # fail
        assert wd._consecutive_failures == 1

        # Fix the issue
        wd._ws.is_connected = True
        wd._strategy.ticks = 1000
        await wd._check_health()  # recover
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

    @pytest.mark.asyncio
    async def test_graceful_shutdown_on_max_failures(self) -> None:
        """After max_failures, watchdog triggers graceful shutdown."""
        lm = MagicMock()
        lm.shutdown = MagicMock(return_value=asyncio.Future())
        lm.shutdown.return_value.set_result(None)

        wd = _make_watchdog(ws_connected=False)
        wd._lm = lm

        for _ in range(3):
            await wd._check_health()

        assert wd._consecutive_failures == 3
        assert not wd._running
        lm.shutdown.assert_called_once()
