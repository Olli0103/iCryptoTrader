"""Tests for Kraken WS v2 codec."""

from __future__ import annotations

import orjson

from icryptotrader.ws.ws_codec import (
    MessageType,
    decode,
    encode_add_order,
    encode_amend_order,
    encode_cancel_after,
    encode_cancel_all,
    encode_cancel_order,
    encode_ping,
    encode_subscribe,
)


class TestDecode:
    def test_heartbeat(self) -> None:
        raw = b'{"channel":"heartbeat"}'
        msg = decode(raw)
        assert msg.msg_type == MessageType.HEARTBEAT

    def test_pong(self) -> None:
        raw = b'{"method":"pong","req_id":42}'
        msg = decode(raw)
        assert msg.msg_type == MessageType.PONG
        assert msg.req_id == 42

    def test_channel_data_book(self) -> None:
        raw = orjson.dumps({
            "channel": "book",
            "type": "snapshot",
            "data": [{"asks": [], "bids": []}],
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.CHANNEL_DATA
        assert msg.channel == "book"
        assert msg.data_type == "snapshot"
        assert len(msg.data) == 1

    def test_channel_data_trade(self) -> None:
        raw = orjson.dumps({
            "channel": "trade",
            "type": "update",
            "data": [{"price": "85000.0", "qty": "0.01"}],
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.CHANNEL_DATA
        assert msg.channel == "trade"
        assert msg.data_type == "update"

    def test_subscribe_success(self) -> None:
        raw = orjson.dumps({
            "method": "subscribe",
            "result": {"channel": "book", "symbol": "XBT/USD"},
            "success": True,
            "req_id": 1,
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.SUBSCRIBE_RESP
        assert msg.success is True
        assert msg.result["channel"] == "book"

    def test_subscribe_failure(self) -> None:
        raw = orjson.dumps({
            "method": "subscribe",
            "error": "Invalid channel",
            "success": False,
            "req_id": 1,
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.SUBSCRIBE_RESP
        assert msg.success is False
        assert msg.error == "Invalid channel"

    def test_add_order_response(self) -> None:
        raw = orjson.dumps({
            "method": "add_order",
            "result": {"order_id": "O123", "cl_ord_id": "my-id"},
            "success": True,
            "req_id": 5,
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.ADD_ORDER_RESP
        assert msg.success is True
        assert msg.result["order_id"] == "O123"

    def test_amend_order_response(self) -> None:
        raw = orjson.dumps({
            "method": "amend_order",
            "result": {"order_id": "O123"},
            "success": True,
            "req_id": 6,
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.AMEND_ORDER_RESP

    def test_cancel_order_response(self) -> None:
        raw = orjson.dumps({
            "method": "cancel_order",
            "result": {},
            "success": True,
            "req_id": 7,
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.CANCEL_ORDER_RESP

    def test_cancel_after_response(self) -> None:
        raw = orjson.dumps({
            "method": "cancel_after",
            "result": {"currentTime": "2025-01-01T00:00:00Z"},
            "success": True,
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.CANCEL_AFTER_RESP

    def test_status_message(self) -> None:
        raw = orjson.dumps({
            "channel": "status",
            "data": {"system": "online", "version": "2.0"},
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.STATUS

    def test_executions_channel(self) -> None:
        raw = orjson.dumps({
            "channel": "executions",
            "type": "update",
            "data": [{"exec_type": "trade", "order_id": "O123", "last_qty": "0.01"}],
        })
        msg = decode(raw)
        assert msg.msg_type == MessageType.CHANNEL_DATA
        assert msg.channel == "executions"
        assert msg.data[0]["exec_type"] == "trade"

    def test_invalid_json(self) -> None:
        msg = decode(b"not json")
        assert msg.msg_type == MessageType.ERROR

    def test_string_input(self) -> None:
        msg = decode('{"channel":"heartbeat"}')
        assert msg.msg_type == MessageType.HEARTBEAT


class TestEncode:
    def test_subscribe(self) -> None:
        frame = encode_subscribe("book", params={"symbol": ["XBT/USD"], "depth": 25}, req_id=1)
        obj = orjson.loads(frame)
        assert obj["method"] == "subscribe"
        assert obj["params"]["channel"] == "book"
        assert obj["params"]["symbol"] == ["XBT/USD"]
        assert obj["req_id"] == 1

    def test_add_order(self) -> None:
        frame = encode_add_order(
            "limit", "buy", "XBT/USD",
            price="85000.0", quantity="0.01",
            cl_ord_id="my-id", post_only=True, req_id=5,
        )
        obj = orjson.loads(frame)
        assert obj["method"] == "add_order"
        assert obj["params"]["limit_price"] == "85000.0"
        assert obj["params"]["order_qty"] == "0.01"
        assert obj["params"]["cl_ord_id"] == "my-id"
        assert obj["params"]["post_only"] is True
        assert obj["params"]["side"] == "buy"

    def test_amend_order(self) -> None:
        frame = encode_amend_order("O123", new_price="86000.0", req_id=6)
        obj = orjson.loads(frame)
        assert obj["method"] == "amend_order"
        assert obj["params"]["order_id"] == "O123"
        assert obj["params"]["limit_price"] == "86000.0"
        assert "order_qty" not in obj["params"]

    def test_amend_qty_only(self) -> None:
        frame = encode_amend_order("O123", new_qty="0.02")
        obj = orjson.loads(frame)
        assert obj["params"]["order_qty"] == "0.02"
        assert "limit_price" not in obj["params"]

    def test_cancel_single(self) -> None:
        frame = encode_cancel_order("O123", req_id=7)
        obj = orjson.loads(frame)
        assert obj["method"] == "cancel_order"
        assert obj["params"]["order_id"] == ["O123"]

    def test_cancel_multiple(self) -> None:
        frame = encode_cancel_order(["O1", "O2", "O3"])
        obj = orjson.loads(frame)
        assert obj["params"]["order_id"] == ["O1", "O2", "O3"]

    def test_cancel_all(self) -> None:
        frame = encode_cancel_all(req_id=8)
        obj = orjson.loads(frame)
        assert obj["method"] == "cancel_all"

    def test_cancel_after(self) -> None:
        frame = encode_cancel_after(60, req_id=9)
        obj = orjson.loads(frame)
        assert obj["method"] == "cancel_after"
        assert obj["params"]["timeout"] == 60

    def test_ping(self) -> None:
        frame = encode_ping(req_id=10)
        obj = orjson.loads(frame)
        assert obj["method"] == "ping"
