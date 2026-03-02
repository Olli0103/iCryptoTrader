"""Trade Flow Imbalance (TFI) — executed taker-side volume tracker.

Replaces naive L2 Order Book Imbalance (OBI) which is trivially spoofable
(phantom bids/asks can be placed and cancelled faster than our 100ms tick).

TFI tracks actually executed trades from the public trade stream, comparing
taker-buy volume to taker-sell volume over a rolling time window.  Unlike
resting L2 liquidity, executed trades cannot be spoofed.

Output: TFI in [-1, 1] range, same convention as OBI:
  - Positive = net taker buy pressure (bullish)
  - Negative = net taker sell pressure (bearish)
  - 0 = balanced flow
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal


@dataclass(slots=True)
class TradeRecord:
    """A single public trade record."""

    timestamp: float  # monotonic time of receipt
    side: str  # "buy" or "sell" (taker side)
    qty: Decimal
    price: Decimal


class TradeFlowImbalance:
    """Computes TFI from a rolling window of executed public trades.

    Uses an exponentially-weighted scheme where recent trades carry more
    weight than older ones, preventing stale fills from 59 seconds ago
    from dominating the signal.

    Usage:
        tfi = TradeFlowImbalance(window_sec=60.0)
        tfi.record_trade(side="buy", qty=Decimal("0.01"), price=Decimal("85000"))
        signal = tfi.compute()  # Returns float in [-1, 1]
    """

    def __init__(
        self,
        window_sec: float = 60.0,
        half_life_sec: float = 15.0,
        clock: object | None = None,
    ) -> None:
        self._window_sec = window_sec
        self._half_life_sec = half_life_sec
        self._clock = clock  # Injectable clock for testing
        self._trades: deque[TradeRecord] = deque(maxlen=5000)

        # Metrics
        self.trades_recorded: int = 0

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()  # type: ignore[operator]
        return time.monotonic()

    def record_trade(
        self,
        side: str,
        qty: Decimal,
        price: Decimal,
    ) -> None:
        """Record a public trade from the Kraken trade channel.

        Args:
            side: "buy" or "sell" (taker side from Kraken WS v2).
            qty: Trade quantity in base currency (BTC).
            price: Trade price.
        """
        self._trades.append(TradeRecord(
            timestamp=self._now(),
            side=side.lower(),
            qty=qty,
            price=price,
        ))
        self.trades_recorded += 1

    def compute(self) -> float:
        """Compute Trade Flow Imbalance over the rolling window.

        Uses exponential decay weighting: weight = 2^(-age / half_life).
        Recent trades dominate; trades older than window_sec are pruned.

        Returns:
            TFI in [-1, 1]. Positive = net taker buy pressure.
        """
        now = self._now()
        cutoff = now - self._window_sec
        ln2 = 0.6931471805599453  # ln(2)
        decay_rate = ln2 / self._half_life_sec

        buy_volume = 0.0
        sell_volume = 0.0

        # Prune expired trades from the front
        while self._trades and self._trades[0].timestamp < cutoff:
            self._trades.popleft()

        for trade in self._trades:
            age = now - trade.timestamp
            weight = 2.0 ** (-age / self._half_life_sec)
            weighted_qty = float(trade.qty) * weight

            if trade.side == "buy":
                buy_volume += weighted_qty
            else:
                sell_volume += weighted_qty

        total = buy_volume + sell_volume
        if total == 0.0:
            return 0.0

        return (buy_volume - sell_volume) / total

    def raw_volumes(self) -> tuple[float, float]:
        """Return raw (buy_volume, sell_volume) for diagnostics."""
        now = self._now()
        cutoff = now - self._window_sec

        buy_volume = 0.0
        sell_volume = 0.0

        for trade in self._trades:
            if trade.timestamp < cutoff:
                continue
            age = now - trade.timestamp
            weight = 2.0 ** (-age / self._half_life_sec)
            weighted_qty = float(trade.qty) * weight
            if trade.side == "buy":
                buy_volume += weighted_qty
            else:
                sell_volume += weighted_qty

        return buy_volume, sell_volume

    @property
    def trade_count(self) -> int:
        """Number of trades currently in the window."""
        return len(self._trades)
