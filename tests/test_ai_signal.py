"""Tests for the AI Signal Engine."""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from icryptotrader.strategy.ai_signal import AISignal, AISignalEngine, SignalDirection

SAMPLE_RESPONSE = """DIRECTION: BUY
CONFIDENCE: 0.75
BIAS_BPS: 15
REGIME_HINT: trending_up
REASONING: Strong upward momentum with positive book imbalance."""


SAMPLE_CONTEXT = {
    "mid_price": Decimal("85000"),
    "spread_bps": Decimal("5"),
    "price_change_1h_pct": 0.5,
    "price_change_24h_pct": 2.1,
    "volatility_pct": 1.2,
    "book_imbalance": 0.3,
    "regime": "range_bound",
    "btc_allocation_pct": 50.0,
    "drawdown_pct": 2.0,
    "ytd_taxable_gain_eur": Decimal("200"),
}


class TestSignalDirection:
    def test_all_directions_exist(self) -> None:
        assert len(SignalDirection) == 5
        assert SignalDirection.STRONG_BUY
        assert SignalDirection.BUY
        assert SignalDirection.NEUTRAL
        assert SignalDirection.SELL
        assert SignalDirection.STRONG_SELL


class TestAISignalDataclass:
    def test_default_neutral(self) -> None:
        sig = AISignal()
        assert sig.direction == SignalDirection.NEUTRAL
        assert sig.confidence == 0.0
        assert sig.suggested_bias_bps == Decimal("0")

    def test_error_signal(self) -> None:
        sig = AISignal(error="provider_error")
        assert sig.error == "provider_error"


class TestParseResponse:
    def test_parses_full_response(self) -> None:
        engine = AISignalEngine(api_key="test")
        signal = engine._parse_response(SAMPLE_RESPONSE)
        assert signal.direction == SignalDirection.BUY
        assert signal.confidence == 0.75
        assert signal.suggested_bias_bps == Decimal("15")
        assert signal.regime_hint == "trending_up"
        assert "momentum" in signal.reasoning.lower()

    def test_parses_sell_signal(self) -> None:
        engine = AISignalEngine(api_key="test")
        response = """DIRECTION: STRONG_SELL
CONFIDENCE: 0.9
BIAS_BPS: -30
REGIME_HINT: trending_down
REASONING: Market crash detected."""
        signal = engine._parse_response(response)
        assert signal.direction == SignalDirection.STRONG_SELL
        assert signal.confidence == 0.9
        assert signal.suggested_bias_bps == Decimal("-30")

    def test_unknown_direction_defaults_neutral(self) -> None:
        engine = AISignalEngine(api_key="test")
        signal = engine._parse_response("DIRECTION: SIDEWAYS\nCONFIDENCE: 0.5")
        assert signal.direction == SignalDirection.NEUTRAL

    def test_invalid_confidence_defaults_zero(self) -> None:
        engine = AISignalEngine(api_key="test")
        signal = engine._parse_response("DIRECTION: BUY\nCONFIDENCE: abc")
        assert signal.confidence == 0.0

    def test_confidence_clamped_to_1(self) -> None:
        engine = AISignalEngine(api_key="test")
        signal = engine._parse_response("DIRECTION: BUY\nCONFIDENCE: 5.0")
        assert signal.confidence == 1.0

    def test_none_regime_hint_ignored(self) -> None:
        engine = AISignalEngine(api_key="test")
        signal = engine._parse_response("DIRECTION: NEUTRAL\nREGIME_HINT: none")
        assert signal.regime_hint == ""

    def test_empty_response(self) -> None:
        engine = AISignalEngine(api_key="test")
        signal = engine._parse_response("")
        assert signal.direction == SignalDirection.NEUTRAL

    def test_malformed_response_is_safe(self) -> None:
        engine = AISignalEngine(api_key="test")
        signal = engine._parse_response("This is garbage text from the AI.")
        assert signal.direction == SignalDirection.NEUTRAL
        assert signal.confidence == 0.0


class TestBuildPrompt:
    def test_includes_market_data(self) -> None:
        engine = AISignalEngine(api_key="test")
        prompt = engine._build_prompt(SAMPLE_CONTEXT)
        assert "$85000" in prompt
        assert "range_bound" in prompt
        assert "STRONG_BUY" in prompt  # Part of response format


class TestCooldown:
    def test_is_ready_initially(self) -> None:
        engine = AISignalEngine(api_key="test", cooldown_sec=300)
        assert engine.is_ready

    def test_not_ready_after_call(self) -> None:
        engine = AISignalEngine(api_key="test", cooldown_sec=300)
        engine._last_call_time = time.time()
        assert not engine.is_ready

    def test_ready_after_cooldown(self) -> None:
        engine = AISignalEngine(api_key="test", cooldown_sec=1)
        engine._last_call_time = time.time() - 2
        assert engine.is_ready


class TestGenerateSignal:
    @pytest.mark.asyncio()
    async def test_no_api_key_returns_error(self) -> None:
        engine = AISignalEngine(api_key="")
        signal = await engine.generate_signal(SAMPLE_CONTEXT)
        assert signal.error == "no_api_key"

    @pytest.mark.asyncio()
    async def test_cooldown_returns_last_signal(self) -> None:
        engine = AISignalEngine(api_key="test", cooldown_sec=9999)
        engine._last_call_time = time.time()
        engine._last_signal = AISignal(direction=SignalDirection.BUY, confidence=0.8)
        signal = await engine.generate_signal(SAMPLE_CONTEXT)
        assert signal.direction == SignalDirection.BUY
        assert signal.confidence == 0.8

    @pytest.mark.asyncio()
    async def test_gemini_call_success(self) -> None:
        engine = AISignalEngine(provider="gemini", api_key="test-key")
        with patch.object(engine, "_call_gemini", new_callable=AsyncMock) as mock:
            mock.return_value = SAMPLE_RESPONSE
            signal = await engine.generate_signal(SAMPLE_CONTEXT)
        assert signal.direction == SignalDirection.BUY
        assert signal.confidence == 0.75
        assert signal.provider == "gemini"
        assert signal.latency_ms > 0
        assert engine._call_count == 1

    @pytest.mark.asyncio()
    async def test_anthropic_call_success(self) -> None:
        engine = AISignalEngine(provider="anthropic", api_key="test-key", model="claude-sonnet-4-6")
        with patch.object(engine, "_call_anthropic", new_callable=AsyncMock) as mock:
            mock.return_value = SAMPLE_RESPONSE
            signal = await engine.generate_signal(SAMPLE_CONTEXT)
        assert signal.direction == SignalDirection.BUY
        assert signal.provider == "anthropic"

    @pytest.mark.asyncio()
    async def test_openai_call_success(self) -> None:
        engine = AISignalEngine(provider="openai", api_key="test-key", model="gpt-4o")
        with patch.object(engine, "_call_openai", new_callable=AsyncMock) as mock:
            mock.return_value = SAMPLE_RESPONSE
            signal = await engine.generate_signal(SAMPLE_CONTEXT)
        assert signal.direction == SignalDirection.BUY
        assert signal.provider == "openai"

    @pytest.mark.asyncio()
    async def test_provider_error_returns_error_signal(self) -> None:
        engine = AISignalEngine(provider="gemini", api_key="test-key")
        with patch.object(engine, "_call_gemini", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("API down")
            signal = await engine.generate_signal(SAMPLE_CONTEXT)
        assert signal.error == "provider_error"
        assert engine._error_count == 1

    @pytest.mark.asyncio()
    async def test_unknown_provider_returns_error(self) -> None:
        engine = AISignalEngine(provider="unknown_ai", api_key="test-key")
        signal = await engine.generate_signal(SAMPLE_CONTEXT)
        assert signal.error == "provider_error"


class TestMetrics:
    def test_metrics_dict(self) -> None:
        engine = AISignalEngine(provider="gemini", api_key="test")
        m = engine.metrics()
        assert m["provider"] == "gemini"
        assert m["call_count"] == 0
        assert m["error_count"] == 0
        assert m["last_direction"] == "NEUTRAL"

    @pytest.mark.asyncio()
    async def test_metrics_after_call(self) -> None:
        engine = AISignalEngine(provider="gemini", api_key="test-key")
        with patch.object(engine, "_call_gemini", new_callable=AsyncMock) as mock:
            mock.return_value = SAMPLE_RESPONSE
            await engine.generate_signal(SAMPLE_CONTEXT)
        m = engine.metrics()
        assert m["call_count"] == 1
        assert m["last_direction"] == "BUY"
        assert m["last_confidence"] == 0.75
