"""Tests for shared types."""

from __future__ import annotations

from decimal import Decimal

from icryptotrader.types import BTC_USD, FeeTier, Pair, Side, SlotState


def test_pair_symbol() -> None:
    assert BTC_USD.kraken_symbol == "XBT/USD"
    assert str(BTC_USD) == "XBT/USD"


def test_pair_frozen() -> None:
    p = Pair(base="ETH", quote="USD")
    assert p.base == "ETH"


def test_fee_tier_rt_cost() -> None:
    tier = FeeTier(min_volume_usd=0, maker_bps=Decimal("25"), taker_bps=Decimal("40"))
    assert tier.rt_cost_bps == Decimal("50")
    assert tier.maker_pct == Decimal("0.0025")
    assert tier.taker_pct == Decimal("0.004")


def test_fee_tier_pro() -> None:
    tier = FeeTier(min_volume_usd=1_000_000, maker_bps=Decimal("4"), taker_bps=Decimal("14"))
    assert tier.rt_cost_bps == Decimal("8")


def test_side_values() -> None:
    assert Side.BUY.value == "buy"
    assert Side.SELL.value == "sell"


def test_slot_states_exist() -> None:
    assert len(SlotState) == 5
