"""Amend-first order slot state machine.

Each grid level maps to an OrderSlot. The state machine enforces:
  - Never stack commands on a PENDING slot (WS sequencing not guaranteed)
  - Prefer amend_order over cancel+new (preserves queue priority)
  - Track cl_ord_id for reconciliation after reconnect
  - Fills immediately update the FIFO ledger

State transitions:
  EMPTY → PENDING_NEW (add_order sent)
  PENDING_NEW → LIVE (ack received, exec_type=new)
  LIVE → AMEND_PENDING (amend_order sent)
  AMEND_PENDING → LIVE (ack, exec_type=restated)
  LIVE → CANCEL_PENDING (cancel_order sent)
  CANCEL_PENDING → EMPTY (ack, exec_type=canceled)
  LIVE → EMPTY (fully filled)
  PENDING_NEW/AMEND_PENDING → timeout → CANCEL_PENDING (stale)
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from icryptotrader.order.rate_limiter import (
    COST_ADD_ORDER,
    COST_AMEND_ORDER,
    COST_CANCEL_ORDER,
    RateLimiter,
)
from icryptotrader.types import Side, SlotState

logger = logging.getLogger(__name__)

# Price/qty comparison epsilon (avoid floating point noise)
PRICE_EPSILON = Decimal("0.01")  # $0.01 for BTC/USD
QTY_EPSILON = Decimal("0.00000001")  # 1 satoshi


@dataclass
class DesiredLevel:
    """What the strategy wants at a given grid slot."""

    price: Decimal
    qty: Decimal
    side: Side


@dataclass
class OrderSlot:
    """Tracks state for a single order slot in the grid."""

    slot_id: int = 0
    state: SlotState = SlotState.EMPTY

    # Kraken-assigned order ID (known after PENDING_NEW → LIVE)
    order_id: str = ""
    # Client-assigned order ID (known from add_order, used for reconnect reconciliation)
    cl_ord_id: str = ""

    # Current order parameters (as confirmed by exchange)
    side: Side = Side.BUY
    price: Decimal = Decimal("0")
    qty: Decimal = Decimal("0")
    filled_qty: Decimal = Decimal("0")

    # Pending state tracking
    pending_since: float = 0.0  # monotonic timestamp
    pending_req_id: int = 0

    # Desired state from last strategy tick (for reconciliation)
    desired: DesiredLevel | None = None

    def remaining_qty(self) -> Decimal:
        return self.qty - self.filled_qty


class Action:
    """Actions the order manager can take on a slot."""

    class AddOrder:
        def __init__(self, price: Decimal, qty: Decimal, side: Side) -> None:
            self.price = price
            self.qty = qty
            self.side = side

    class AmendOrder:
        def __init__(
            self,
            order_id: str,
            new_price: Decimal | None = None,
            new_qty: Decimal | None = None,
        ) -> None:
            self.order_id = order_id
            self.new_price = new_price
            self.new_qty = new_qty

    class CancelOrder:
        def __init__(self, order_id: str) -> None:
            self.order_id = order_id

    class Noop:
        pass


class OrderManager:
    """Manages order slots with amend-first logic.

    Coordinates between the strategy engine (which sets desired levels)
    and WS2 (which sends/receives order commands).
    """

    def __init__(
        self,
        num_slots: int = 10,
        rate_limiter: RateLimiter | None = None,
        pending_timeout_ms: int = 500,
        pair: str = "XBT/USD",
    ) -> None:
        self._pair = pair
        self._rate_limiter = rate_limiter or RateLimiter()
        self._pending_timeout_sec = pending_timeout_ms / 1000.0

        # Order slots: indices 0..num_slots-1
        self._slots = [OrderSlot(slot_id=i) for i in range(num_slots)]

        # Lookup maps for fast routing of execution events
        self._order_id_to_slot: dict[str, OrderSlot] = {}
        self._cl_ord_id_to_slot: dict[str, OrderSlot] = {}
        self._req_id_to_slot: dict[int, OrderSlot] = {}

        # req_id counter (offset from WS req_ids to avoid collisions)
        self._req_id_counter = 2000

        # Fill callback (called on every fill for FIFO ledger integration)
        self._on_fill: list[Any] = []

        # Metrics
        self.orders_placed: int = 0
        self.orders_amended: int = 0
        self.orders_cancelled: int = 0
        self.orders_filled: int = 0
        self.amend_rejects: int = 0
        self.timeout_cancels: int = 0

    @property
    def slots(self) -> list[OrderSlot]:
        return self._slots

    def on_fill(self, callback: Any) -> None:
        """Register callback for fills: callback(slot, fill_data)."""
        self._on_fill.append(callback)

    # --- Strategy interface ---

    def decide_action(
        self, slot: OrderSlot, desired: DesiredLevel | None,
    ) -> Action.AddOrder | Action.AmendOrder | Action.CancelOrder | Action.Noop:
        """Per-slot decision: amend, cancel, add, or no-op.

        Called once per strategy tick per slot. Returns an Action that the
        caller should execute via WS2.
        """
        slot.desired = desired
        now = time.monotonic()

        # EMPTY slot
        if slot.state == SlotState.EMPTY:
            if desired is not None:
                return Action.AddOrder(desired.price, desired.qty, desired.side)
            return Action.Noop()

        # PENDING slots: do NOT stack commands
        if slot.state in (SlotState.PENDING_NEW, SlotState.AMEND_PENDING, SlotState.CANCEL_PENDING):
            elapsed = now - slot.pending_since
            if elapsed > self._pending_timeout_sec and slot.state != SlotState.CANCEL_PENDING:
                # Stale pending — force cancel
                self.timeout_cancels += 1
                logger.warning(
                    "Slot %d: pending timeout (%.0fms), forcing cancel on %s",
                    slot.slot_id, elapsed * 1000, slot.order_id or slot.cl_ord_id,
                )
                if slot.order_id:
                    return Action.CancelOrder(slot.order_id)
            return Action.Noop()

        # LIVE slot
        if slot.state == SlotState.LIVE:
            if desired is None:
                return Action.CancelOrder(slot.order_id)

            price_changed = abs(slot.price - desired.price) > PRICE_EPSILON
            qty_changed = abs(slot.remaining_qty() - desired.qty) > QTY_EPSILON
            side_changed = slot.side != desired.side

            # Side change requires cancel+new (can't amend side)
            if side_changed:
                return Action.CancelOrder(slot.order_id)

            if not price_changed and not qty_changed:
                return Action.Noop()

            # Amend: single-phase, preserves queue priority on qty-only changes
            return Action.AmendOrder(
                order_id=slot.order_id,
                new_price=desired.price if price_changed else None,
                new_qty=desired.qty if qty_changed else None,
            )

        return Action.Noop()

    # --- Command execution (called by strategy after decide_action) ---

    def _next_req_id(self) -> int:
        self._req_id_counter += 1
        return self._req_id_counter

    def prepare_add(self, slot: OrderSlot, action: Action.AddOrder) -> dict[str, Any]:
        """Prepare an add_order command. Returns kwargs for WS2.send_add_order.

        Generates a ``req_id`` and registers it in ``_req_id_to_slot`` so that
        ``on_add_order_ack`` can route the response back to this slot.  The
        ``req_id`` is included in the returned params dict; callers that forward
        to ``WSPrivate.send_add_order`` should pass it through.
        """
        cl_ord_id = str(uuid.uuid4())
        req_id = self._next_req_id()

        slot.state = SlotState.PENDING_NEW
        slot.pending_since = time.monotonic()
        slot.cl_ord_id = cl_ord_id
        slot.pending_req_id = req_id
        slot.side = action.side
        slot.price = action.price
        slot.qty = action.qty
        slot.filled_qty = Decimal("0")

        self._cl_ord_id_to_slot[cl_ord_id] = slot
        self._req_id_to_slot[req_id] = slot
        self.orders_placed += 1
        self._rate_limiter.record_send(COST_ADD_ORDER)

        return {
            "order_type": "limit",
            "side": action.side.value,
            "pair": self._pair,
            "price": str(action.price),
            "quantity": str(action.qty),
            "cl_ord_id": cl_ord_id,
            "post_only": True,
            "req_id": req_id,
        }

    def prepare_amend(self, slot: OrderSlot, action: Action.AmendOrder) -> dict[str, Any]:
        """Prepare an amend_order command. Returns kwargs for WS2.send_amend_order."""
        slot.state = SlotState.AMEND_PENDING
        slot.pending_since = time.monotonic()
        self.orders_amended += 1
        self._rate_limiter.record_send(COST_AMEND_ORDER)

        cmd: dict[str, Any] = {"order_id": action.order_id}
        if action.new_price is not None:
            cmd["new_price"] = str(action.new_price)
        if action.new_qty is not None:
            cmd["new_qty"] = str(action.new_qty)
        return cmd

    def prepare_cancel(self, slot: OrderSlot, action: Action.CancelOrder) -> dict[str, Any]:
        """Prepare a cancel_order command. Returns kwargs for WS2.send_cancel_order."""
        slot.state = SlotState.CANCEL_PENDING
        slot.pending_since = time.monotonic()
        self.orders_cancelled += 1
        self._rate_limiter.record_send(COST_CANCEL_ORDER)
        return {"order_id": action.order_id}

    # --- Execution event handlers (called from WS2 callbacks) ---

    def on_add_order_ack(self, req_id: int, order_id: str, success: bool, error: str = "") -> None:
        """Handle add_order response from WS2."""
        slot = self._req_id_to_slot.pop(req_id, None)
        if slot is None:
            # Try cl_ord_id lookup from the result
            for s in self._slots:
                if s.state == SlotState.PENDING_NEW and s.pending_req_id == req_id:
                    slot = s
                    break
        if slot is None:
            logger.warning("add_order ack for unknown req_id %d", req_id)
            return

        if success:
            slot.state = SlotState.LIVE
            slot.order_id = order_id
            self._order_id_to_slot[order_id] = slot
            logger.info(
                "Slot %d: LIVE order_id=%s price=%s qty=%s %s",
                slot.slot_id, order_id, slot.price, slot.qty, slot.side.value,
            )
        else:
            slot.state = SlotState.EMPTY
            slot.order_id = ""
            logger.error("Slot %d: add_order rejected: %s", slot.slot_id, error)
            self._cleanup_slot_maps(slot)

    def on_amend_order_ack(self, order_id: str, success: bool, error: str = "") -> None:
        """Handle amend_order response."""
        slot = self._order_id_to_slot.get(order_id)
        if slot is None:
            logger.warning("amend_order ack for unknown order_id %s", order_id)
            return

        if success:
            slot.state = SlotState.LIVE
            # Update price/qty from the desired level (confirmed by exchange)
            if slot.desired:
                if abs(slot.price - slot.desired.price) > PRICE_EPSILON:
                    slot.price = slot.desired.price
                if abs(slot.remaining_qty() - slot.desired.qty) > QTY_EPSILON:
                    slot.qty = slot.desired.qty + slot.filled_qty
            logger.info(
                "Slot %d: amended order_id=%s price=%s qty=%s",
                slot.slot_id, order_id, slot.price, slot.qty,
            )
        else:
            # Amend rejected — revert to LIVE with old params
            slot.state = SlotState.LIVE
            self.amend_rejects += 1
            logger.warning("Slot %d: amend rejected: %s", slot.slot_id, error)

    def on_cancel_ack(self, order_id: str, success: bool, error: str = "") -> None:
        """Handle cancel_order response."""
        slot = self._order_id_to_slot.get(order_id)
        if slot is None:
            logger.warning("cancel ack for unknown order_id %s", order_id)
            return

        if success:
            slot.state = SlotState.EMPTY
            logger.info("Slot %d: cancelled order_id=%s", slot.slot_id, order_id)
            self._cleanup_slot_maps(slot)
        else:
            # Cancel rejected (order may have already filled)
            logger.warning("Slot %d: cancel rejected: %s", slot.slot_id, error)
            # Don't change state — will be resolved by execution events

    def on_execution_event(self, exec_data: dict[str, Any]) -> None:
        """Handle an execution event from the executions channel.

        Execution events include: new, trade (fill), restated (amend), canceled.
        """
        exec_type = exec_data.get("exec_type", "")
        order_id = exec_data.get("order_id", "")
        cl_ord_id = exec_data.get("cl_ord_id", "")

        # Find the slot
        slot = self._order_id_to_slot.get(order_id)
        if slot is None and cl_ord_id:
            slot = self._cl_ord_id_to_slot.get(cl_ord_id)

        if exec_type == "new":
            # Order accepted — matches our add_order ack path
            if slot and slot.state == SlotState.PENDING_NEW:
                slot.state = SlotState.LIVE
                if order_id and not slot.order_id:
                    slot.order_id = order_id
                    self._order_id_to_slot[order_id] = slot

        elif exec_type == "trade":
            # Fill (partial or full)
            if slot is None:
                logger.warning("Fill for unknown order %s", order_id)
                return
            fill_qty = Decimal(str(exec_data.get("last_qty", "0")))
            fill_price = Decimal(str(exec_data.get("last_price", "0")))
            slot.filled_qty += fill_qty

            is_full_fill = slot.filled_qty >= slot.qty
            if is_full_fill:
                slot.state = SlotState.EMPTY
                self.orders_filled += 1
                logger.info(
                    "Slot %d: FILLED order_id=%s fill_qty=%s @ %s (total filled=%s)",
                    slot.slot_id, order_id, fill_qty, fill_price, slot.filled_qty,
                )
                self._cleanup_slot_maps(slot)
            else:
                logger.info(
                    "Slot %d: partial fill order_id=%s fill_qty=%s @ %s (filled=%s/%s)",
                    slot.slot_id, order_id, fill_qty, fill_price,
                    slot.filled_qty, slot.qty,
                )

            # Notify fill callbacks (for FIFO ledger)
            for cb in self._on_fill:
                try:
                    cb(slot, exec_data)
                except Exception:
                    logger.exception("Fill callback error")

        elif exec_type == "restated":
            # Amend confirmed
            if slot and slot.state == SlotState.AMEND_PENDING:
                slot.state = SlotState.LIVE
                # Update from execution data if available
                new_price = exec_data.get("limit_price")
                new_qty = exec_data.get("order_qty")
                if new_price:
                    slot.price = Decimal(str(new_price))
                if new_qty:
                    slot.qty = Decimal(str(new_qty))

        elif exec_type == "canceled" and slot:
            slot.state = SlotState.EMPTY
            logger.info("Slot %d: canceled via execution event", slot.slot_id)
            self._cleanup_slot_maps(slot)

        # Update rate counter from server if present
        rate_count = exec_data.get("rate_count")
        if rate_count is not None:
            self._rate_limiter.update_from_server(float(rate_count))

    # --- Reconciliation (after reconnect) ---

    def reconcile_snapshot(
        self, open_orders: list[dict[str, Any]], recent_trades: list[dict[str, Any]]
    ) -> None:
        """Reconcile local state against exchange snapshot after reconnect.

        Called after WS2 reconnects and receives executions snapshot.
        """
        snapshot_order_ids = {o.get("order_id", ""): o for o in open_orders}

        for slot in self._slots:
            if slot.state == SlotState.EMPTY:
                continue

            if slot.order_id and slot.order_id in snapshot_order_ids:
                # Order still exists — update from snapshot
                snap = snapshot_order_ids.pop(slot.order_id)
                slot.state = SlotState.LIVE
                slot.price = Decimal(str(snap.get("limit_price", slot.price)))
                slot.qty = Decimal(str(snap.get("order_qty", slot.qty)))
                slot.filled_qty = Decimal(str(snap.get("filled_qty", slot.filled_qty)))
                logger.info(
                    "Slot %d: reconciled from snapshot, order_id=%s, price=%s, qty=%s",
                    slot.slot_id, slot.order_id, slot.price, slot.qty,
                )
            elif slot.cl_ord_id:
                # Check if order exists under cl_ord_id
                found = False
                for oid, snap in list(snapshot_order_ids.items()):
                    if snap.get("cl_ord_id") == slot.cl_ord_id:
                        slot.state = SlotState.LIVE
                        slot.order_id = oid
                        slot.price = Decimal(str(snap.get("limit_price", slot.price)))
                        slot.qty = Decimal(str(snap.get("order_qty", slot.qty)))
                        slot.filled_qty = Decimal(str(snap.get("filled_qty", "0")))
                        self._order_id_to_slot[oid] = slot
                        snapshot_order_ids.pop(oid)
                        found = True
                        logger.info(
                            "Slot %d: reconciled by cl_ord_id, order_id=%s",
                            slot.slot_id, oid,
                        )
                        break
                if not found:
                    # Order gone — was filled or cancelled during disconnect
                    slot.state = SlotState.EMPTY
                    logger.info(
                        "Slot %d: order disappeared during disconnect (filled or cancelled)",
                        slot.slot_id,
                    )
                    self._cleanup_slot_maps(slot)
            else:
                # No order_id or cl_ord_id — mark empty
                slot.state = SlotState.EMPTY
                self._cleanup_slot_maps(slot)

        # Orphan orders: in snapshot but not in any local slot — cancel them
        orphan_ids = list(snapshot_order_ids.keys())
        if orphan_ids:
            logger.warning(
                "Found %d orphan orders after reconnect: %s",
                len(orphan_ids), orphan_ids,
            )
        # Caller should cancel these via WS2

        return  # orphan_ids available via the snapshot_order_ids that remain

    # --- Query methods ---

    def live_slots(self) -> list[OrderSlot]:
        return [s for s in self._slots if s.state == SlotState.LIVE]

    def empty_slots(self) -> list[OrderSlot]:
        return [s for s in self._slots if s.state == SlotState.EMPTY]

    def pending_slots(self) -> list[OrderSlot]:
        return [
            s for s in self._slots
            if s.state in (SlotState.PENDING_NEW, SlotState.AMEND_PENDING, SlotState.CANCEL_PENDING)
        ]

    def buy_slots(self) -> list[OrderSlot]:
        return [s for s in self._slots if s.state != SlotState.EMPTY and s.side == Side.BUY]

    def sell_slots(self) -> list[OrderSlot]:
        return [s for s in self._slots if s.state != SlotState.EMPTY and s.side == Side.SELL]

    def slot_by_order_id(self, order_id: str) -> OrderSlot | None:
        return self._order_id_to_slot.get(order_id)

    # --- Helpers ---

    def _cleanup_slot_maps(self, slot: OrderSlot) -> None:
        """Remove a slot from all lookup maps."""
        if slot.order_id:
            self._order_id_to_slot.pop(slot.order_id, None)
        if slot.cl_ord_id:
            self._cl_ord_id_to_slot.pop(slot.cl_ord_id, None)
        if slot.pending_req_id:
            self._req_id_to_slot.pop(slot.pending_req_id, None)
        slot.order_id = ""
        slot.cl_ord_id = ""
        slot.pending_req_id = 0
        slot.filled_qty = Decimal("0")
