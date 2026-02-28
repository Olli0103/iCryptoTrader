"""Lifecycle Manager — graceful shutdown, startup reconciliation, reconnect recovery.

Coordinates the bot's lifecycle:
  - SIGTERM/SIGINT handling with clean shutdown sequence
  - Startup reconciliation: load ledger, connect, reconcile orders
  - Reconnect state recovery: reconcile after WS2 reconnect

Shutdown sequence:
  1. Stop strategy loop (no new ticks)
  2. Cancel all open orders via WS2
  3. Disarm dead man's switch
  4. Save FIFO ledger
  5. Close WS connections
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from icryptotrader.order.order_manager import OrderManager
    from icryptotrader.strategy.strategy_loop import StrategyLoop
    from icryptotrader.ws.ws_private import WSPrivate
    from icryptotrader.ws.ws_public import WSPublicFeed

logger = logging.getLogger(__name__)


class LifecycleManager:
    """Manages bot startup, shutdown, and reconnect recovery.

    Usage:
        lm = LifecycleManager(
            strategy_loop=strategy_loop,
            ws_private=ws2,
            ws_public=ws1,
            order_manager=order_manager,
        )
        lm.install_signal_handlers()
        await lm.startup()
        # ... run main loop ...
        await lm.shutdown()
    """

    def __init__(
        self,
        strategy_loop: StrategyLoop,
        ws_private: WSPrivate,
        ws_public: WSPublicFeed | None = None,
        order_manager: OrderManager | None = None,
    ) -> None:
        self._strategy = strategy_loop
        self._ws2 = ws_private
        self._ws1 = ws_public
        self._om = order_manager

        self._shutting_down = False
        self._shutdown_event = asyncio.Event()
        self._shutdown_complete = asyncio.Event()

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def shutdown_event(self) -> asyncio.Event:
        """Fires when shutdown is requested. Main loop should check this."""
        return self._shutdown_event

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Install SIGTERM and SIGINT handlers for graceful shutdown."""
        ev_loop = loop or asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            ev_loop.add_signal_handler(sig, self._on_signal, sig)
        logger.info("Signal handlers installed (SIGTERM, SIGINT)")

    def _on_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal."""
        if self._shutting_down:
            logger.warning("Received %s again during shutdown, forcing exit", sig.name)
            return
        logger.info("Received %s, initiating graceful shutdown", sig.name)
        self._shutting_down = True
        self._shutdown_event.set()
        asyncio.ensure_future(self.shutdown())

    # --- Startup ---

    async def startup(self) -> None:
        """Run startup sequence: load ledger, wait for WS connections.

        Call after creating all components but before starting the tick loop.
        """
        logger.info("Starting bot lifecycle...")

        # 1. Load ledger from disk
        self._strategy.load_ledger()
        logger.info("Ledger loaded")

        # 2. Wait for WS2 to connect and subscribe
        connected = await self._ws2.wait_connected(timeout=30.0)
        if not connected:
            logger.error("WS2 failed to connect within 30s")
            return

        logger.info("WS2 connected, startup complete")

    async def reconcile_after_reconnect(
        self,
        open_orders: list[dict[str, Any]],
        recent_trades: list[dict[str, Any]],
    ) -> None:
        """Reconcile state after WS2 reconnect.

        Called when WS2 reconnects and provides execution snapshots.
        Matches local order slots to exchange state and cancels orphans.
        """
        if self._om is None:
            logger.warning("No OrderManager configured, skipping reconciliation")
            return

        logger.info(
            "Reconciling: %d open orders, %d recent trades from snapshot",
            len(open_orders), len(recent_trades),
        )

        # Reconcile order slots against exchange snapshot
        orphan_ids = self._om.reconcile_snapshot(open_orders, recent_trades)

        # Cancel orphan orders (orders on exchange not in local state)
        for order_id in orphan_ids:
            logger.warning("Cancelling orphan order: %s", order_id)
            await self._ws2.send_cancel_order(order_id)

        # Save ledger after reconciliation (fills may have been replayed)
        self._strategy.save_ledger()
        logger.info("Reconciliation complete, %d orphans cancelled", len(orphan_ids))

    # --- Shutdown ---

    async def shutdown(self) -> None:
        """Execute clean shutdown sequence.

        1. Stop strategy loop (prevent new ticks)
        2. Cancel all open orders
        3. Disarm DMS
        4. Save ledger
        5. Close connections
        """
        if self._shutdown_complete.is_set():
            return

        logger.info("Shutdown sequence starting...")
        self._shutting_down = True

        # 1. Cancel all open orders
        await self._cancel_all_orders()

        # 2. Save ledger
        try:
            self._strategy.save_ledger()
            logger.info("Ledger saved")
        except Exception:
            logger.exception("Failed to save ledger during shutdown")

        # 3. Stop WS2 (disarms DMS, closes connection)
        try:
            await self._ws2.stop()
            logger.info("WS2 stopped")
        except Exception:
            logger.exception("Error stopping WS2")

        # 4. Stop WS1 if present
        if self._ws1:
            try:
                await self._ws1.stop()
                logger.info("WS1 stopped")
            except Exception:
                logger.exception("Error stopping WS1")

        self._shutdown_complete.set()
        logger.info("Shutdown complete")

    async def _cancel_all_orders(self) -> None:
        """Cancel all open orders before shutdown."""
        if not self._ws2.is_connected:
            logger.warning("WS2 not connected, cannot cancel orders")
            return

        # Try cancel_all first (single command)
        req_id = await self._ws2.send_cancel_all()
        if req_id is not None:
            logger.info("cancel_all sent (req_id=%d)", req_id)
            # Brief wait for the exchange to process
            await asyncio.sleep(0.5)
        else:
            logger.warning("cancel_all failed to send")
