"""Tests for the Lifecycle Manager — graceful shutdown and reconciliation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from icryptotrader.lifecycle import LifecycleManager


@pytest.fixture()
def mock_components():
    """Create mock components for lifecycle manager tests."""
    strategy = MagicMock()
    strategy.load_ledger = MagicMock()
    strategy.save_ledger = MagicMock()

    ws2 = AsyncMock()
    ws2.is_connected = True
    ws2.wait_connected = AsyncMock(return_value=True)
    ws2.send_cancel_all = AsyncMock(return_value=1001)
    ws2.send_cancel_order = AsyncMock(return_value=1002)
    ws2.stop = AsyncMock()

    ws1 = AsyncMock()
    ws1.stop = AsyncMock()

    om = MagicMock()
    om.reconcile_snapshot = MagicMock(return_value=[])
    om.live_slots = MagicMock(return_value=[])

    return strategy, ws2, ws1, om


class TestLifecycleManager:
    def test_init(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
            ws_public=ws1, order_manager=om,
        )
        assert not lm.is_shutting_down
        assert not lm.shutdown_event.is_set()


class TestStartup:
    @pytest.mark.asyncio()
    async def test_startup_loads_ledger(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
            ws_public=ws1, order_manager=om,
        )
        await lm.startup()
        strategy.load_ledger.assert_called_once()
        ws2.wait_connected.assert_called_once()

    @pytest.mark.asyncio()
    async def test_startup_handles_ws2_timeout(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        ws2.wait_connected = AsyncMock(return_value=False)
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        await lm.startup()
        strategy.load_ledger.assert_called_once()


class TestShutdown:
    @pytest.mark.asyncio()
    async def test_shutdown_cancels_orders(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
            ws_public=ws1, order_manager=om,
        )
        await lm.shutdown()
        ws2.send_cancel_all.assert_called_once()

    @pytest.mark.asyncio()
    async def test_shutdown_saves_ledger(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        await lm.shutdown()
        strategy.save_ledger.assert_called_once()

    @pytest.mark.asyncio()
    async def test_shutdown_stops_ws2(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        await lm.shutdown()
        ws2.stop.assert_called_once()

    @pytest.mark.asyncio()
    async def test_shutdown_stops_ws1(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
            ws_public=ws1,
        )
        await lm.shutdown()
        ws1.stop.assert_called_once()

    @pytest.mark.asyncio()
    async def test_shutdown_without_ws1(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        await lm.shutdown()
        # Should not crash without ws1
        ws2.stop.assert_called_once()

    @pytest.mark.asyncio()
    async def test_shutdown_sets_flag(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        assert not lm.is_shutting_down
        await lm.shutdown()
        assert lm.is_shutting_down

    @pytest.mark.asyncio()
    async def test_double_shutdown_is_noop(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        await lm.shutdown()
        await lm.shutdown()  # Second call should be a no-op
        ws2.stop.assert_called_once()

    @pytest.mark.asyncio()
    async def test_shutdown_when_disconnected(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        ws2.is_connected = False
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        await lm.shutdown()
        ws2.send_cancel_all.assert_not_called()

    @pytest.mark.asyncio()
    async def test_shutdown_handles_save_error(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        strategy.save_ledger.side_effect = OSError("disk full")
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        await lm.shutdown()  # Should not raise
        ws2.stop.assert_called_once()


class TestReconciliation:
    @pytest.mark.asyncio()
    async def test_reconcile_calls_om(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
            order_manager=om,
        )
        orders = [{"order_id": "O1", "limit_price": "85000", "order_qty": "0.01"}]
        trades = [{"trade_id": "T1"}]
        await lm.reconcile_after_reconnect(orders, trades)
        om.reconcile_snapshot.assert_called_once_with(orders, trades)
        strategy.save_ledger.assert_called_once()

    @pytest.mark.asyncio()
    async def test_reconcile_cancels_orphans(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        om.reconcile_snapshot.return_value = ["ORPHAN1", "ORPHAN2"]
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
            order_manager=om,
        )
        await lm.reconcile_after_reconnect([], [])
        assert ws2.send_cancel_order.call_count == 2

    @pytest.mark.asyncio()
    async def test_reconcile_without_om(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        await lm.reconcile_after_reconnect([], [])  # Should not raise


class TestSignalHandlers:
    def test_install_signal_handlers(self, mock_components):
        strategy, ws2, ws1, om = mock_components
        lm = LifecycleManager(
            strategy_loop=strategy, ws_private=ws2,
        )
        loop = MagicMock()
        lm.install_signal_handlers(loop=loop)
        assert loop.add_signal_handler.call_count == 2
