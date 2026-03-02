"""Cross-Exchange Oracle — Binance bookTicker monitor for toxic flow detection.

Kraken is not the primary price-discovery venue for BTC or ETH. Binance
Perpetual Futures and CME lead price discovery. When Binance dumps, HFT
arbitrageurs sweep Kraken resting bids ~50-100ms later.

By monitoring Binance BTCUSDT@bookTicker, the oracle detects when Binance
mid-price drops sharply below the local Kraken mid-price. When divergence
exceeds a configurable threshold (default 15 bps), the strategy loop issues
a preemptive cancel_all on Kraken before the toxic taker flow arrives.

This is defensive only — it does not place orders on Binance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal

logger = logging.getLogger(__name__)

# Default Binance WS stream endpoint for book ticker
_BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"

# Default divergence threshold in bps to trigger preemptive cancel
_DEFAULT_DIVERGENCE_THRESHOLD_BPS = 15.0

# Data older than this is considered stale and ignored
_DEFAULT_STALE_SEC = 5.0


class CrossExchangeOracle:
    """Monitors Binance BTCUSDT for cross-exchange divergence.

    Connects to Binance's lightweight bookTicker stream (best bid/ask only,
    no full L2 book) which updates on every top-of-book change — typically
    multiple times per second for BTCUSDT.

    Usage:
        oracle = CrossExchangeOracle()
        task = asyncio.create_task(oracle.run())

        # In strategy loop tick:
        if oracle.should_preemptive_cancel(kraken_mid):
            commands.append(cancel_all_command)
    """

    def __init__(
        self,
        ws_url: str = _BINANCE_WS_URL,
        divergence_threshold_bps: float = _DEFAULT_DIVERGENCE_THRESHOLD_BPS,
        stale_threshold_sec: float = _DEFAULT_STALE_SEC,
        clock: object | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._divergence_threshold_bps = divergence_threshold_bps
        self._stale_threshold_sec = stale_threshold_sec
        self._clock = clock

        # Binance state
        self._binance_bid: Decimal = Decimal("0")
        self._binance_ask: Decimal = Decimal("0")
        self._binance_mid: Decimal = Decimal("0")
        self._last_update_ts: float = 0.0
        self._running = False

        # Metrics
        self.updates_received: int = 0
        self.cancel_signals: int = 0
        self.reconnects: int = 0

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()  # type: ignore[operator]
        return time.monotonic()

    @property
    def binance_mid(self) -> Decimal:
        """Current Binance BTCUSDT mid-price."""
        return self._binance_mid

    @property
    def is_stale(self) -> bool:
        """True if Binance data hasn't been received recently."""
        if self._last_update_ts == 0:
            return True
        return (self._now() - self._last_update_ts) > self._stale_threshold_sec

    def divergence_bps(self, kraken_mid: Decimal) -> float:
        """Compute divergence between Binance and Kraken mid-price in bps.

        Returns:
            Signed bps: negative = Binance is lower (bearish leading signal).
        """
        if self._binance_mid <= 0 or kraken_mid <= 0:
            return 0.0
        return float((self._binance_mid - kraken_mid) / kraken_mid) * 10000

    def should_preemptive_cancel(self, kraken_mid: Decimal) -> bool:
        """Check if Binance divergence warrants a preemptive cancel on Kraken.

        Returns True if Binance mid has dropped sharply below Kraken mid,
        indicating imminent toxic taker flow on Kraken bids.

        Only triggers on downward divergence (Binance leading lower).
        Stale data is ignored to prevent false positives during connectivity
        issues.
        """
        if self.is_stale:
            return False  # Don't act on stale data

        div = self.divergence_bps(kraken_mid)
        # Negative divergence = Binance is lower (bearish signal)
        if div < -self._divergence_threshold_bps:
            self.cancel_signals += 1
            logger.warning(
                "Cross-exchange divergence: Binance mid %.2f vs Kraken %.2f "
                "(%.1f bps) — preemptive cancel signal #%d",
                self._binance_mid, kraken_mid, div, self.cancel_signals,
            )
            return True
        return False

    def update(self, bid: Decimal, ask: Decimal) -> None:
        """Manually update Binance price (for testing or REST fallback)."""
        self._binance_bid = bid
        self._binance_ask = ask
        self._binance_mid = (bid + ask) / 2
        self._last_update_ts = self._now()
        self.updates_received += 1

    async def run(self) -> None:
        """Main loop: connect to Binance WS, track bookTicker, auto-reconnect."""
        try:
            import websockets
            import websockets.asyncio.client as ws_client
        except ImportError:
            logger.warning(
                "websockets not available for Binance oracle; "
                "cross-exchange protection disabled",
            )
            return

        self._running = True
        backoff = [0.0, 1.0, 2.0, 5.0, 10.0, 30.0]
        attempt = 0

        while self._running:
            try:
                async with ws_client.connect(
                    self._ws_url,
                    max_size=2**20,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    attempt = 0
                    logger.info("Binance oracle connected to %s", self._ws_url)

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw_msg)
                            # Binance bookTicker format:
                            # {"u":id, "s":"BTCUSDT", "b":"bid", "B":"bidQty",
                            #  "a":"ask", "A":"askQty"}
                            bid = Decimal(data["b"])
                            ask = Decimal(data["a"])
                            self._binance_bid = bid
                            self._binance_ask = ask
                            self._binance_mid = (bid + ask) / 2
                            self._last_update_ts = self._now()
                            self.updates_received += 1
                        except (KeyError, ValueError):
                            logger.debug("Binance oracle: invalid message")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.reconnects += 1
                attempt = min(attempt + 1, len(backoff) - 1)
                wait = backoff[attempt]
                logger.warning(
                    "Binance oracle disconnected: %s (reconnect in %.1fs)",
                    e, wait,
                )
                await asyncio.sleep(wait)

        logger.info("Binance oracle stopped")

    def stop(self) -> None:
        """Signal the oracle to stop."""
        self._running = False
