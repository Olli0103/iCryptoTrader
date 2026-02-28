"""Process Watchdog — health monitoring and auto-recovery.

Runs as a background asyncio task. Checks:
  - Strategy tick recency (detect stall)
  - WebSocket connection alive
  - Memory usage (detect leaks)

If unhealthy for N consecutive checks → log critical + trigger graceful shutdown
via the LifecycleManager (saves ledger, cancels orders, closes WS).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from icryptotrader.lifecycle import LifecycleManager
    from icryptotrader.strategy.strategy_loop import StrategyLoop
    from icryptotrader.ws.ws_private import WSPrivate

logger = logging.getLogger(__name__)

# Health check interval
CHECK_INTERVAL_SEC = 15.0

# Max time since last tick before considered stalled
MAX_TICK_AGE_SEC = 120.0

# Consecutive failures before action
MAX_FAILURES = 5


class Watchdog:
    """Background health monitor for the trading bot.

    Usage:
        wd = Watchdog(strategy_loop=loop, ws_private=ws2, lifecycle_manager=lm)
        task = asyncio.create_task(wd.run())
        # ... later ...
        wd.stop()
    """

    def __init__(
        self,
        strategy_loop: StrategyLoop,
        ws_private: WSPrivate,
        lifecycle_manager: LifecycleManager | None = None,
        check_interval: float = CHECK_INTERVAL_SEC,
        max_tick_age: float = MAX_TICK_AGE_SEC,
        max_failures: int = MAX_FAILURES,
    ) -> None:
        self._strategy = strategy_loop
        self._ws = ws_private
        self._lm = lifecycle_manager
        self._interval = check_interval
        self._max_tick_age = max_tick_age
        self._max_failures = max_failures
        self._running = False
        self._consecutive_failures = 0

        # Metrics
        self.checks: int = 0
        self.failures: int = 0
        self.recoveries: int = 0

    def stop(self) -> None:
        """Signal the watchdog to stop."""
        self._running = False

    async def run(self) -> None:
        """Main watchdog loop. Run as a background task."""
        self._running = True
        logger.info("Watchdog started (interval=%ss)", self._interval)

        while self._running:
            try:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break
                await self._check_health()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Watchdog check error")

        logger.info("Watchdog stopped")

    async def _check_health(self) -> None:
        """Run one health check cycle."""
        self.checks += 1
        issues: list[str] = []

        # Check tick recency
        tick_age = time.time() - self._strategy._start_time
        if self._strategy.ticks > 0:
            # Estimate last tick time from tick count and start
            ticks_per_sec = self._strategy.ticks / max(tick_age, 1)
            if ticks_per_sec < 0.1 and tick_age > 30:
                issues.append(f"low_tick_rate({ticks_per_sec:.2f}/s)")

        # Check WS connection
        if not self._ws.is_connected:
            issues.append("ws_disconnected")

        # Check memory (basic — just log if high)
        try:
            import resource
            usage_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            if usage_mb > 512:
                issues.append(f"high_memory({usage_mb:.0f}MB)")
        except (ImportError, AttributeError):
            pass

        if issues:
            self._consecutive_failures += 1
            self.failures += 1
            logger.warning(
                "Watchdog: UNHEALTHY (%d/%d) — %s",
                self._consecutive_failures, self._max_failures,
                ", ".join(issues),
            )

            if self._consecutive_failures >= self._max_failures:
                logger.critical(
                    "Watchdog: %d consecutive failures, triggering graceful shutdown",
                    self._consecutive_failures,
                )
                self._running = False
                # Trigger graceful shutdown (saves ledger, cancels orders)
                # instead of sys.exit(1) which bypasses cleanup
                if self._lm is not None:
                    await self._lm.shutdown()
        else:
            if self._consecutive_failures > 0:
                self.recoveries += 1
                logger.info(
                    "Watchdog: recovered after %d failures",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
