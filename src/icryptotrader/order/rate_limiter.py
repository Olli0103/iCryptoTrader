"""Per-pair rate limiter for Kraken spot trading.

Kraken uses a shared rate counter across REST, WS, and FIX:
  - Each add_order: +1 (fixed) + decaying penalty based on resting time
  - Each amend_order: lower penalty (atomic amends are cheaper)
  - Each cancel_order: always accepted (even when counter exceeds max)
  - Decay: Pro tier = 3.75/sec, Intermediate = 2.34/sec
  - Max counter: Pro = 180

The authoritative rate_count is available in the executions channel.
Between updates, we maintain a conservative local estimate.

Reference: https://docs.kraken.com/api/docs/guides/spot-ratelimits/
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Cost per command type (conservative estimates)
COST_ADD_ORDER = 1.0
COST_AMEND_ORDER = 0.5  # Atomic amends have lower cost
COST_CANCEL_ORDER = 0.0  # Cancels are always accepted


class RateLimiter:
    """Tracks Kraken's per-pair rate counter and gates outbound commands.

    Uses the authoritative rate_count from executions channel when available,
    with a conservative local estimate between updates.
    """

    def __init__(
        self,
        max_counter: int = 180,
        decay_rate: float = 3.75,
        headroom_pct: float = 0.80,
    ) -> None:
        self._max_counter = max_counter
        self._decay_rate = decay_rate
        self._headroom_pct = headroom_pct
        self._threshold = max_counter * headroom_pct

        self._estimated_count: float = 0.0
        self._last_update_ts: float = time.monotonic()
        self._authoritative_count: float | None = None

        # Metrics
        self.throttle_count: int = 0

    @property
    def estimated_count(self) -> float:
        """Current estimated rate counter (after decay)."""
        self._decay()
        return self._estimated_count

    @property
    def headroom(self) -> float:
        """Remaining budget before throttling (in counter units)."""
        return max(0.0, self._threshold - self.estimated_count)

    @property
    def utilization_pct(self) -> float:
        """Rate limit utilization as percentage of threshold."""
        if self._threshold == 0:
            return 0.0
        return self.estimated_count / self._threshold

    def can_send(self, cost: float = COST_ADD_ORDER) -> bool:
        """Check if a command with the given cost can be sent without exceeding the threshold."""
        self._decay()
        return (self._estimated_count + cost) < self._threshold

    def record_send(self, cost: float = COST_ADD_ORDER) -> None:
        """Record that a command was sent (increment counter)."""
        self._decay()
        self._estimated_count += cost

    def update_from_server(self, server_rate_count: float) -> None:
        """Sync from the authoritative rate_count in executions channel.

        This corrects any drift in our local estimate.
        """
        self._authoritative_count = server_rate_count
        self._estimated_count = server_rate_count
        self._last_update_ts = time.monotonic()

    def cost_for_method(self, method: str) -> float:
        """Return the rate limit cost for a given command method."""
        if method == "cancel_order" or method == "cancel_all":
            return COST_CANCEL_ORDER
        if method == "amend_order":
            return COST_AMEND_ORDER
        return COST_ADD_ORDER

    def should_throttle(self, method: str) -> bool:
        """Check if a specific method should be throttled.

        Cancels are NEVER throttled (Kraken always accepts them).
        Other commands are throttled based on the rate counter.
        """
        cost = self.cost_for_method(method)
        if cost == 0.0:
            return False  # Cancels always pass
        if not self.can_send(cost):
            self.throttle_count += 1
            logger.warning(
                "Rate limited: %s (counter=%.1f, threshold=%.1f)",
                method, self._estimated_count, self._threshold,
            )
            return True
        return False

    def _decay(self) -> None:
        """Apply time-based decay to the estimated counter."""
        now = time.monotonic()
        elapsed = now - self._last_update_ts
        self._estimated_count = max(0.0, self._estimated_count - elapsed * self._decay_rate)
        self._last_update_ts = now
