"""Tests for configuration loading."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

from icryptotrader.config import Config, load_config


def test_default_config_loads() -> None:
    cfg = load_config(Path("/dev/null"))
    assert cfg.pair == "XBT/USD"
    assert cfg.grid.levels == 5
    assert cfg.grid.order_size_usd == Decimal("500")
    assert cfg.risk.emergency_drawdown_pct == 0.20
    assert cfg.tax.holding_period_days == 365


def test_toml_override() -> None:
    toml_content = b"""
pair = "ETH/USD"

[grid]
levels = 3
order_size_usd = "250"
"""
    with NamedTemporaryFile(suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        cfg = load_config(Path(f.name))

    assert cfg.pair == "ETH/USD"
    assert cfg.grid.levels == 3
    assert cfg.grid.order_size_usd == Decimal("250")
    # Unset fields keep defaults
    assert cfg.risk.emergency_drawdown_pct == 0.20


def test_regime_config_defaults() -> None:
    cfg = Config()
    assert cfg.regime.range_bound.btc_target_pct == 0.50
    assert cfg.regime.chaos.grid_levels == 0
    assert cfg.regime.chaos.signal_enabled is False
    assert cfg.regime.trending_up.btc_max_pct == 0.80


def test_default_toml_file_loads() -> None:
    cfg = load_config()
    assert cfg.pair == "XBT/USD"
    assert cfg.ws.cancel_after_timeout_sec == 60
