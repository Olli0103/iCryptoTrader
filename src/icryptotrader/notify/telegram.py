"""Telegram Bot — interactive operator interface with inline keyboards.

Two-layer architecture:
  1. TelegramNotifier: push notifications (fills, risk alerts, tax unlocks)
  2. TelegramBot: interactive command/button handler with long polling

The bot provides:
  - Inline keyboard menus (no /commands needed, though supported)
  - Portfolio status, lot viewer, P&L, tax reports, AI signal status
  - Sub-menus with back navigation
  - Push notifications for trading events
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


# ---------------------------------------------------------------------------
# Data provider protocol — strategy loop implements this
# ---------------------------------------------------------------------------


class BotDataProvider(Protocol):
    """Protocol for querying current bot state."""

    def bot_snapshot(self) -> BotSnapshot: ...


@dataclass
class BotSnapshot:
    """Point-in-time snapshot of all bot state for Telegram display."""

    # Portfolio
    portfolio_value_usd: Decimal = Decimal("0")
    btc_balance: Decimal = Decimal("0")
    usd_balance: Decimal = Decimal("0")
    btc_allocation_pct: float = 0.0

    # Risk
    drawdown_pct: float = 0.0
    pause_state: str = "ACTIVE_TRADING"
    high_water_mark_usd: Decimal = Decimal("0")

    # Regime
    regime: str = "range_bound"

    # Grid
    active_orders: int = 0
    grid_levels: int = 0

    # Strategy loop stats
    ticks: int = 0
    commands_issued: int = 0
    last_tick_ms: float = 0.0
    uptime_sec: float = 0.0

    # Tax
    ytd_taxable_gain_eur: Decimal = Decimal("0")
    tax_free_btc: Decimal = Decimal("0")
    locked_btc: Decimal = Decimal("0")
    sellable_ratio: float = 0.0
    days_until_unlock: int | None = None
    open_lots: int = 0

    # AI
    ai_direction: str = "NEUTRAL"
    ai_confidence: float = 0.0
    ai_last_latency_ms: float = 0.0
    ai_provider: str = ""
    ai_call_count: int = 0
    ai_error_count: int = 0

    # Fills today
    fills_today: int = 0
    profit_today_usd: Decimal = Decimal("0")

    # EUR/USD
    eur_usd_rate: Decimal = Decimal("1.08")


# ---------------------------------------------------------------------------
# Inline keyboard builder
# ---------------------------------------------------------------------------


def _kb(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """Build an inline_keyboard reply_markup from (text, callback_data) tuples."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# TelegramNotifier — push notifications (one-way)
# ---------------------------------------------------------------------------


class TelegramNotifier:
    """Sends Telegram messages via the Bot API.

    Usage:
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        await notifier.send("Hello from iCryptoTrader!")
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        enabled: bool = True,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._enabled = enabled
        self._client = http_client
        self._owns_client = http_client is None
        self.messages_sent: int = 0
        self.send_failures: int = 0

    @property
    def base_url(self) -> str:
        return f"{TELEGRAM_API}/bot{self._bot_token}"

    async def send(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """Send a message. Returns True on success."""
        if not self._enabled or not self._bot_token or not self._chat_id:
            return False

        url = f"{self.base_url}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            client = self._client or httpx.AsyncClient(timeout=10.0)
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                self.messages_sent += 1
                return True
            finally:
                if self._client is None:
                    await client.aclose()
        except Exception:
            self.send_failures += 1
            logger.warning("Telegram send failed", exc_info=True)
            return False

    async def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """Edit an existing message (used for button responses)."""
        if not self._enabled or not self._bot_token:
            return False

        url = f"{self.base_url}/editMessageText"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            client = self._client or httpx.AsyncClient(timeout=10.0)
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return True
            finally:
                if self._client is None:
                    await client.aclose()
        except Exception:
            logger.warning("Telegram edit failed", exc_info=True)
            return False

    async def answer_callback(self, callback_query_id: str) -> None:
        """Acknowledge a callback query (dismiss loading indicator)."""
        if not self._enabled or not self._bot_token:
            return

        url = f"{self.base_url}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}

        try:
            client = self._client or httpx.AsyncClient(timeout=10.0)
            try:
                await client.post(url, json=payload)
            finally:
                if self._client is None:
                    await client.aclose()
        except Exception:
            logger.warning("Telegram answerCallback failed", exc_info=True)

    # -- Push notification helpers --

    async def notify_fill(
        self,
        side: str,
        qty: Decimal,
        price: Decimal,
        order_id: str,
    ) -> None:
        """Notify about an order fill."""
        emoji = "\U0001f7e2" if side == "buy" else "\U0001f534"
        label = "BUY" if side == "buy" else "SELL"
        await self.send(
            f"{emoji} <b>{label}</b> {qty} BTC @ ${price:,.1f}\n"
            f"Order: <code>{order_id[:12]}</code>",
        )

    async def notify_risk_state_change(
        self,
        old_state: str,
        new_state: str,
        drawdown_pct: float,
    ) -> None:
        """Notify about risk pause state changes."""
        await self.send(
            f"\u26a0\ufe0f <b>RISK STATE</b>: {old_state} \u2192 {new_state}\n"
            f"Drawdown: {drawdown_pct:.1%}",
        )

    async def notify_tax_unlock(
        self,
        lot_id: str,
        qty: Decimal,
        days_until_free: int,
    ) -> None:
        """Notify about lots approaching tax-free maturity."""
        if days_until_free == 0:
            await self.send(
                f"\u2705 <b>TAX FREE</b>: Lot <code>{lot_id[:8]}</code>"
                f" ({qty} BTC) is now tax-free!",
            )
        else:
            await self.send(
                f"\u23f3 <b>TAX COUNTDOWN</b>: Lot <code>{lot_id[:8]}"
                f"</code> ({qty} BTC) free in {days_until_free}d",
            )

    async def notify_daily_summary(
        self,
        portfolio_usd: Decimal,
        drawdown_pct: float,
        fills_today: int,
        profit_today_usd: Decimal,
        regime: str,
    ) -> None:
        """Send daily P&L summary."""
        await self.send(
            f"\U0001f4ca <b>DAILY SUMMARY</b>\n"
            f"Portfolio: ${portfolio_usd:,.0f}\n"
            f"Drawdown: {drawdown_pct:.1%}\n"
            f"Fills: {fills_today}\n"
            f"P&L: ${profit_today_usd:,.2f}\n"
            f"Regime: {regime}",
        )

    async def close(self) -> None:
        """Close owned HTTP client."""
        if self._owns_client and self._client:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# TelegramBot — interactive handler with inline keyboards
# ---------------------------------------------------------------------------

# Menu layouts
MAIN_MENU = _kb([
    [("\U0001f4ca Status", "menu:status"), ("\U0001f4c8 P&L", "menu:pnl")],
    [("\U0001f4b0 Lots", "menu:lots"), ("\U0001f4cb Tax", "menu:tax")],
    [("\U0001f916 AI Signal", "menu:ai"), ("\u2699\ufe0f Settings", "menu:settings")],
])

LOTS_MENU = _kb([
    [("\U0001f4cb Lot-Tabelle", "lots:table")],
    [("\U0001f4ca Altersverteilung", "lots:histogram")],
    [("\U0001f513 Unlock-Zeitplan", "lots:schedule")],
    [("\U0001f4dd Zusammenfassung", "lots:summary")],
    [("\u25c0\ufe0f Zur\u00fcck", "back:main")],
])

PNL_MENU = _kb([
    [("\U0001f4c5 Heute", "pnl:daily")],
    [("\U0001f4c6 YTD Steuer", "pnl:ytd")],
    [("\U0001f4e5 Export CSV", "pnl:export")],
    [("\u25c0\ufe0f Zur\u00fcck", "back:main")],
])

TAX_MENU = _kb([
    [("\U0001f4ca Jahresbericht", "tax:summary")],
    [("\U0001f33e Harvest Empfehlung", "tax:harvest")],
    [("\U0001f512 Freigrenze Status", "tax:freigrenze")],
    [("\u25c0\ufe0f Zur\u00fcck", "back:main")],
])

SETTINGS_MENU = _kb([
    [("\U0001f50d Bot-Info", "settings:info")],
    [("\u25c0\ufe0f Zur\u00fcck", "back:main")],
])

BACK_BUTTON = _kb([[("\u25c0\ufe0f Zur\u00fcck", "back:main")]])


@dataclass
class TelegramBot:
    """Interactive Telegram bot with inline keyboard navigation.

    Usage:
        bot = TelegramBot(
            bot_token="123:ABC",
            chat_id="456",
        )
        bot.set_data_provider(strategy_loop)
        await bot.start()   # starts long-polling in background
        await bot.stop()     # clean shutdown
    """

    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = True
    poll_interval_sec: float = 1.0
    _notifier: TelegramNotifier = field(init=False, repr=False)
    _data_provider: BotDataProvider | None = field(
        init=False, default=None, repr=False,
    )
    _poll_task: asyncio.Task[None] | None = field(
        init=False, default=None, repr=False,
    )
    _running: bool = field(init=False, default=False, repr=False)
    _last_update_id: int = field(init=False, default=0, repr=False)
    _start_time: float = field(init=False, default_factory=time.time, repr=False)
    # Extra data providers (set by lifecycle/strategy)
    _lot_viewer_fn: Any = field(init=False, default=None, repr=False)
    _tax_report_fn: Any = field(init=False, default=None, repr=False)
    _harvest_fn: Any = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._notifier = TelegramNotifier(
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            enabled=self.enabled,
        )

    @property
    def notifier(self) -> TelegramNotifier:
        """Access the underlying notifier for push notifications."""
        return self._notifier

    def set_data_provider(self, provider: BotDataProvider) -> None:
        """Set the data source for interactive queries."""
        self._data_provider = provider

    def set_lot_viewer(self, fn: Any) -> None:
        """Set lot viewer callable: () -> str."""
        self._lot_viewer_fn = fn

    def set_tax_report(self, fn: Any) -> None:
        """Set tax report callable: (year) -> str."""
        self._tax_report_fn = fn

    def set_harvest_provider(self, fn: Any) -> None:
        """Set harvest recommendation callable: () -> list."""
        self._harvest_fn = fn

    async def start(self) -> None:
        """Start the long-polling loop in the background."""
        if not self.enabled or not self.bot_token:
            logger.info("Telegram bot disabled or no token, skipping start")
            return

        self._running = True
        self._start_time = time.time()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Telegram bot started (polling)")

        # Send startup message with main menu
        await self._notifier.send(
            "\U0001f680 <b>iCryptoTrader gestartet</b>\n\n"
            "W\u00e4hle eine Option:",
            reply_markup=MAIN_MENU,
        )

    async def stop(self) -> None:
        """Stop polling and clean up."""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task

        await self._notifier.send(
            "\U0001f6d1 <b>iCryptoTrader gestoppt</b>",
        )
        await self._notifier.close()
        logger.info("Telegram bot stopped")

    # -- Long polling --

    async def _poll_loop(self) -> None:
        """Poll getUpdates in a loop."""
        async with httpx.AsyncClient(timeout=35.0) as client:
            self._notifier._client = client
            self._notifier._owns_client = False

            while self._running:
                try:
                    await self._poll_once(client)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.warning(
                        "Telegram poll error", exc_info=True,
                    )
                    await asyncio.sleep(5.0)

            self._notifier._client = None
            self._notifier._owns_client = True

    async def _poll_once(self, client: httpx.AsyncClient) -> None:
        """Single poll iteration with long polling."""
        url = f"{self._notifier.base_url}/getUpdates"
        params: dict[str, Any] = {
            "timeout": 30,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._last_update_id > 0:
            params["offset"] = self._last_update_id + 1

        resp = await client.post(url, json=params)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("ok"):
            return

        for update in data.get("result", []):
            update_id = update.get("update_id", 0)
            if update_id > self._last_update_id:
                self._last_update_id = update_id

            await self._handle_update(update)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        """Route an update to the appropriate handler."""
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
        elif "message" in update:
            msg = update["message"]
            text = msg.get("text", "")
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # Only respond to authorized chat
            if chat_id != self.chat_id:
                return

            if text.startswith("/"):
                await self._handle_command(text, chat_id)

    # -- Command handlers --

    async def _handle_command(self, text: str, chat_id: str) -> None:
        """Handle /command messages."""
        cmd = text.split()[0].lower()

        handlers: dict[str, Any] = {
            "/start": self._cmd_start,
            "/menu": self._cmd_start,
            "/status": self._cmd_status,
            "/lots": self._cmd_lots_menu,
            "/pnl": self._cmd_pnl_menu,
            "/tax": self._cmd_tax_menu,
            "/ai": self._cmd_ai,
            "/help": self._cmd_help,
        }

        handler = handlers.get(cmd)
        if handler:
            await handler()
        else:
            await self._notifier.send(
                "Unbekannter Befehl. Tippe /help f\u00fcr Hilfe.",
            )

    async def _cmd_start(self) -> None:
        await self._notifier.send(
            "\U0001f4b9 <b>iCryptoTrader</b>\n\n"
            "W\u00e4hle eine Option:",
            reply_markup=MAIN_MENU,
        )

    async def _cmd_help(self) -> None:
        await self._notifier.send(
            "<b>Verf\u00fcgbare Befehle</b>\n\n"
            "/start \u2014 Hauptmen\u00fc\n"
            "/status \u2014 Portfolio-Status\n"
            "/lots \u2014 FIFO-Lots anzeigen\n"
            "/pnl \u2014 Gewinn/Verlust\n"
            "/tax \u2014 Steuerbericht\n"
            "/ai \u2014 AI-Signal Status\n"
            "/help \u2014 Diese Hilfe\n\n"
            "<i>Oder nutze einfach die Buttons!</i>",
            reply_markup=MAIN_MENU,
        )

    async def _cmd_status(self) -> None:
        text = self._format_status()
        await self._notifier.send(text, reply_markup=BACK_BUTTON)

    async def _cmd_lots_menu(self) -> None:
        await self._notifier.send(
            "\U0001f4b0 <b>FIFO Lots</b>\n\n"
            "W\u00e4hle eine Ansicht:",
            reply_markup=LOTS_MENU,
        )

    async def _cmd_pnl_menu(self) -> None:
        await self._notifier.send(
            "\U0001f4c8 <b>P&L Reports</b>\n\n"
            "W\u00e4hle einen Zeitraum:",
            reply_markup=PNL_MENU,
        )

    async def _cmd_tax_menu(self) -> None:
        await self._notifier.send(
            "\U0001f4cb <b>Steuer</b>\n\n"
            "W\u00e4hle eine Option:",
            reply_markup=TAX_MENU,
        )

    async def _cmd_ai(self) -> None:
        text = self._format_ai()
        await self._notifier.send(text, reply_markup=BACK_BUTTON)

    # -- Callback query handler --

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        """Handle inline keyboard button presses."""
        cq_id = cq.get("id", "")
        data = cq.get("data", "")
        msg = cq.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        message_id = msg.get("message_id", 0)

        # Only respond to authorized chat
        if chat_id != self.chat_id:
            await self._notifier.answer_callback(cq_id)
            return

        # Acknowledge immediately (dismisses loading spinner)
        await self._notifier.answer_callback(cq_id)

        # Build response based on callback data
        text, markup = self._route_callback(data)

        if text:
            await self._notifier.edit_message(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=markup,
            )

    def _route_callback(
        self, data: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Map callback data to (text, reply_markup)."""
        # Main menu navigation
        if data == "back:main":
            return (
                "\U0001f4b9 <b>iCryptoTrader</b>\n\n"
                "W\u00e4hle eine Option:",
                MAIN_MENU,
            )

        # Status
        if data == "menu:status":
            return self._format_status(), BACK_BUTTON

        # P&L menu
        if data == "menu:pnl":
            return (
                "\U0001f4c8 <b>P&L Reports</b>\n\n"
                "W\u00e4hle einen Zeitraum:",
                PNL_MENU,
            )

        # Lots menu
        if data == "menu:lots":
            return (
                "\U0001f4b0 <b>FIFO Lots</b>\n\n"
                "W\u00e4hle eine Ansicht:",
                LOTS_MENU,
            )

        # Tax menu
        if data == "menu:tax":
            return (
                "\U0001f4cb <b>Steuer</b>\n\n"
                "W\u00e4hle eine Option:",
                TAX_MENU,
            )

        # Settings menu
        if data == "menu:settings":
            return (
                "\u2699\ufe0f <b>Einstellungen</b>\n\n"
                "W\u00e4hle eine Option:",
                SETTINGS_MENU,
            )

        # AI Signal
        if data == "menu:ai":
            return self._format_ai(), BACK_BUTTON

        # Lot sub-views
        if data == "lots:table":
            return self._format_lots_table(), _kb(
                [[("\u25c0\ufe0f Lots", "menu:lots")]],
            )
        if data == "lots:histogram":
            return self._format_lots_histogram(), _kb(
                [[("\u25c0\ufe0f Lots", "menu:lots")]],
            )
        if data == "lots:schedule":
            return self._format_lots_schedule(), _kb(
                [[("\u25c0\ufe0f Lots", "menu:lots")]],
            )
        if data == "lots:summary":
            return self._format_lots_summary(), _kb(
                [[("\u25c0\ufe0f Lots", "menu:lots")]],
            )

        # P&L sub-views
        if data == "pnl:daily":
            return self._format_pnl_daily(), _kb(
                [[("\u25c0\ufe0f P&L", "menu:pnl")]],
            )
        if data == "pnl:ytd":
            return self._format_pnl_ytd(), _kb(
                [[("\u25c0\ufe0f P&L", "menu:pnl")]],
            )
        if data == "pnl:export":
            return self._format_pnl_export(), _kb(
                [[("\u25c0\ufe0f P&L", "menu:pnl")]],
            )

        # Tax sub-views
        if data == "tax:summary":
            return self._format_tax_summary(), _kb(
                [[("\u25c0\ufe0f Steuer", "menu:tax")]],
            )
        if data == "tax:harvest":
            return self._format_tax_harvest(), _kb(
                [[("\u25c0\ufe0f Steuer", "menu:tax")]],
            )
        if data == "tax:freigrenze":
            return self._format_tax_freigrenze(), _kb(
                [[("\u25c0\ufe0f Steuer", "menu:tax")]],
            )

        # Settings sub-views
        if data == "settings:info":
            return self._format_settings_info(), _kb(
                [[("\u25c0\ufe0f Einstellungen", "menu:settings")]],
            )

        return "Unbekannte Aktion.", BACK_BUTTON

    # -- Formatters --

    def _snap(self) -> BotSnapshot:
        """Get current snapshot or return defaults."""
        if self._data_provider:
            return self._data_provider.bot_snapshot()
        return BotSnapshot()

    def _format_status(self) -> str:
        s = self._snap()
        pause_icon = {
            "ACTIVE_TRADING": "\U0001f7e2",
            "TAX_LOCK_ACTIVE": "\U0001f7e1",
            "RISK_PAUSE_ACTIVE": "\U0001f534",
            "DUAL_LOCK": "\U0001f6d1",
            "EMERGENCY_SELL": "\u203c\ufe0f",
        }.get(s.pause_state, "\u2753")

        return (
            f"\U0001f4ca <b>Portfolio Status</b>\n"
            f"\n"
            f"<b>Portfolio</b>\n"
            f"  Wert:         ${s.portfolio_value_usd:,.0f}\n"
            f"  BTC:          {s.btc_balance:.8f}\n"
            f"  USD:          ${s.usd_balance:,.0f}\n"
            f"  Allokation:   {s.btc_allocation_pct:.1%} BTC\n"
            f"\n"
            f"<b>Risk</b>\n"
            f"  {pause_icon} Status: {s.pause_state}\n"
            f"  Drawdown:     {s.drawdown_pct:.1%}\n"
            f"  HWM:          ${s.high_water_mark_usd:,.0f}\n"
            f"\n"
            f"<b>Trading</b>\n"
            f"  Regime:       {s.regime}\n"
            f"  Orders:       {s.active_orders}/{s.grid_levels}\n"
            f"  Ticks:        {s.ticks:,}\n"
            f"  Commands:     {s.commands_issued:,}\n"
            f"  Tick-Latenz:  {s.last_tick_ms:.1f}ms\n"
        )

    def _format_ai(self) -> str:
        s = self._snap()
        if not s.ai_provider:
            return (
                "\U0001f916 <b>AI Signal</b>\n\n"
                "<i>AI Signal Engine ist deaktiviert.</i>"
            )

        dir_icon = {
            "STRONG_BUY": "\u2b06\ufe0f\u2b06\ufe0f",
            "BUY": "\u2b06\ufe0f",
            "NEUTRAL": "\u27a1\ufe0f",
            "SELL": "\u2b07\ufe0f",
            "STRONG_SELL": "\u2b07\ufe0f\u2b07\ufe0f",
        }.get(s.ai_direction, "\u2753")

        conf_bar = _progress_bar(s.ai_confidence, 10)

        return (
            f"\U0001f916 <b>AI Signal</b>\n"
            f"\n"
            f"  {dir_icon} Richtung:  {s.ai_direction}\n"
            f"  Konfidenz:   {conf_bar} {s.ai_confidence:.0%}\n"
            f"  Provider:    {s.ai_provider}\n"
            f"  Latenz:      {s.ai_last_latency_ms:.0f}ms\n"
            f"  Aufrufe:     {s.ai_call_count}\n"
            f"  Fehler:      {s.ai_error_count}\n"
        )

    def _format_lots_table(self) -> str:
        if self._lot_viewer_fn:
            try:
                table = self._lot_viewer_fn("table")
                return f"<pre>{_escape_html(table)}</pre>"
            except Exception:
                logger.warning("Lot table error", exc_info=True)
        return "<i>Lot-Daten nicht verf\u00fcgbar.</i>"

    def _format_lots_histogram(self) -> str:
        if self._lot_viewer_fn:
            try:
                hist = self._lot_viewer_fn("histogram")
                return f"<pre>{_escape_html(hist)}</pre>"
            except Exception:
                logger.warning("Lot histogram error", exc_info=True)
        return "<i>Lot-Daten nicht verf\u00fcgbar.</i>"

    def _format_lots_schedule(self) -> str:
        if self._lot_viewer_fn:
            try:
                sched = self._lot_viewer_fn("schedule")
                return f"<pre>{_escape_html(sched)}</pre>"
            except Exception:
                logger.warning("Lot schedule error", exc_info=True)
        return "<i>Lot-Daten nicht verf\u00fcgbar.</i>"

    def _format_lots_summary(self) -> str:
        s = self._snap()
        unlock_text = (
            f"{s.days_until_unlock}d" if s.days_until_unlock is not None
            else "N/A"
        )
        free_bar = _progress_bar(s.sellable_ratio, 10)

        return (
            f"\U0001f4dd <b>Lot-Zusammenfassung</b>\n"
            f"\n"
            f"  Offene Lots:     {s.open_lots}\n"
            f"  Gesamt BTC:      {s.btc_balance:.8f}\n"
            f"  Steuerfrei:      {s.tax_free_btc:.8f}\n"
            f"  Gesperrt:        {s.locked_btc:.8f}\n"
            f"  Ratio:           {free_bar} {s.sellable_ratio:.0%}\n"
            f"  N\u00e4chster Unlock: {unlock_text}\n"
            f"  YTD Steuergewinn: \u20ac{s.ytd_taxable_gain_eur:,.2f}\n"
        )

    def _format_pnl_daily(self) -> str:
        s = self._snap()
        pnl_icon = "\U0001f4b0" if s.profit_today_usd >= 0 else "\U0001f4c9"
        return (
            f"\U0001f4c5 <b>Tagesbilanz</b>\n"
            f"\n"
            f"  {pnl_icon} P&L: ${s.profit_today_usd:,.2f}\n"
            f"  Fills:   {s.fills_today}\n"
            f"  Regime:  {s.regime}\n"
            f"  DD:      {s.drawdown_pct:.1%}\n"
        )

    def _format_pnl_ytd(self) -> str:
        s = self._snap()
        freigrenze = Decimal("1000")
        remaining = freigrenze - s.ytd_taxable_gain_eur
        status = "\u2705" if remaining > 0 else "\u26a0\ufe0f"
        pct_used = float(s.ytd_taxable_gain_eur / freigrenze) if freigrenze else 0
        bar = _progress_bar(min(pct_used, 1.0), 10)

        return (
            f"\U0001f4c6 <b>YTD Steuerstatus</b>\n"
            f"\n"
            f"  Steuerpflichtig: \u20ac{s.ytd_taxable_gain_eur:,.2f}\n"
            f"  Freigrenze:      \u20ac{freigrenze:,.0f}\n"
            f"  Verbraucht:      {bar} {pct_used:.0%}\n"
            f"  Verbleibend:     {status} \u20ac{remaining:,.2f}\n"
            f"  EUR/USD:         {s.eur_usd_rate}\n"
        )

    def _format_pnl_export(self) -> str:
        return (
            "\U0001f4e5 <b>CSV Export</b>\n\n"
            "<i>Export wird \u00fcber die CLI ausgel\u00f6st:\n"
            "<code>icryptotrader export --year 2025</code></i>"
        )

    def _format_tax_summary(self) -> str:
        if self._tax_report_fn:
            try:
                import datetime
                year = datetime.datetime.now(datetime.UTC).year
                report = self._tax_report_fn(year)
                return f"<pre>{_escape_html(report)}</pre>"
            except Exception:
                logger.warning("Tax report error", exc_info=True)

        s = self._snap()
        return (
            f"\U0001f4ca <b>Jahresbericht</b>\n\n"
            f"  YTD Gewinn: \u20ac{s.ytd_taxable_gain_eur:,.2f}\n"
            f"  Steuerfrei: {s.tax_free_btc:.8f} BTC\n"
            f"  Gesperrt:   {s.locked_btc:.8f} BTC\n"
        )

    def _format_tax_harvest(self) -> str:
        if self._harvest_fn:
            try:
                recs = self._harvest_fn()
                if not recs:
                    return (
                        "\U0001f33e <b>Harvest Empfehlung</b>\n\n"
                        "\u2705 Kein Harvesting empfohlen.\n"
                        "<i>Entweder keine YTD-Gewinne oder "
                        "keine Underwater-Lots.</i>"
                    )
                lines = ["\U0001f33e <b>Harvest Empfehlung</b>\n"]
                for r in recs:
                    lines.append(
                        f"\n  Lot: <code>{r.lot_id[:8]}</code>\n"
                        f"  Menge: {r.qty_btc:.8f} BTC\n"
                        f"  Gesch. Verlust: \u20ac{r.estimated_loss_eur:,.2f}\n"
                        f"  Haltedauer: {r.days_held}d\n"
                        f"  Grund: {r.reason}\n"
                    )
                return "\n".join(lines)
            except Exception:
                logger.warning("Harvest error", exc_info=True)

        return (
            "\U0001f33e <b>Harvest Empfehlung</b>\n\n"
            "<i>Harvest-Daten nicht verf\u00fcgbar.</i>"
        )

    def _format_tax_freigrenze(self) -> str:
        s = self._snap()
        freigrenze = Decimal("1000")
        remaining = freigrenze - s.ytd_taxable_gain_eur
        pct = float(s.ytd_taxable_gain_eur / freigrenze) if freigrenze else 0
        bar = _progress_bar(min(pct, 1.0), 20)

        if remaining > freigrenze * Decimal("0.5"):
            status = "\U0001f7e2 Komfortabel"
        elif remaining > 0:
            status = "\U0001f7e1 Aufpassen"
        else:
            status = "\U0001f534 \u00dcberschritten!"

        return (
            f"\U0001f512 <b>Freigrenze Status</b>\n"
            f"\n"
            f"  {bar}\n"
            f"  \u20ac{s.ytd_taxable_gain_eur:,.2f} / "
            f"\u20ac{freigrenze:,.0f}\n"
            f"\n"
            f"  Status: {status}\n"
            f"  Verbleibend: \u20ac{remaining:,.2f}\n"
        )

    def _format_settings_info(self) -> str:
        s = self._snap()
        uptime_h = s.uptime_sec / 3600 if s.uptime_sec else 0

        return (
            f"\U0001f50d <b>Bot Info</b>\n"
            f"\n"
            f"  Uptime:     {uptime_h:.1f}h\n"
            f"  Ticks:      {s.ticks:,}\n"
            f"  Commands:   {s.commands_issued:,}\n"
            f"  Fills:      {s.fills_today}\n"
            f"  Regime:     {s.regime}\n"
            f"  Pause:      {s.pause_state}\n"
            f"  EUR/USD:    {s.eur_usd_rate}\n"
            f"  AI:         {s.ai_provider or 'disabled'}\n"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _progress_bar(ratio: float, width: int = 10) -> str:
    """Render a progress bar using unicode block characters."""
    filled = int(ratio * width)
    empty = width - filled
    return "\u2588" * filled + "\u2591" * empty


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
