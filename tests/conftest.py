"""Shared test fixtures."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from icryptotrader.config import Config, load_config
from icryptotrader.types import BTC_USD, FeeTier, Pair


@pytest.fixture
def default_config() -> Config:
    return load_config(Path("/dev/null"))  # All defaults


@pytest.fixture
def btc_usd() -> Pair:
    return BTC_USD


@pytest.fixture
def base_fee_tier() -> FeeTier:
    return FeeTier(min_volume_usd=0, maker_bps=Decimal("25"), taker_bps=Decimal("40"))


@pytest.fixture
def pro_fee_tier() -> FeeTier:
    return FeeTier(min_volume_usd=1_000_000, maker_bps=Decimal("4"), taker_bps=Decimal("14"))
