"""Tests for configuration loading."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from icryptotrader.config import Config, ConfigError, load_config, validate_config


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


class TestConfigValidation:
    def test_valid_default_config(self) -> None:
        cfg = Config()
        errors = validate_config(cfg)
        assert errors == []

    def test_negative_grid_levels(self) -> None:
        cfg = Config()
        cfg.grid.levels = -1
        errors = validate_config(cfg)
        assert any("grid.levels" in e for e in errors)

    def test_zero_order_size(self) -> None:
        cfg = Config()
        cfg.grid.order_size_usd = Decimal("0")
        errors = validate_config(cfg)
        assert any("order_size_usd" in e for e in errors)

    def test_emergency_below_max_drawdown(self) -> None:
        cfg = Config()
        cfg.risk.emergency_drawdown_pct = 0.10
        cfg.risk.max_portfolio_drawdown_pct = 0.15
        errors = validate_config(cfg)
        assert any("emergency_drawdown_pct" in e for e in errors)

    def test_negative_velocity(self) -> None:
        cfg = Config()
        cfg.risk.price_velocity_freeze_pct = -0.01
        errors = validate_config(cfg)
        assert any("velocity" in e for e in errors)

    def test_near_threshold_exceeds_holding(self) -> None:
        cfg = Config()
        cfg.tax.near_threshold_days = 400
        cfg.tax.holding_period_days = 365
        errors = validate_config(cfg)
        assert any("near_threshold_days" in e for e in errors)

    def test_regime_allocation_ordering(self) -> None:
        cfg = Config()
        cfg.regime.range_bound.btc_min_pct = 0.70
        cfg.regime.range_bound.btc_target_pct = 0.50
        errors = validate_config(cfg)
        assert any("btc_min_pct" in e for e in errors)

    def test_bollinger_window_too_small(self) -> None:
        cfg = Config()
        cfg.bollinger.window = 1
        errors = validate_config(cfg)
        assert any("bollinger.window" in e for e in errors)

    def test_ai_signal_enabled_no_key(self) -> None:
        cfg = Config()
        cfg.ai_signal.enabled = True
        cfg.ai_signal.api_key = ""
        errors = validate_config(cfg)
        assert any("ai_signal.api_key" in e for e in errors)

    def test_ai_signal_invalid_provider(self) -> None:
        cfg = Config()
        cfg.ai_signal.enabled = True
        cfg.ai_signal.api_key = "test"
        cfg.ai_signal.provider = "invalid"
        errors = validate_config(cfg)
        assert any("provider" in e for e in errors)

    def test_load_config_raises_on_invalid(self) -> None:
        toml_content = b"""
[grid]
levels = -5
"""
        with NamedTemporaryFile(suffix=".toml", delete=False) as f:
            f.write(toml_content)
            f.flush()
            with pytest.raises(ConfigError):
                load_config(Path(f.name))

    def test_ai_signal_config_defaults(self) -> None:
        cfg = Config()
        assert cfg.ai_signal.enabled is False
        assert cfg.ai_signal.provider == "gemini"
        assert cfg.ai_signal.weight == 0.3

    def test_invalid_persistence_backend(self) -> None:
        cfg = Config()
        cfg.persistence_backend = "postgresql"
        errors = validate_config(cfg)
        assert any("persistence_backend" in e for e in errors)

    def test_invalid_hedge_strategy(self) -> None:
        cfg = Config()
        cfg.hedge.enabled = True
        cfg.hedge.strategy = "invalid"
        errors = validate_config(cfg)
        assert any("hedge.strategy" in e for e in errors)

    def test_hedge_config_defaults(self) -> None:
        cfg = Config()
        assert cfg.hedge.enabled is False
        assert cfg.hedge.trigger_drawdown_pct == 0.10
        assert cfg.hedge.strategy == "reduce_exposure"

    def test_web_config_defaults(self) -> None:
        cfg = Config()
        assert cfg.web.enabled is False
        assert cfg.web.port == 8080

    def test_persistence_backend_default(self) -> None:
        cfg = Config()
        assert cfg.persistence_backend == "json"

    def test_metrics_config_defaults(self) -> None:
        cfg = Config()
        assert cfg.metrics.enabled is False
        assert cfg.metrics.port == 9090

    def test_grid_auto_compound_defaults(self) -> None:
        cfg = Config()
        assert cfg.grid.auto_compound is False
        assert cfg.grid.compound_base_usd == Decimal("5000")

    def test_bollinger_atr_defaults(self) -> None:
        cfg = Config()
        assert cfg.bollinger.atr_enabled is True
        assert cfg.bollinger.atr_window == 14
        assert cfg.bollinger.atr_weight == 0.3

    def test_risk_trailing_stop_defaults(self) -> None:
        cfg = Config()
        assert cfg.risk.trailing_stop_enabled is True
        assert cfg.risk.trailing_stop_tighten_pct == 0.02

    def test_toml_loads_new_fields(self) -> None:
        toml_content = b"""
[grid]
auto_compound = true
compound_base_usd = "10000"

[bollinger]
atr_enabled = false
atr_window = 20
atr_weight = 0.5

[risk]
trailing_stop_enabled = false
trailing_stop_tighten_pct = 0.05
"""
        with NamedTemporaryFile(suffix=".toml", delete=False) as f:
            f.write(toml_content)
            f.flush()
            cfg = load_config(Path(f.name))
        assert cfg.grid.auto_compound is True
        assert cfg.grid.compound_base_usd == Decimal("10000")
        assert cfg.bollinger.atr_enabled is False
        assert cfg.bollinger.atr_window == 20
        assert cfg.bollinger.atr_weight == 0.5
        assert cfg.risk.trailing_stop_enabled is False
        assert cfg.risk.trailing_stop_tighten_pct == 0.05
