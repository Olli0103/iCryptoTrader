"""Telegram notification service for operator alerts.

Sends notifications for fills, risk state changes, tax unlock countdowns,
and daily P&L summaries via the Telegram Bot API.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from decimal import Decimal

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


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

    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message. Returns True on success."""
        if not self._enabled or not self._bot_token or not self._chat_id:
            return False

        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }

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

    async def notify_fill(
        self,
        side: str,
        qty: Decimal,
        price: Decimal,
        order_id: str,
    ) -> None:
        """Notify about an order fill."""
        emoji = "BUY" if side == "buy" else "SELL"
        await self.send(
            f"<b>{emoji}</b> {qty} BTC @ ${price:,.1f}\n"
            f"Order: <code>{order_id[:12]}</code>"
        )

    async def notify_risk_state_change(
        self,
        old_state: str,
        new_state: str,
        drawdown_pct: float,
    ) -> None:
        """Notify about risk pause state changes."""
        await self.send(
            f"<b>RISK STATE</b>: {old_state} -> {new_state}\n"
            f"Drawdown: {drawdown_pct:.1%}"
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
                f"<b>TAX FREE</b>: Lot <code>{lot_id[:8]}</code> ({qty} BTC) "
                f"is now tax-free!"
            )
        else:
            await self.send(
                f"<b>TAX COUNTDOWN</b>: Lot <code>{lot_id[:8]}</code> "
                f"({qty} BTC) free in {days_until_free}d"
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
            f"<b>DAILY SUMMARY</b>\n"
            f"Portfolio: ${portfolio_usd:,.0f}\n"
            f"Drawdown: {drawdown_pct:.1%}\n"
            f"Fills: {fills_today}\n"
            f"P&L: ${profit_today_usd:,.2f}\n"
            f"Regime: {regime}"
        )

    async def close(self) -> None:
        """Close owned HTTP client."""
        if self._owns_client and self._client:
            await self._client.aclose()
            self._client = None
