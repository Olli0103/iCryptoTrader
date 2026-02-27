"""Tests for amend-first order manager state machine."""

from __future__ import annotations

import time
from decimal import Decimal

from icryptotrader.order.order_manager import (
    Action,
    DesiredLevel,
    OrderManager,
)
from icryptotrader.order.rate_limiter import RateLimiter
from icryptotrader.types import Side, SlotState


def _desired(price: str, qty: str, side: Side = Side.BUY) -> DesiredLevel:
    return DesiredLevel(price=Decimal(price), qty=Decimal(qty), side=side)


class TestDecideAction:
    def test_empty_slot_with_desired_returns_add(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        action = om.decide_action(slot, _desired("85000", "0.01"))
        assert isinstance(action, Action.AddOrder)
        assert action.price == Decimal("85000")
        assert action.qty == Decimal("0.01")
        assert action.side == Side.BUY

    def test_empty_slot_no_desired_returns_noop(self) -> None:
        om = OrderManager(num_slots=1)
        action = om.decide_action(om.slots[0], None)
        assert isinstance(action, Action.Noop)

    def test_live_slot_no_desired_returns_cancel(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        slot.price = Decimal("85000")
        slot.qty = Decimal("0.01")
        slot.side = Side.BUY

        action = om.decide_action(slot, None)
        assert isinstance(action, Action.CancelOrder)
        assert action.order_id == "O123"

    def test_live_slot_same_params_returns_noop(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        slot.price = Decimal("85000")
        slot.qty = Decimal("0.01")
        slot.side = Side.BUY

        action = om.decide_action(slot, _desired("85000", "0.01"))
        assert isinstance(action, Action.Noop)

    def test_live_slot_price_change_returns_amend(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        slot.price = Decimal("85000")
        slot.qty = Decimal("0.01")
        slot.side = Side.BUY

        action = om.decide_action(slot, _desired("84500", "0.01"))
        assert isinstance(action, Action.AmendOrder)
        assert action.order_id == "O123"
        assert action.new_price == Decimal("84500")
        assert action.new_qty is None

    def test_live_slot_qty_change_returns_amend(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        slot.price = Decimal("85000")
        slot.qty = Decimal("0.01")
        slot.side = Side.BUY

        action = om.decide_action(slot, _desired("85000", "0.02"))
        assert isinstance(action, Action.AmendOrder)
        assert action.new_qty == Decimal("0.02")
        assert action.new_price is None

    def test_live_slot_both_change_returns_amend(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        slot.price = Decimal("85000")
        slot.qty = Decimal("0.01")
        slot.side = Side.BUY

        action = om.decide_action(slot, _desired("84000", "0.02"))
        assert isinstance(action, Action.AmendOrder)
        assert action.new_price == Decimal("84000")
        assert action.new_qty == Decimal("0.02")

    def test_live_slot_side_change_returns_cancel(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        slot.price = Decimal("85000")
        slot.qty = Decimal("0.01")
        slot.side = Side.BUY

        action = om.decide_action(slot, _desired("85000", "0.01", Side.SELL))
        assert isinstance(action, Action.CancelOrder)

    def test_pending_slot_returns_noop(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.PENDING_NEW
        slot.pending_since = time.monotonic()

        action = om.decide_action(slot, _desired("85000", "0.01"))
        assert isinstance(action, Action.Noop)

    def test_pending_timeout_returns_cancel(self) -> None:
        om = OrderManager(num_slots=1, pending_timeout_ms=0)
        slot = om.slots[0]
        slot.state = SlotState.PENDING_NEW
        slot.order_id = "O123"
        slot.pending_since = time.monotonic() - 1.0  # 1 second ago

        action = om.decide_action(slot, _desired("85000", "0.01"))
        assert isinstance(action, Action.CancelOrder)
        assert om.timeout_cancels == 1


class TestPrepareCommands:
    def test_prepare_add(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        action = Action.AddOrder(Decimal("85000"), Decimal("0.01"), Side.BUY)
        cmd = om.prepare_add(slot, action)

        assert slot.state == SlotState.PENDING_NEW
        assert slot.cl_ord_id != ""
        assert cmd["price"] == "85000"
        assert cmd["quantity"] == "0.01"
        assert cmd["side"] == "buy"
        assert cmd["post_only"] is True
        assert om.orders_placed == 1

    def test_prepare_add_populates_req_id(self) -> None:
        """req_id must be generated, stored on slot, in _req_id_to_slot, and in params."""
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        action = Action.AddOrder(Decimal("85000"), Decimal("0.01"), Side.BUY)
        cmd = om.prepare_add(slot, action)

        req_id = cmd["req_id"]
        assert req_id > 0
        assert slot.pending_req_id == req_id
        assert om._req_id_to_slot[req_id] is slot

    def test_prepare_amend(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        action = Action.AmendOrder("O123", new_price=Decimal("84000"))
        cmd = om.prepare_amend(slot, action)

        assert slot.state == SlotState.AMEND_PENDING
        assert cmd["order_id"] == "O123"
        assert cmd["new_price"] == "84000"
        assert om.orders_amended == 1

    def test_prepare_cancel(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        action = Action.CancelOrder("O123")
        cmd = om.prepare_cancel(slot, action)

        assert slot.state == SlotState.CANCEL_PENDING
        assert cmd["order_id"] == "O123"
        assert om.orders_cancelled == 1


class TestExecutionEvents:
    def test_add_order_ack_routed_via_req_id_from_prepare(self) -> None:
        """End-to-end: prepare_add generates req_id, on_add_order_ack routes via it."""
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        action = Action.AddOrder(Decimal("85000"), Decimal("0.01"), Side.BUY)
        cmd = om.prepare_add(slot, action)
        req_id = cmd["req_id"]

        om.on_add_order_ack(req_id=req_id, order_id="O123", success=True)
        assert slot.state == SlotState.LIVE
        assert slot.order_id == "O123"
        # req_id should be consumed from the map
        assert req_id not in om._req_id_to_slot

    def test_add_order_ack_success(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.PENDING_NEW
        slot.pending_req_id = 100

        om.on_add_order_ack(req_id=100, order_id="O123", success=True)
        assert slot.state == SlotState.LIVE
        assert slot.order_id == "O123"

    def test_add_order_ack_failure(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.PENDING_NEW
        slot.pending_req_id = 100

        om.on_add_order_ack(req_id=100, order_id="", success=False, error="Insufficient funds")
        assert slot.state == SlotState.EMPTY

    def test_amend_ack_success(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.AMEND_PENDING
        slot.order_id = "O123"
        slot.price = Decimal("85000")
        slot.desired = _desired("84000", "0.01")
        om._order_id_to_slot["O123"] = slot

        om.on_amend_order_ack("O123", success=True)
        assert slot.state == SlotState.LIVE
        assert slot.price == Decimal("84000")

    def test_amend_ack_failure_reverts_to_live(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.AMEND_PENDING
        slot.order_id = "O123"
        slot.price = Decimal("85000")
        om._order_id_to_slot["O123"] = slot

        om.on_amend_order_ack("O123", success=False, error="Invalid price")
        assert slot.state == SlotState.LIVE
        assert slot.price == Decimal("85000")  # Unchanged
        assert om.amend_rejects == 1

    def test_cancel_ack_success(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.CANCEL_PENDING
        slot.order_id = "O123"
        om._order_id_to_slot["O123"] = slot

        om.on_cancel_ack("O123", success=True)
        assert slot.state == SlotState.EMPTY
        assert slot.order_id == ""

    def test_full_fill_via_execution(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        slot.qty = Decimal("0.01")
        slot.side = Side.BUY
        slot.price = Decimal("85000")
        om._order_id_to_slot["O123"] = slot

        fills_received: list = []
        om.on_fill(lambda s, d: fills_received.append((s.slot_id, d)))

        om.on_execution_event({
            "exec_type": "trade",
            "order_id": "O123",
            "last_qty": "0.01",
            "last_price": "85000.0",
        })
        assert slot.state == SlotState.EMPTY
        assert om.orders_filled == 1
        assert len(fills_received) == 1

    def test_partial_fill_stays_live(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        slot.qty = Decimal("0.10")
        om._order_id_to_slot["O123"] = slot

        om.on_execution_event({
            "exec_type": "trade",
            "order_id": "O123",
            "last_qty": "0.03",
            "last_price": "85000.0",
        })
        assert slot.state == SlotState.LIVE
        assert slot.filled_qty == Decimal("0.03")

    def test_restated_confirms_amend(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.AMEND_PENDING
        slot.order_id = "O123"
        om._order_id_to_slot["O123"] = slot

        om.on_execution_event({
            "exec_type": "restated",
            "order_id": "O123",
            "limit_price": "84000.0",
            "order_qty": "0.02",
        })
        assert slot.state == SlotState.LIVE
        assert slot.price == Decimal("84000.0")
        assert slot.qty == Decimal("0.02")

    def test_canceled_execution_event(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        om._order_id_to_slot["O123"] = slot

        om.on_execution_event({"exec_type": "canceled", "order_id": "O123"})
        assert slot.state == SlotState.EMPTY

    def test_rate_count_synced_from_execution(self) -> None:
        rl = RateLimiter(decay_rate=0.0)
        om = OrderManager(num_slots=1, rate_limiter=rl)
        slot = om.slots[0]
        slot.state = SlotState.LIVE
        slot.order_id = "O123"
        om._order_id_to_slot["O123"] = slot

        om.on_execution_event({
            "exec_type": "trade",
            "order_id": "O123",
            "last_qty": "0.01",
            "last_price": "85000.0",
            "rate_count": 42.5,
        })
        assert rl.estimated_count == 42.5


class TestReconciliation:
    def test_reconcile_order_still_open(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.AMEND_PENDING  # Was pending when disconnect happened
        slot.order_id = "O123"
        om._order_id_to_slot["O123"] = slot

        om.reconcile_snapshot(
            open_orders=[{
                "order_id": "O123",
                "limit_price": "84500",
                "order_qty": "0.015",
                "filled_qty": "0.003",
            }],
            recent_trades=[],
        )
        assert slot.state == SlotState.LIVE
        assert slot.price == Decimal("84500")
        assert slot.qty == Decimal("0.015")
        assert slot.filled_qty == Decimal("0.003")

    def test_reconcile_order_disappeared(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.PENDING_NEW
        slot.cl_ord_id = "my-cl-id"
        om._cl_ord_id_to_slot["my-cl-id"] = slot

        # Order not in snapshot — was filled or rejected during disconnect
        om.reconcile_snapshot(open_orders=[], recent_trades=[])
        assert slot.state == SlotState.EMPTY

    def test_reconcile_by_cl_ord_id(self) -> None:
        om = OrderManager(num_slots=1)
        slot = om.slots[0]
        slot.state = SlotState.PENDING_NEW
        slot.cl_ord_id = "my-cl-id"
        om._cl_ord_id_to_slot["my-cl-id"] = slot

        om.reconcile_snapshot(
            open_orders=[{
                "order_id": "O999",
                "cl_ord_id": "my-cl-id",
                "limit_price": "85000",
                "order_qty": "0.01",
                "filled_qty": "0",
            }],
            recent_trades=[],
        )
        assert slot.state == SlotState.LIVE
        assert slot.order_id == "O999"


class TestQueryMethods:
    def test_live_slots(self) -> None:
        om = OrderManager(num_slots=3)
        om.slots[0].state = SlotState.LIVE
        om.slots[1].state = SlotState.EMPTY
        om.slots[2].state = SlotState.LIVE
        assert len(om.live_slots()) == 2

    def test_empty_slots(self) -> None:
        om = OrderManager(num_slots=3)
        assert len(om.empty_slots()) == 3

    def test_buy_sell_slots(self) -> None:
        om = OrderManager(num_slots=4)
        om.slots[0].state = SlotState.LIVE
        om.slots[0].side = Side.BUY
        om.slots[1].state = SlotState.LIVE
        om.slots[1].side = Side.SELL
        om.slots[2].state = SlotState.LIVE
        om.slots[2].side = Side.BUY
        assert len(om.buy_slots()) == 2
        assert len(om.sell_slots()) == 1
