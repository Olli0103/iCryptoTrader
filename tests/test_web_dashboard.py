"""Tests for the Web Dashboard."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

from icryptotrader.web.dashboard import WebDashboard, _DecimalEncoder


class TestDecimalEncoder:
    def test_decimal_to_float(self) -> None:
        result = json.dumps({"val": Decimal("1.23")}, cls=_DecimalEncoder)
        assert '"val": 1.23' in result

    def test_nested_decimal(self) -> None:
        data = {"a": {"b": Decimal("99.99")}}
        result = json.loads(json.dumps(data, cls=_DecimalEncoder))
        assert result["a"]["b"] == 99.99


class TestWebDashboard:
    def test_construction(self) -> None:
        loop = MagicMock()
        dash = WebDashboard(
            strategy_loop=loop,
            host="127.0.0.1",
            port=8080,
        )
        assert dash._host == "127.0.0.1"
        assert dash._port == 8080

    def test_auth_check_no_auth(self) -> None:
        loop = MagicMock()
        dash = WebDashboard(
            strategy_loop=loop,
            username="admin",
            password="secret",
        )
        assert not dash._check_auth({})

    def test_auth_check_valid(self) -> None:
        import base64

        loop = MagicMock()
        dash = WebDashboard(
            strategy_loop=loop,
            username="admin",
            password="secret",
        )
        creds = base64.b64encode(b"admin:secret").decode()
        assert dash._check_auth({"authorization": f"Basic {creds}"})

    def test_auth_check_invalid(self) -> None:
        import base64

        loop = MagicMock()
        dash = WebDashboard(
            strategy_loop=loop,
            username="admin",
            password="secret",
        )
        creds = base64.b64encode(b"admin:wrong").decode()
        assert not dash._check_auth({"authorization": f"Basic {creds}"})

    def test_auth_check_malformed(self) -> None:
        loop = MagicMock()
        dash = WebDashboard(
            strategy_loop=loop,
            username="admin",
            password="secret",
        )
        assert not dash._check_auth({"authorization": "Bearer token"})
