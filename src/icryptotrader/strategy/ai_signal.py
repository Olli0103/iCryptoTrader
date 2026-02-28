"""AI Signal Engine — multi-provider LLM-based market signal generation.

Provides a flexible, async signal engine that queries an LLM (Gemini, Anthropic,
or OpenAI) for market regime and directional signals. Designed to be a
secondary alpha source alongside the grid engine.

Supports:
  - Google Gemini (default, via google-genai SDK)
  - Anthropic Claude (via anthropic SDK)
  - OpenAI-compatible APIs (via httpx)

The engine is:
  - Async-first: non-blocking HTTP calls with configurable timeouts
  - Rate-limited: cooldown between AI calls to avoid quota exhaustion
  - Fail-open: if the AI provider is down, returns a neutral signal
  - Auditable: logs every signal with reasoning for post-hoc review
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Any

logger = logging.getLogger(__name__)


class SignalDirection(Enum):
    """Directional bias from AI signal."""

    STRONG_BUY = auto()
    BUY = auto()
    NEUTRAL = auto()
    SELL = auto()
    STRONG_SELL = auto()


@dataclass
class AISignal:
    """Output from the AI Signal Engine."""

    direction: SignalDirection = SignalDirection.NEUTRAL
    confidence: float = 0.0  # 0.0 to 1.0
    reasoning: str = ""
    suggested_bias_bps: Decimal = Decimal("0")  # Skew to apply to grid
    regime_hint: str = ""  # Optional regime suggestion
    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    error: str = ""  # Non-empty if call failed


class AISignalEngine:
    """Async AI signal generator with multi-provider support.

    Usage:
        engine = AISignalEngine(
            provider="gemini",
            api_key="...",
            model="gemini-2.0-flash",
        )
        signal = await engine.generate_signal(market_context)
    """

    def __init__(
        self,
        provider: str = "gemini",
        api_key: str = "",
        model: str = "gemini-2.0-flash",
        temperature: float = 0.2,
        max_tokens: int = 512,
        cooldown_sec: int = 300,
        weight: float = 0.3,
        timeout_sec: int = 10,
    ) -> None:
        self._provider = provider
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._cooldown_sec = cooldown_sec
        self._weight = weight
        self._timeout_sec = timeout_sec

        self._last_call_time: float = 0.0
        self._last_signal: AISignal = AISignal()
        self._call_count: int = 0
        self._error_count: int = 0

    @property
    def weight(self) -> float:
        return self._weight

    @property
    def last_signal(self) -> AISignal:
        return self._last_signal

    @property
    def is_ready(self) -> bool:
        """True if enough time has passed since last call."""
        return (time.time() - self._last_call_time) >= self._cooldown_sec

    async def generate_signal(
        self,
        market_context: dict[str, Any],
    ) -> AISignal:
        """Generate a trading signal from market context.

        Args:
            market_context: Dict with keys like:
                - mid_price: Current BTC price
                - spread_bps: Current spread
                - volatility_pct: Recent volatility
                - regime: Current regime classification
                - btc_allocation_pct: Current BTC allocation
                - drawdown_pct: Current drawdown
                - price_change_1h_pct: 1h price change
                - price_change_24h_pct: 24h price change
                - book_imbalance: Order book imbalance (-1 to 1)
                - ytd_taxable_gain_eur: Year-to-date taxable gain

        Returns:
            AISignal with direction, confidence, and optional bias.
        """
        if not self._api_key:
            return AISignal(error="no_api_key")

        if not self.is_ready:
            return self._last_signal

        start = time.monotonic()
        prompt = self._build_prompt(market_context)

        try:
            response_text = await self._call_provider(prompt)
            signal = self._parse_response(response_text)
            signal.latency_ms = (time.monotonic() - start) * 1000
            signal.provider = self._provider
            signal.model = self._model
            signal.timestamp = time.time()

            self._last_signal = signal
            self._last_call_time = time.time()
            self._call_count += 1

            logger.info(
                "AI signal: %s (confidence=%.2f, bias=%s bps, latency=%.0fms) — %s",
                signal.direction.name, signal.confidence,
                signal.suggested_bias_bps, signal.latency_ms,
                signal.reasoning[:100],
            )
            return signal

        except Exception:
            self._error_count += 1
            logger.exception("AI signal generation failed (provider=%s)", self._provider)
            return AISignal(
                error="provider_error",
                latency_ms=(time.monotonic() - start) * 1000,
                provider=self._provider,
                model=self._model,
            )

    async def _call_provider(self, prompt: str) -> str:
        """Route to the appropriate provider backend."""
        if self._provider == "gemini":
            return await self._call_gemini(prompt)
        if self._provider == "anthropic":
            return await self._call_anthropic(prompt)
        if self._provider == "openai":
            return await self._call_openai(prompt)
        raise ValueError(f"Unknown AI provider: {self._provider}")

    async def _call_gemini(self, prompt: str) -> str:
        """Call Google Gemini API via httpx (no SDK dependency required)."""
        import httpx

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:generateContent?key={self._api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_tokens,
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return parts[0].get("text", "") if parts else ""

    async def _call_anthropic(self, prompt: str) -> str:
        """Call Anthropic Claude API via httpx."""
        import httpx

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        content = data.get("content", [])
        return content[0].get("text", "") if content else ""

    async def _call_openai(self, prompt: str) -> str:
        """Call OpenAI-compatible API via httpx."""
        import httpx

        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        choices = data.get("choices", [])
        return choices[0].get("message", {}).get("content", "") if choices else ""

    def _build_prompt(self, ctx: dict[str, Any]) -> str:
        """Build a structured market analysis prompt."""
        return f"""You are a quantitative trading analyst for a BTC/USD spot grid bot.
Analyze the current market state and provide a directional signal.

MARKET STATE:
- BTC Price: ${ctx.get('mid_price', 'N/A')}
- Spread: {ctx.get('spread_bps', 'N/A')} bps
- 1h Change: {ctx.get('price_change_1h_pct', 'N/A')}%
- 24h Change: {ctx.get('price_change_24h_pct', 'N/A')}%
- Volatility: {ctx.get('volatility_pct', 'N/A')}%
- Order Book Imbalance: {ctx.get('book_imbalance', 'N/A')}
- Current Regime: {ctx.get('regime', 'N/A')}

PORTFOLIO STATE:
- BTC Allocation: {ctx.get('btc_allocation_pct', 'N/A')}%
- Drawdown: {ctx.get('drawdown_pct', 'N/A')}%
- YTD Taxable Gain (EUR): {ctx.get('ytd_taxable_gain_eur', 'N/A')}

RESPOND IN EXACTLY THIS FORMAT (one line each):
DIRECTION: [STRONG_BUY|BUY|NEUTRAL|SELL|STRONG_SELL]
CONFIDENCE: [0.0-1.0]
BIAS_BPS: [integer, negative=sell bias, positive=buy bias]
REGIME_HINT: [range_bound|trending_up|trending_down|chaos|none]
REASONING: [one sentence explanation]"""

    def _parse_response(self, text: str) -> AISignal:
        """Parse structured AI response into AISignal."""
        signal = AISignal()

        for line in text.strip().split("\n"):
            line = line.strip()
            if line.startswith("DIRECTION:"):
                direction_str = line.split(":", 1)[1].strip().upper()
                try:
                    signal.direction = SignalDirection[direction_str]
                except KeyError:
                    signal.direction = SignalDirection.NEUTRAL

            elif line.startswith("CONFIDENCE:"):
                try:
                    signal.confidence = max(0.0, min(1.0, float(line.split(":", 1)[1].strip())))
                except ValueError:
                    signal.confidence = 0.0

            elif line.startswith("BIAS_BPS:"):
                try:
                    signal.suggested_bias_bps = Decimal(line.split(":", 1)[1].strip())
                except Exception:
                    signal.suggested_bias_bps = Decimal("0")

            elif line.startswith("REGIME_HINT:"):
                hint = line.split(":", 1)[1].strip().lower()
                if hint != "none":
                    signal.regime_hint = hint

            elif line.startswith("REASONING:"):
                signal.reasoning = line.split(":", 1)[1].strip()

        return signal

    def metrics(self) -> dict[str, Any]:
        """Return engine metrics for observability."""
        return {
            "provider": self._provider,
            "model": self._model,
            "call_count": self._call_count,
            "error_count": self._error_count,
            "last_direction": self._last_signal.direction.name,
            "last_confidence": self._last_signal.confidence,
            "last_latency_ms": self._last_signal.latency_ms,
            "cooldown_remaining_sec": max(
                0.0, self._cooldown_sec - (time.time() - self._last_call_time),
            ),
        }
