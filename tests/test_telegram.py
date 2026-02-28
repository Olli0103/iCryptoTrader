"""Tests for Telegram notification service."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from icryptotrader.notify.telegram import TelegramNotifier


class TestTelegramNotifier:
    @pytest.fixture()
    def notifier(self) -> TelegramNotifier:
        return TelegramNotifier(bot_token="123:ABC", chat_id="456", enabled=True)

    def test_disabled_does_not_send(self) -> None:
        n = TelegramNotifier(bot_token="123:ABC", chat_id="456", enabled=False)
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(n.send("test"))
        assert result is False
        assert n.messages_sent == 0

    def test_missing_token_does_not_send(self) -> None:
        n = TelegramNotifier(bot_token="", chat_id="456", enabled=True)
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(n.send("test"))
        assert result is False

    async def test_send_success(self, notifier: TelegramNotifier) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        result = await notifier.send("Hello")
        assert result is True
        assert notifier.messages_sent == 1
        mock_client.post.assert_called_once()

        # Verify URL and payload
        call_args = mock_client.post.call_args
        assert "sendMessage" in call_args[0][0]
        assert call_args[1]["json"]["text"] == "Hello"
        assert call_args[1]["json"]["chat_id"] == "456"

    async def test_send_failure_increments_counter(self, notifier: TelegramNotifier) -> None:
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("network error")
        notifier._client = mock_client
        notifier._owns_client = False

        result = await notifier.send("Hello")
        assert result is False
        assert notifier.send_failures == 1

    async def test_notify_fill_formatting(self, notifier: TelegramNotifier) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        await notifier.notify_fill("buy", Decimal("0.01"), Decimal("85000"), "ORDER123456")
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "BUY" in text
        assert "0.01" in text
        assert "85,000" in text

    async def test_notify_risk_state_change(self, notifier: TelegramNotifier) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        await notifier.notify_risk_state_change("ACTIVE_TRADING", "RISK_PAUSE_ACTIVE", 0.15)
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "RISK STATE" in text
        assert "15.0%" in text

    async def test_notify_tax_unlock(self, notifier: TelegramNotifier) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        await notifier.notify_tax_unlock("lot-abc-123", Decimal("0.05"), 0)
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "TAX FREE" in text

        await notifier.notify_tax_unlock("lot-abc-123", Decimal("0.05"), 30)
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "30d" in text

    async def test_notify_daily_summary(self, notifier: TelegramNotifier) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        await notifier.notify_daily_summary(
            Decimal("5000"), 0.05, 3, Decimal("12.50"), "range_bound",
        )
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "DAILY SUMMARY" in text
        assert "5,000" in text
        assert "range_bound" in text
