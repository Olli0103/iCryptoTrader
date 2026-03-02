"""Kraken WebSocket v2 message codec.

Handles serialization/deserialization of all WS v2 message types.
Kraken WS v2 uses JSON messages with a consistent envelope structure.

Reference: https://docs.kraken.com/api/docs/guides/spot-ws-intro/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

import orjson

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """Kraken WS v2 message categories."""

    # Channel data
    CHANNEL_DATA = auto()

    # Request/response
    SUBSCRIBE_RESP = auto()
    UNSUBSCRIBE_RESP = auto()
    ADD_ORDER_RESP = auto()
    AMEND_ORDER_RESP = auto()
    CANCEL_ORDER_RESP = auto()
    CANCEL_ALL_RESP = auto()
    CANCEL_AFTER_RESP = auto()
    BATCH_ADD_RESP = auto()

    # System
    HEARTBEAT = auto()
    PONG = auto()
    STATUS = auto()
    ERROR = auto()

    UNKNOWN = auto()


@dataclass
class WSMessage:
    """Parsed Kraken WS v2 message."""

    msg_type: MessageType = MessageType.UNKNOWN
    channel: str = ""
    data_type: str = ""  # "snapshot", "update", or "" for non-channel msgs
    data: list[dict[str, Any]] = field(default_factory=list)
    method: str = ""
    req_id: int | None = None
    success: bool | None = None
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


# Method name → MessageType mapping for responses
_METHOD_MAP: dict[str, MessageType] = {
    "subscribe": MessageType.SUBSCRIBE_RESP,
    "unsubscribe": MessageType.UNSUBSCRIBE_RESP,
    "add_order": MessageType.ADD_ORDER_RESP,
    "amend_order": MessageType.AMEND_ORDER_RESP,
    "cancel_order": MessageType.CANCEL_ORDER_RESP,
    "cancel_all": MessageType.CANCEL_ALL_RESP,
    "cancel_after": MessageType.CANCEL_AFTER_RESP,
    "batch_add": MessageType.BATCH_ADD_RESP,
}


def decode(raw_bytes: bytes | str) -> WSMessage:
    """Decode a raw WS frame into a WSMessage."""
    try:
        if isinstance(raw_bytes, str):
            obj = orjson.loads(raw_bytes.encode())
        else:
            obj = orjson.loads(raw_bytes)
    except orjson.JSONDecodeError:
        logger.warning("Failed to decode WS message: %s", raw_bytes[:200])
        return WSMessage(msg_type=MessageType.ERROR, error="JSON decode error")

    if not isinstance(obj, dict):
        return WSMessage(msg_type=MessageType.UNKNOWN, raw={"value": obj})

    # Heartbeat
    if obj.get("channel") == "heartbeat":
        return WSMessage(msg_type=MessageType.HEARTBEAT, channel="heartbeat", raw=obj)

    # Pong
    if obj.get("method") == "pong":
        return WSMessage(
            msg_type=MessageType.PONG, method="pong",
            req_id=obj.get("req_id"), raw=obj,
        )

    # System status
    if obj.get("channel") == "status":
        return WSMessage(
            msg_type=MessageType.STATUS,
            channel="status",
            data=[obj.get("data", {})],
            raw=obj,
        )

    # Channel data (book, trade, ticker, ohlc, executions, balances, instrument)
    if "channel" in obj and "data" in obj:
        data = obj["data"]
        if not isinstance(data, list):
            data = [data]
        return WSMessage(
            msg_type=MessageType.CHANNEL_DATA,
            channel=obj["channel"],
            data_type=obj.get("type", ""),
            data=data,
            raw=obj,
        )

    # Method response (subscribe, add_order, amend_order, etc.)
    if "method" in obj:
        method = obj["method"]
        msg_type = _METHOD_MAP.get(method, MessageType.UNKNOWN)
        success = obj.get("success")
        error = obj.get("error", "")
        if success is False and not error:
            error = "Unknown error"
        return WSMessage(
            msg_type=msg_type,
            method=method,
            req_id=obj.get("req_id"),
            success=success,
            error=error,
            result=obj.get("result", {}),
            raw=obj,
        )

    return WSMessage(msg_type=MessageType.UNKNOWN, raw=obj)


def encode_subscribe(
    channel: str,
    params: dict[str, Any] | None = None,
    req_id: int | None = None,
) -> bytes:
    """Encode a subscribe request."""
    msg: dict[str, Any] = {
        "method": "subscribe",
        "params": {"channel": channel, **(params or {})},
    }
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))


def encode_unsubscribe(
    channel: str,
    params: dict[str, Any] | None = None,
    req_id: int | None = None,
) -> bytes:
    """Encode an unsubscribe request."""
    msg: dict[str, Any] = {
        "method": "unsubscribe",
        "params": {"channel": channel, **(params or {})},
    }
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))


def encode_add_order(
    order_type: str,
    side: str,
    pair: str,
    *,
    price: str | None = None,
    quantity: str | None = None,
    cl_ord_id: str | None = None,
    post_only: bool = False,
    req_id: int | None = None,
) -> bytes:
    """Encode an add_order command."""
    params: dict[str, Any] = {
        "order_type": order_type,
        "side": side,
        "symbol": pair,
    }
    if price is not None:
        params["limit_price"] = price
    if quantity is not None:
        params["order_qty"] = quantity
    if cl_ord_id is not None:
        params["cl_ord_id"] = cl_ord_id
    if post_only:
        params["post_only"] = True

    msg: dict[str, Any] = {"method": "add_order", "params": params}
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))


def encode_amend_order(
    order_id: str,
    *,
    new_price: str | None = None,
    new_qty: str | None = None,
    req_id: int | None = None,
) -> bytes:
    """Encode an amend_order command (atomic, queue-preserving)."""
    params: dict[str, Any] = {"order_id": order_id}
    if new_price is not None:
        params["limit_price"] = new_price
    if new_qty is not None:
        params["order_qty"] = new_qty

    msg: dict[str, Any] = {"method": "amend_order", "params": params}
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))


def encode_cancel_order(
    order_id: str | list[str],
    *,
    req_id: int | None = None,
) -> bytes:
    """Encode a cancel_order command. Accepts single ID or list."""
    if isinstance(order_id, list):
        params: dict[str, Any] = {"order_id": order_id}
    else:
        params = {"order_id": [order_id]}

    msg: dict[str, Any] = {"method": "cancel_order", "params": params}
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))


def encode_cancel_all(req_id: int | None = None) -> bytes:
    """Encode a cancel_all command."""
    msg: dict[str, Any] = {"method": "cancel_all"}
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))


def encode_cancel_after(timeout_sec: int, req_id: int | None = None) -> bytes:
    """Encode a cancel_after (dead man's switch) command."""
    msg: dict[str, Any] = {
        "method": "cancel_after",
        "params": {"timeout": timeout_sec},
    }
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))


def encode_batch_add(
    orders: list[dict[str, Any]],
    *,
    req_id: int | None = None,
) -> bytes:
    """Encode a batch_add command for multiple orders in a single frame.

    Each order dict should contain the same fields as a single add_order:
      - order_type: "limit" (or "market")
      - side: "buy" or "sell"
      - symbol: e.g., "XBT/USD"
      - limit_price: str (for limit orders)
      - order_qty: str
      - cl_ord_id: str (optional)
      - post_only: bool (optional)

    Kraken WS v2 batch_add sends up to 15 orders per frame,
    consuming only 1 rate-limit counter increment instead of N.
    """
    msg: dict[str, Any] = {
        "method": "batch_add",
        "params": {"orders": orders},
    }
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))


def encode_batch_cancel(
    order_ids: list[str],
    *,
    cl_ord_ids: list[str] | None = None,
    req_id: int | None = None,
) -> bytes:
    """Encode a batch cancel command using cancel_order with a list of IDs.

    Kraken's cancel_order already supports a list of order_id values.
    This is a convenience wrapper that also supports cl_ord_id lists.
    """
    params: dict[str, Any] = {}
    if order_ids:
        params["order_id"] = order_ids
    if cl_ord_ids:
        params["cl_ord_id"] = cl_ord_ids

    msg: dict[str, Any] = {"method": "cancel_order", "params": params}
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))


def encode_ping(req_id: int | None = None) -> bytes:
    """Encode a ping message."""
    msg: dict[str, Any] = {"method": "ping"}
    if req_id is not None:
        msg["req_id"] = req_id
    return bytes(orjson.dumps(msg))
