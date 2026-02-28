"""Tests for Telegram bot — notifier + interactive bot."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from icryptotrader.notify.telegram import (
    BACK_BUTTON,
    LOTS_MENU,
    MAIN_MENU,
    PNL_MENU,
    TAX_MENU,
    BotSnapshot,
    TelegramBot,
    TelegramNotifier,
    _escape_html,
    _kb,
    _progress_bar,
)

# ---------------------------------------------------------------------------
# TelegramNotifier tests
# ---------------------------------------------------------------------------


class TestTelegramNotifier:
    @pytest.fixture()
    def notifier(self) -> TelegramNotifier:
        return TelegramNotifier(
            bot_token="123:ABC", chat_id="456", enabled=True,
        )

    def test_disabled_does_not_send(self) -> None:
        n = TelegramNotifier(
            bot_token="123:ABC", chat_id="456", enabled=False,
        )
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

        call_args = mock_client.post.call_args
        assert "sendMessage" in call_args[0][0]
        assert call_args[1]["json"]["text"] == "Hello"
        assert call_args[1]["json"]["chat_id"] == "456"

    async def test_send_with_reply_markup(
        self, notifier: TelegramNotifier,
    ) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        markup = _kb([[("Button", "cb:test")]])
        result = await notifier.send("Hello", reply_markup=markup)
        assert result is True

        payload = mock_client.post.call_args[1]["json"]
        assert "reply_markup" in payload
        assert payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "Button"

    async def test_send_failure_increments_counter(
        self, notifier: TelegramNotifier,
    ) -> None:
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("network error")
        notifier._client = mock_client
        notifier._owns_client = False

        result = await notifier.send("Hello")
        assert result is False
        assert notifier.send_failures == 1

    async def test_edit_message(self, notifier: TelegramNotifier) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        result = await notifier.edit_message(
            chat_id="456", message_id=100,
            text="Updated", reply_markup=BACK_BUTTON,
        )
        assert result is True
        url = mock_client.post.call_args[0][0]
        assert "editMessageText" in url

        payload = mock_client.post.call_args[1]["json"]
        assert payload["message_id"] == 100
        assert payload["text"] == "Updated"

    async def test_answer_callback(
        self, notifier: TelegramNotifier,
    ) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        await notifier.answer_callback("cq-123")
        url = mock_client.post.call_args[0][0]
        assert "answerCallbackQuery" in url
        payload = mock_client.post.call_args[1]["json"]
        assert payload["callback_query_id"] == "cq-123"

    async def test_notify_fill_formatting(
        self, notifier: TelegramNotifier,
    ) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        await notifier.notify_fill(
            "buy", Decimal("0.01"), Decimal("85000"), "ORDER123456",
        )
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "BUY" in text
        assert "0.01" in text
        assert "85,000" in text

    async def test_notify_risk_state_change(
        self, notifier: TelegramNotifier,
    ) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        await notifier.notify_risk_state_change(
            "ACTIVE_TRADING", "RISK_PAUSE_ACTIVE", 0.15,
        )
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "RISK STATE" in text
        assert "15.0%" in text

    async def test_notify_tax_unlock_free(
        self, notifier: TelegramNotifier,
    ) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        await notifier.notify_tax_unlock(
            "lot-abc-123", Decimal("0.05"), 0,
        )
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "TAX FREE" in text

    async def test_notify_tax_unlock_countdown(
        self, notifier: TelegramNotifier,
    ) -> None:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        notifier._client = mock_client
        notifier._owns_client = False

        await notifier.notify_tax_unlock(
            "lot-abc-123", Decimal("0.05"), 30,
        )
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "30d" in text

    async def test_notify_daily_summary(
        self, notifier: TelegramNotifier,
    ) -> None:
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


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_kb_single_row(self) -> None:
        result = _kb([[("A", "a"), ("B", "b")]])
        assert len(result["inline_keyboard"]) == 1
        assert result["inline_keyboard"][0][0]["text"] == "A"
        assert result["inline_keyboard"][0][0]["callback_data"] == "a"
        assert result["inline_keyboard"][0][1]["text"] == "B"

    def test_kb_multiple_rows(self) -> None:
        result = _kb([
            [("A", "a")],
            [("B", "b"), ("C", "c")],
        ])
        assert len(result["inline_keyboard"]) == 2
        assert len(result["inline_keyboard"][0]) == 1
        assert len(result["inline_keyboard"][1]) == 2

    def test_progress_bar_empty(self) -> None:
        bar = _progress_bar(0.0, 10)
        assert len(bar) == 10
        assert "\u2588" not in bar

    def test_progress_bar_full(self) -> None:
        bar = _progress_bar(1.0, 10)
        assert len(bar) == 10
        assert "\u2591" not in bar

    def test_progress_bar_half(self) -> None:
        bar = _progress_bar(0.5, 10)
        assert bar.count("\u2588") == 5
        assert bar.count("\u2591") == 5

    def test_escape_html(self) -> None:
        assert _escape_html("<b>test</b>") == "&lt;b&gt;test&lt;/b&gt;"
        assert _escape_html("A & B") == "A &amp; B"

    def test_main_menu_structure(self) -> None:
        kb = MAIN_MENU["inline_keyboard"]
        assert len(kb) == 3  # 3 rows
        assert len(kb[0]) == 2  # 2 buttons per row
        # Check callback data patterns
        all_data = [
            btn["callback_data"] for row in kb for btn in row
        ]
        assert "menu:status" in all_data
        assert "menu:pnl" in all_data
        assert "menu:lots" in all_data
        assert "menu:tax" in all_data
        assert "menu:ai" in all_data
        assert "menu:settings" in all_data


# ---------------------------------------------------------------------------
# BotSnapshot tests
# ---------------------------------------------------------------------------


class TestBotSnapshot:
    def test_default_values(self) -> None:
        snap = BotSnapshot()
        assert snap.portfolio_value_usd == Decimal("0")
        assert snap.regime == "range_bound"
        assert snap.pause_state == "ACTIVE_TRADING"
        assert snap.ai_direction == "NEUTRAL"

    def test_custom_values(self) -> None:
        snap = BotSnapshot(
            portfolio_value_usd=Decimal("50000"),
            btc_balance=Decimal("0.5"),
            regime="trending_up",
            drawdown_pct=0.05,
        )
        assert snap.portfolio_value_usd == Decimal("50000")
        assert snap.regime == "trending_up"


# ---------------------------------------------------------------------------
# TelegramBot tests
# ---------------------------------------------------------------------------


class TestTelegramBot:
    @pytest.fixture()
    def bot(self) -> TelegramBot:
        return TelegramBot(
            bot_token="123:ABC", chat_id="456", enabled=True,
        )

    @pytest.fixture()
    def bot_with_mock(self, bot: TelegramBot) -> TelegramBot:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp
        bot._notifier._client = mock_client
        bot._notifier._owns_client = False
        return bot

    def test_notifier_accessible(self, bot: TelegramBot) -> None:
        assert bot.notifier is not None
        assert bot.notifier._bot_token == "123:ABC"

    def test_set_data_provider(self, bot: TelegramBot) -> None:
        class MockProvider:
            def bot_snapshot(self) -> BotSnapshot:
                return BotSnapshot(
                    portfolio_value_usd=Decimal("10000"),
                )

        bot.set_data_provider(MockProvider())
        assert bot._data_provider is not None

    # -- Callback routing tests --

    def test_route_back_main(self, bot: TelegramBot) -> None:
        text, markup = bot._route_callback("back:main")
        assert "iCryptoTrader" in text
        assert markup == MAIN_MENU

    def test_route_menu_status(self, bot: TelegramBot) -> None:
        text, markup = bot._route_callback("menu:status")
        assert "Portfolio Status" in text
        assert markup == BACK_BUTTON

    def test_route_menu_pnl(self, bot: TelegramBot) -> None:
        text, markup = bot._route_callback("menu:pnl")
        assert "P&L" in text
        assert markup == PNL_MENU

    def test_route_menu_lots(self, bot: TelegramBot) -> None:
        text, markup = bot._route_callback("menu:lots")
        assert "FIFO Lots" in text
        assert markup == LOTS_MENU

    def test_route_menu_tax(self, bot: TelegramBot) -> None:
        text, markup = bot._route_callback("menu:tax")
        assert "Steuer" in text
        assert markup == TAX_MENU

    def test_route_menu_ai_disabled(self, bot: TelegramBot) -> None:
        text, markup = bot._route_callback("menu:ai")
        assert "deaktiviert" in text

    def test_route_menu_ai_with_provider(self, bot: TelegramBot) -> None:
        class MockProvider:
            def bot_snapshot(self) -> BotSnapshot:
                return BotSnapshot(
                    ai_provider="gemini",
                    ai_direction="BUY",
                    ai_confidence=0.8,
                    ai_call_count=5,
                )

        bot.set_data_provider(MockProvider())
        text, markup = bot._route_callback("menu:ai")
        assert "gemini" in text
        assert "BUY" in text
        assert "80%" in text

    def test_route_lots_table_no_provider(self, bot: TelegramBot) -> None:
        text, _markup = bot._route_callback("lots:table")
        assert "nicht verf" in text

    def test_route_lots_table_with_provider(self, bot: TelegramBot) -> None:
        bot.set_lot_viewer(lambda view: "Lot1  30d  0.01 BTC")
        text, _markup = bot._route_callback("lots:table")
        assert "Lot1" in text

    def test_route_lots_histogram(self, bot: TelegramBot) -> None:
        bot.set_lot_viewer(lambda view: "0-30d  |####| 0.05 BTC")
        text, _markup = bot._route_callback("lots:histogram")
        assert "0-30d" in text

    def test_route_lots_schedule(self, bot: TelegramBot) -> None:
        bot.set_lot_viewer(lambda view: "2026-03-01  30d")
        text, _markup = bot._route_callback("lots:schedule")
        assert "2026" in text

    def test_route_lots_summary(self, bot: TelegramBot) -> None:
        text, _markup = bot._route_callback("lots:summary")
        assert "Lot-Zusammenfassung" in text

    def test_route_pnl_daily(self, bot: TelegramBot) -> None:
        text, _markup = bot._route_callback("pnl:daily")
        assert "Tagesbilanz" in text

    def test_route_pnl_ytd(self, bot: TelegramBot) -> None:
        text, _markup = bot._route_callback("pnl:ytd")
        assert "YTD" in text
        assert "Freigrenze" in text

    def test_route_pnl_export(self, bot: TelegramBot) -> None:
        text, _markup = bot._route_callback("pnl:export")
        assert "Export" in text

    def test_route_tax_summary_no_provider(self, bot: TelegramBot) -> None:
        text, _markup = bot._route_callback("tax:summary")
        assert "Jahresbericht" in text

    def test_route_tax_summary_with_provider(
        self, bot: TelegramBot,
    ) -> None:
        bot.set_tax_report(lambda year: f"Report for {year}")
        text, _markup = bot._route_callback("tax:summary")
        import datetime
        current_year = datetime.datetime.now(datetime.UTC).year
        assert str(current_year) in text

    def test_route_tax_harvest_no_recs(self, bot: TelegramBot) -> None:
        bot.set_harvest_provider(lambda: [])
        text, _markup = bot._route_callback("tax:harvest")
        assert "Kein Harvesting" in text

    def test_route_tax_harvest_with_recs(self, bot: TelegramBot) -> None:
        from icryptotrader.types import HarvestRecommendation

        rec = HarvestRecommendation(
            lot_id="lot-abc-12345678",
            qty_btc=Decimal("0.01"),
            estimated_loss_eur=Decimal("-100"),
            current_price_usd=Decimal("80000"),
            cost_basis_per_btc_eur=Decimal("85000"),
            days_held=200,
            reason="offset_gains",
        )
        bot.set_harvest_provider(lambda: [rec])
        text, _markup = bot._route_callback("tax:harvest")
        assert "lot-abc-" in text
        assert "offset_gains" in text

    def test_route_tax_freigrenze(self, bot: TelegramBot) -> None:
        text, _markup = bot._route_callback("tax:freigrenze")
        assert "Freigrenze" in text

    def test_route_tax_freigrenze_exceeded(self, bot: TelegramBot) -> None:
        class MockProvider:
            def bot_snapshot(self) -> BotSnapshot:
                return BotSnapshot(
                    ytd_taxable_gain_eur=Decimal("1200"),
                )

        bot.set_data_provider(MockProvider())
        text, _markup = bot._route_callback("tax:freigrenze")
        assert "\u00dcberschritten" in text

    def test_route_settings_info(self, bot: TelegramBot) -> None:
        text, _markup = bot._route_callback("settings:info")
        assert "Bot Info" in text

    def test_route_unknown(self, bot: TelegramBot) -> None:
        text, markup = bot._route_callback("unknown:action")
        assert "Unbekannte" in text
        assert markup == BACK_BUTTON

    # -- Format tests with data provider --

    def test_status_format_active(self, bot: TelegramBot) -> None:
        class MockProvider:
            def bot_snapshot(self) -> BotSnapshot:
                return BotSnapshot(
                    portfolio_value_usd=Decimal("50000"),
                    btc_balance=Decimal("0.5"),
                    usd_balance=Decimal("7500"),
                    btc_allocation_pct=0.85,
                    drawdown_pct=0.03,
                    pause_state="ACTIVE_TRADING",
                    regime="range_bound",
                    ticks=1000,
                )

        bot.set_data_provider(MockProvider())
        text = bot._format_status()
        assert "50,000" in text
        assert "0.5" in text
        assert "range_bound" in text
        assert "\U0001f7e2" in text  # green circle for active

    def test_status_format_risk_pause(self, bot: TelegramBot) -> None:
        class MockProvider:
            def bot_snapshot(self) -> BotSnapshot:
                return BotSnapshot(pause_state="RISK_PAUSE_ACTIVE")

        bot.set_data_provider(MockProvider())
        text = bot._format_status()
        assert "\U0001f534" in text  # red circle for risk pause

    # -- Command handler tests --

    async def test_cmd_start(self, bot_with_mock: TelegramBot) -> None:
        await bot_with_mock._cmd_start()
        payload = bot_with_mock.notifier._client.post.call_args[1]["json"]
        assert "reply_markup" in payload
        assert payload["reply_markup"] == MAIN_MENU

    async def test_cmd_help(self, bot_with_mock: TelegramBot) -> None:
        await bot_with_mock._cmd_help()
        payload = bot_with_mock.notifier._client.post.call_args[1]["json"]
        text = payload["text"]
        assert "/start" in text
        assert "/status" in text
        assert "/lots" in text
        assert "Buttons" in text

    async def test_cmd_status(self, bot_with_mock: TelegramBot) -> None:
        await bot_with_mock._cmd_status()
        payload = bot_with_mock.notifier._client.post.call_args[1]["json"]
        assert "Portfolio Status" in payload["text"]

    # -- Update handling tests --

    async def test_handle_command_routing(
        self, bot_with_mock: TelegramBot,
    ) -> None:
        await bot_with_mock._handle_command("/status", "456")
        text = bot_with_mock.notifier._client.post.call_args[1]["json"]["text"]
        assert "Portfolio Status" in text

    async def test_handle_unknown_command(
        self, bot_with_mock: TelegramBot,
    ) -> None:
        await bot_with_mock._handle_command("/unknown", "456")
        text = bot_with_mock.notifier._client.post.call_args[1]["json"]["text"]
        assert "Unbekannter Befehl" in text

    async def test_handle_update_message(
        self, bot_with_mock: TelegramBot,
    ) -> None:
        update = {
            "message": {
                "text": "/start",
                "chat": {"id": 456},
            },
        }
        await bot_with_mock._handle_update(update)
        assert bot_with_mock.notifier._client.post.called

    async def test_handle_update_wrong_chat(
        self, bot_with_mock: TelegramBot,
    ) -> None:
        update = {
            "message": {
                "text": "/start",
                "chat": {"id": 999},
            },
        }
        await bot_with_mock._handle_update(update)
        # Should not have sent anything
        bot_with_mock.notifier._client.post.assert_not_called()

    async def test_handle_callback_query(
        self, bot_with_mock: TelegramBot,
    ) -> None:
        update = {
            "callback_query": {
                "id": "cq-123",
                "data": "menu:status",
                "message": {
                    "chat": {"id": 456},
                    "message_id": 100,
                },
            },
        }
        await bot_with_mock._handle_update(update)
        # Should have called answerCallbackQuery + editMessageText
        calls = bot_with_mock.notifier._client.post.call_args_list
        urls = [c[0][0] for c in calls]
        assert any("answerCallbackQuery" in u for u in urls)
        assert any("editMessageText" in u for u in urls)

    async def test_handle_callback_wrong_chat(
        self, bot_with_mock: TelegramBot,
    ) -> None:
        update = {
            "callback_query": {
                "id": "cq-123",
                "data": "menu:status",
                "message": {
                    "chat": {"id": 999},
                    "message_id": 100,
                },
            },
        }
        await bot_with_mock._handle_update(update)
        calls = bot_with_mock.notifier._client.post.call_args_list
        # Should only answer callback (required), not edit
        urls = [c[0][0] for c in calls]
        assert any("answerCallbackQuery" in u for u in urls)
        assert not any("editMessageText" in u for u in urls)
