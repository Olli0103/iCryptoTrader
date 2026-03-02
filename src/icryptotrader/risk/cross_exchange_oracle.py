"""Cross-Exchange Oracle — Binance bookTicker monitor for toxic flow detection.

Kraken is not the primary price-discovery venue for BTC or ETH. Binance
Perpetual Futures and CME lead price discovery. When Binance dumps, HFT
arbitrageurs sweep Kraken resting bids ~50-100ms later.

By monitoring Binance BTCUSDT@bookTicker, the oracle detects when Binance
mid-price drops sharply below the local Kraken mid-price. When divergence
exceeds a dynamically-scaled threshold, the strategy loop issues a preemptive
cancel_all on Kraken before the toxic taker flow arrives.

Key design features:
  - Lead-lag correlation: rolling Pearson ρ between Binance and Kraken mid
    dynamically scales the trigger threshold. When ρ is high (Binance leads
    Kraken closely), a smaller divergence is sufficient to trigger cancel.
    Formula: effective_threshold = base_bps / max(0.1, ρ)
  - Dead-man's switch: if Binance data is older than 1.5s, returns
    STATE_UNKNOWN which forces the strategy loop to widen spreads 3x until
    the feed is re-established. This is critical because Binance WS is
    notoriously unstable during volatility spikes — exactly when defense
    matters most.
  - Auto-reconnect with exponential backoff.

This is defensive only — it does not place orders on Binance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto

logger = logging.getLogger(__name__)

# Default Binance WS stream endpoint for book ticker
_BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"

# Default divergence threshold in bps to trigger preemptive cancel
_DEFAULT_DIVERGENCE_THRESHOLD_BPS = 15.0

# Dead-man's switch: data older than this triggers STATE_UNKNOWN
_DEADMAN_STALE_SEC = 1.5

# Permissive stale threshold for general "is data usable" checks
_GENERAL_STALE_SEC = 5.0

# Rolling window size for Pearson correlation (number of samples)
_CORRELATION_WINDOW = 60

# Minimum ρ clamp to prevent division by near-zero correlation
_MIN_RHO_CLAMP = 0.1

# Spread multiplier when oracle is in STATE_UNKNOWN (dead-man's switch)
UNKNOWN_SPREAD_MULTIPLIER = Decimal("3")


class OracleState(Enum):
    """Oracle feed health state."""

    HEALTHY = auto()  # Fresh data, Binance feed flowing
    DIVERGENCE = auto()  # Binance diverging from Kraken (cancel signal)
    UNKNOWN = auto()  # Dead-man's switch: stale data, widen spreads 3x


@dataclass(frozen=True)
class OracleAssessment:
    """Result of a single oracle tick assessment."""

    state: OracleState
    divergence_bps: float  # Signed; negative = Binance lower
    effective_threshold_bps: float  # Dynamic threshold (base / ρ)
    correlation_rho: float  # Rolling Pearson ρ
    should_cancel: bool  # True → issue cancel_all
    spread_multiplier: Decimal  # 1 = normal, 3 = unknown state


class CrossExchangeOracle:
    """Monitors Binance BTCUSDT for cross-exchange divergence.

    Connects to Binance's lightweight bookTicker stream (best bid/ask only,
    no full L2 book) which updates on every top-of-book change — typically
    multiple times per second for BTCUSDT.

    Usage:
        oracle = CrossExchangeOracle()
        task = asyncio.create_task(oracle.run())

        # In strategy loop tick:
        assessment = oracle.assess(kraken_mid)
        if assessment.should_cancel:
            commands.append(cancel_all_command)
        if assessment.spread_multiplier > 1:
            buy_spacing *= assessment.spread_multiplier
    """

    def __init__(
        self,
        ws_url: str = _BINANCE_WS_URL,
        divergence_threshold_bps: float = _DEFAULT_DIVERGENCE_THRESHOLD_BPS,
        deadman_stale_sec: float = _DEADMAN_STALE_SEC,
        correlation_window: int = _CORRELATION_WINDOW,
        clock: object | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._base_threshold_bps = divergence_threshold_bps
        self._deadman_stale_sec = deadman_stale_sec
        self._clock = clock

        # Binance state
        self._binance_bid: Decimal = Decimal("0")
        self._binance_ask: Decimal = Decimal("0")
        self._binance_mid: Decimal = Decimal("0")
        self._last_update_ts: float = 0.0
        self._running = False

        # Lead-lag correlation: rolling paired samples of (binance_mid, kraken_mid)
        # recorded each time assess() is called with valid data.
        self._correlation_window = correlation_window
        self._paired_samples: deque[tuple[float, float]] = deque(
            maxlen=correlation_window,
        )

        # Metrics
        self.updates_received: int = 0
        self.cancel_signals: int = 0
        self.reconnects: int = 0
        self.deadman_triggers: int = 0

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
        """True if Binance data hasn't been received recently (general)."""
        if self._last_update_ts == 0:
            return True
        return (self._now() - self._last_update_ts) > _GENERAL_STALE_SEC

    @property
    def is_deadman_stale(self) -> bool:
        """True if data is stale beyond the dead-man's switch threshold.

        This is a tighter check than is_stale. When triggered, the oracle
        returns STATE_UNKNOWN and forces 3x spread widening.
        """
        if self._last_update_ts == 0:
            return True
        return (self._now() - self._last_update_ts) > self._deadman_stale_sec

    def divergence_bps(self, kraken_mid: Decimal) -> float:
        """Compute divergence between Binance and Kraken mid-price in bps.

        Returns:
            Signed bps: negative = Binance is lower (bearish leading signal).
        """
        if self._binance_mid <= 0 or kraken_mid <= 0:
            return 0.0
        return float((self._binance_mid - kraken_mid) / kraken_mid) * 10000

    def correlation(self) -> float:
        """Rolling Pearson correlation between Binance and Kraken mid-prices.

        Uses the paired samples collected during assess() calls. Returns 0.0
        if insufficient data (<3 samples).
        """
        n = len(self._paired_samples)
        if n < 3:
            return 0.0

        sum_x = sum_y = sum_xy = sum_x2 = sum_y2 = 0.0
        for bx, kx in self._paired_samples:
            sum_x += bx
            sum_y += kx
            sum_xy += bx * kx
            sum_x2 += bx * bx
            sum_y2 += kx * kx

        denom_x = n * sum_x2 - sum_x * sum_x
        denom_y = n * sum_y2 - sum_y * sum_y

        if denom_x <= 0 or denom_y <= 0:
            return 0.0

        rho = (n * sum_xy - sum_x * sum_y) / math.sqrt(denom_x * denom_y)
        return max(-1.0, min(1.0, rho))

    def effective_threshold_bps(self) -> float:
        """Compute dynamic trigger threshold scaled by lead-lag correlation.

        When ρ is high (Binance and Kraken move together closely), the oracle
        is confident that Binance divergence is meaningful → lower threshold.
        When ρ is low, divergence is noise → higher threshold to avoid
        false positives.

        With insufficient data (<3 paired samples), returns the base threshold
        unchanged as a conservative default.

        Formula: base_bps / max(0.1, ρ)
        """
        if len(self._paired_samples) < 3:
            return self._base_threshold_bps
        rho = self.correlation()
        clamped_rho = max(_MIN_RHO_CLAMP, abs(rho))
        return self._base_threshold_bps / clamped_rho

    def assess(self, kraken_mid: Decimal) -> OracleAssessment:
        """Full oracle assessment combining all signals.

        This replaces the old should_preemptive_cancel() with a richer
        return type that includes the dead-man's switch state.

        Returns OracleAssessment with:
          - state: HEALTHY / DIVERGENCE / UNKNOWN
          - should_cancel: True if cancel_all should be issued
          - spread_multiplier: 1 (normal) or 3 (unknown state)
        """
        # Dead-man's switch: if Binance feed is stale beyond 1.5s,
        # we cannot trust the defense shield. Force spread widening.
        if self.is_deadman_stale:
            self.deadman_triggers += 1
            return OracleAssessment(
                state=OracleState.UNKNOWN,
                divergence_bps=0.0,
                effective_threshold_bps=self.effective_threshold_bps(),
                correlation_rho=self.correlation(),
                should_cancel=False,
                spread_multiplier=UNKNOWN_SPREAD_MULTIPLIER,
            )

        # Record paired sample for correlation tracking
        if self._binance_mid > 0 and kraken_mid > 0:
            self._paired_samples.append(
                (float(self._binance_mid), float(kraken_mid)),
            )

        div = self.divergence_bps(kraken_mid)
        rho = self.correlation()
        threshold = self.effective_threshold_bps()

        # Negative divergence = Binance is lower (bearish signal)
        if div < -threshold:
            self.cancel_signals += 1
            logger.warning(
                "Cross-exchange divergence: Binance mid %.2f vs Kraken %.2f "
                "(%.1f bps, threshold=%.1f bps, ρ=%.3f) — cancel signal #%d",
                self._binance_mid, kraken_mid, div, threshold,
                rho, self.cancel_signals,
            )
            return OracleAssessment(
                state=OracleState.DIVERGENCE,
                divergence_bps=div,
                effective_threshold_bps=threshold,
                correlation_rho=rho,
                should_cancel=True,
                spread_multiplier=Decimal("1"),
            )

        return OracleAssessment(
            state=OracleState.HEALTHY,
            divergence_bps=div,
            effective_threshold_bps=threshold,
            correlation_rho=rho,
            should_cancel=False,
            spread_multiplier=Decimal("1"),
        )

    # -- Legacy compatibility --

    def should_preemptive_cancel(self, kraken_mid: Decimal) -> bool:
        """Legacy API: check if divergence warrants cancel.

        Prefer assess() for full state information including dead-man's switch.
        """
        assessment = self.assess(kraken_mid)
        return assessment.should_cancel

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
                    ping_interval=20,
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
                base_wait = backoff[attempt]
                # Full jitter: randomize [0, base_wait] to prevent
                # thundering herd when many oracle instances reconnect
                # simultaneously after a Binance WS outage.
                wait = random.uniform(0, base_wait) if base_wait > 0 else 0.0  # noqa: S311
                logger.warning(
                    "Binance oracle disconnected: %s (reconnect in %.1fs, base=%.1fs)",
                    e, wait, base_wait,
                )
                await asyncio.sleep(wait)

        logger.info("Binance oracle stopped")

    def stop(self) -> None:
        """Signal the oracle to stop."""
        self._running = False
