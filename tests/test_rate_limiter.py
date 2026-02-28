"""Tests for rate limiter."""

from __future__ import annotations

import time

from icryptotrader.order.rate_limiter import (
    COST_ADD_ORDER,
    COST_AMEND_ORDER,
    COST_CANCEL_ORDER,
    RateLimiter,
)


class TestBasicBehavior:
    def test_starts_at_zero(self) -> None:
        rl = RateLimiter()
        assert rl.estimated_count < 0.1  # ~0 after tiny decay

    def test_can_send_initially(self) -> None:
        rl = RateLimiter()
        assert rl.can_send() is True

    def test_record_increments_counter(self) -> None:
        rl = RateLimiter(decay_rate=0.0)  # No decay for testing
        rl.record_send(1.0)
        rl.record_send(1.0)
        assert rl.estimated_count == 2.0

    def test_throttles_at_threshold(self) -> None:
        rl = RateLimiter(max_counter=10, headroom_pct=0.80, decay_rate=0.0)
        # Threshold = 10 * 0.80 = 8.0
        for _ in range(8):
            rl.record_send(1.0)
        assert rl.can_send(1.0) is False

    def test_headroom(self) -> None:
        rl = RateLimiter(max_counter=180, headroom_pct=0.80, decay_rate=0.0)
        # Threshold = 144
        assert rl.headroom == 144.0
        rl.record_send(10.0)
        assert rl.headroom == 134.0


class TestDecay:
    def test_counter_decays_over_time(self) -> None:
        rl = RateLimiter(decay_rate=100.0)  # Fast decay for testing
        rl.record_send(50.0)
        time.sleep(0.1)  # 100.0 * 0.1 = 10 units decayed
        count = rl.estimated_count
        assert count < 50.0
        assert count > 30.0  # Should have decayed ~10 units

    def test_counter_floors_at_zero(self) -> None:
        rl = RateLimiter(decay_rate=1000.0)
        rl.record_send(1.0)
        time.sleep(0.01)  # More than enough to decay to 0
        assert rl.estimated_count == 0.0


class TestServerSync:
    def test_update_from_server(self) -> None:
        rl = RateLimiter(decay_rate=0.0)
        rl.record_send(50.0)
        rl.update_from_server(30.0)  # Server says counter is 30
        assert rl.estimated_count == 30.0

    def test_server_overrides_local(self) -> None:
        rl = RateLimiter(decay_rate=0.0)
        rl.record_send(100.0)
        rl.update_from_server(5.0)  # Server knows better
        assert rl.estimated_count == 5.0


class TestMethodCosts:
    def test_cancel_cost_is_zero(self) -> None:
        rl = RateLimiter()
        assert rl.cost_for_method("cancel_order") == COST_CANCEL_ORDER
        assert COST_CANCEL_ORDER == 0.0

    def test_amend_cost_lower_than_add(self) -> None:
        rl = RateLimiter()
        assert rl.cost_for_method("amend_order") == COST_AMEND_ORDER
        assert COST_AMEND_ORDER < COST_ADD_ORDER

    def test_add_cost(self) -> None:
        rl = RateLimiter()
        assert rl.cost_for_method("add_order") == COST_ADD_ORDER


class TestShouldThrottle:
    def test_cancel_never_throttled(self) -> None:
        rl = RateLimiter(max_counter=10, headroom_pct=0.80, decay_rate=0.0)
        # Fill to capacity
        for _ in range(10):
            rl.record_send(1.0)
        # Cancel should still pass
        assert rl.should_throttle("cancel_order") is False
        assert rl.should_throttle("cancel_all") is False

    def test_add_throttled_when_full(self) -> None:
        rl = RateLimiter(max_counter=10, headroom_pct=0.80, decay_rate=0.0)
        for _ in range(8):
            rl.record_send(1.0)
        assert rl.should_throttle("add_order") is True
        assert rl.throttle_count == 1

    def test_amend_not_throttled_when_room(self) -> None:
        rl = RateLimiter(max_counter=180, headroom_pct=0.80, decay_rate=0.0)
        assert rl.should_throttle("amend_order") is False


class TestUtilization:
    def test_zero_when_empty(self) -> None:
        rl = RateLimiter()
        assert rl.utilization_pct < 0.01

    def test_increases_with_sends(self) -> None:
        rl = RateLimiter(max_counter=100, headroom_pct=1.0, decay_rate=0.0)
        rl.record_send(50.0)
        assert rl.utilization_pct == 0.5
