"""T+X Mark-Out Tracker — post-fill adverse selection measurement.

Records the mid-price at fill time and measures how the market moved
at T+1s, T+10s, T+60s after the fill.  This reveals whether our fills
are being adversely selected (market moves against us immediately after
we get filled, indicating toxic flow or stale quotes).

Adverse selection measured in basis points:
  - For a BUY fill: (fill_price - mid_at_T+X) / fill_price * 10000
    Positive = mid moved down after our buy (we overpaid = adverse)
  - For a SELL fill: (mid_at_T+X - fill_price) / fill_price * 10000
    Positive = mid moved up after our sell (we undersold = adverse)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)

# Mark-out horizons in seconds
MARK_OUT_HORIZONS = (1.0, 10.0, 60.0)


@dataclass(slots=True)
class PendingMarkOut:
    """A fill awaiting mark-out price checks."""

    fill_ts: float  # monotonic timestamp of fill
    fill_price: Decimal
    side: str  # "buy" or "sell"
    qty: Decimal
    mark_outs: dict[float, Decimal] = field(default_factory=dict)
    # Horizons not yet measured
    pending_horizons: list[float] = field(default_factory=list)


@dataclass
class MarkOutStats:
    """Aggregated adverse selection statistics."""

    # Average mark-out in bps per horizon
    avg_adverse_bps: dict[float, float] = field(default_factory=dict)
    # Number of observations per horizon
    observations: dict[float, int] = field(default_factory=dict)
    # Suggested adverse_selection_bps for grid engine calibration
    suggested_adverse_bps: float = 0.0


class MarkOutTracker:
    """Tracks post-fill mid-price movement to measure adverse selection.

    Usage:
        tracker = MarkOutTracker()
        # On fill:
        tracker.record_fill(fill_price, side="buy", qty=qty, mid_price=mid)
        # On every tick (or periodically):
        tracker.check_mark_outs(current_mid=mid)
        # Get stats:
        stats = tracker.stats()
    """

    def __init__(
        self,
        max_pending: int = 200,
        max_completed: int = 1000,
        clock: object | None = None,
    ) -> None:
        self._pending: deque[PendingMarkOut] = deque(maxlen=max_pending)
        self._completed_adverse_bps: dict[float, deque[float]] = {
            h: deque(maxlen=max_completed) for h in MARK_OUT_HORIZONS
        }
        self._clock = clock

        # Metrics
        self.fills_tracked: int = 0
        self.mark_outs_completed: int = 0

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()  # type: ignore[operator]
        return time.monotonic()

    def record_fill(
        self,
        fill_price: Decimal,
        side: str,
        qty: Decimal,
        mid_price: Decimal,
    ) -> None:
        """Record a new fill for mark-out tracking.

        Args:
            fill_price: The fill price.
            side: "buy" or "sell".
            qty: Fill quantity.
            mid_price: Current mid-price at time of fill (for T+0 reference).
        """
        self._pending.append(PendingMarkOut(
            fill_ts=self._now(),
            fill_price=fill_price,
            side=side.lower(),
            qty=qty,
            pending_horizons=list(MARK_OUT_HORIZONS),
        ))
        self.fills_tracked += 1

    def check_mark_outs(self, current_mid: Decimal) -> None:
        """Check all pending fills for completed mark-out horizons.

        Call this on every strategy tick with the current mid-price.
        """
        now = self._now()
        completed_indices: list[int] = []

        for i, pmo in enumerate(self._pending):
            elapsed = now - pmo.fill_ts
            remaining: list[float] = []

            for horizon in pmo.pending_horizons:
                if elapsed >= horizon:
                    # Record this mark-out
                    pmo.mark_outs[horizon] = current_mid
                    adverse_bps = self._compute_adverse_bps(
                        pmo.fill_price, current_mid, pmo.side,
                    )
                    self._completed_adverse_bps[horizon].append(adverse_bps)
                    self.mark_outs_completed += 1
                else:
                    remaining.append(horizon)

            pmo.pending_horizons = remaining
            if not remaining:
                completed_indices.append(i)

        # Remove fully completed entries (iterate in reverse to preserve indices)
        for idx in reversed(completed_indices):
            del self._pending[idx]

    def stats(self) -> MarkOutStats:
        """Compute aggregated adverse selection statistics.

        Returns MarkOutStats with per-horizon averages and a suggested
        adverse_selection_bps value for grid engine calibration.
        """
        avg: dict[float, float] = {}
        obs: dict[float, int] = {}

        for horizon, values in self._completed_adverse_bps.items():
            obs[horizon] = len(values)
            if values:
                avg[horizon] = sum(values) / len(values)
            else:
                avg[horizon] = 0.0

        # Suggest adverse_selection_bps from T+10s mark-out (most relevant
        # for grid trading — long enough to see real moves, short enough
        # to be actionable before the next grid level triggers)
        suggested = avg.get(10.0, 0.0)
        # Clamp to reasonable range [1, 50] bps
        suggested = max(1.0, min(50.0, suggested))

        return MarkOutStats(
            avg_adverse_bps=avg,
            observations=obs,
            suggested_adverse_bps=suggested,
        )

    @staticmethod
    def _compute_adverse_bps(
        fill_price: Decimal,
        mark_out_mid: Decimal,
        side: str,
    ) -> float:
        """Compute adverse selection in bps for a single mark-out.

        Positive = adverse (market moved against us).
        Negative = favorable (market moved in our favor).
        """
        if fill_price <= 0:
            return 0.0

        if side == "buy":
            # Bought at fill_price; if mid fell, we overpaid
            adverse = float((fill_price - mark_out_mid) / fill_price) * 10000
        else:
            # Sold at fill_price; if mid rose, we undersold
            adverse = float((mark_out_mid - fill_price) / fill_price) * 10000

        return adverse
