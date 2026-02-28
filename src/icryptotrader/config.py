"""Configuration system — loads TOML config into typed dataclasses."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "default.toml"


@dataclass
class KrakenConfig:
    api_key: str = ""
    api_secret: str = ""
    ws_public_url: str = "wss://ws.kraken.com/v2"
    ws_private_url: str = "wss://ws-auth.kraken.com/v2"
    rest_url: str = "https://api.kraken.com"


@dataclass
class GridConfig:
    levels: int = 5
    order_size_usd: Decimal = Decimal("500")
    min_spacing_bps: Decimal = Decimal("20")
    post_only: bool = True


@dataclass
class RiskConfig:
    max_portfolio_drawdown_pct: float = 0.15
    emergency_drawdown_pct: float = 0.20
    price_velocity_freeze_pct: float = 0.03
    price_velocity_window_sec: int = 60
    price_velocity_cooldown_sec: int = 30


@dataclass
class TaxConfig:
    holding_period_days: int = 365
    near_threshold_days: int = 330
    annual_exemption_eur: Decimal = Decimal("1000")
    emergency_dd_override_pct: float = 0.20
    harvest_enabled: bool = False
    harvest_min_loss_eur: Decimal = Decimal("50")
    harvest_max_per_day: int = 3
    harvest_target_net_eur: Decimal = Decimal("800")


@dataclass
class RegimeAllocation:
    btc_target_pct: float = 0.50
    btc_max_pct: float = 0.60
    btc_min_pct: float = 0.40
    grid_levels: int = 5
    signal_enabled: bool = True


@dataclass
class RegimeConfig:
    range_bound: RegimeAllocation = field(
        default_factory=lambda: RegimeAllocation(
            btc_target_pct=0.50, btc_max_pct=0.60, btc_min_pct=0.40,
            grid_levels=5, signal_enabled=True,
        )
    )
    trending_up: RegimeAllocation = field(
        default_factory=lambda: RegimeAllocation(
            btc_target_pct=0.70, btc_max_pct=0.80, btc_min_pct=0.55,
            grid_levels=3, signal_enabled=True,
        )
    )
    trending_down: RegimeAllocation = field(
        default_factory=lambda: RegimeAllocation(
            btc_target_pct=0.30, btc_max_pct=0.40, btc_min_pct=0.15,
            grid_levels=3, signal_enabled=True,
        )
    )
    chaos: RegimeAllocation = field(
        default_factory=lambda: RegimeAllocation(
            btc_target_pct=0.00, btc_max_pct=0.05, btc_min_pct=0.00,
            grid_levels=0, signal_enabled=False,
        )
    )


@dataclass
class WSConfig:
    cancel_after_timeout_sec: int = 60
    heartbeat_interval_sec: int = 20
    reconnect_max_backoff_sec: int = 30
    pending_ack_timeout_ms: int = 500


@dataclass
class RateLimitConfig:
    max_counter: int = 180
    decay_rate: float = 3.75
    headroom_pct: float = 0.80


@dataclass
class BollingerConfig:
    enabled: bool = True
    window: int = 20
    multiplier: float = 2.0
    spacing_scale: float = 0.5
    min_spacing_bps: Decimal = Decimal("15")
    max_spacing_bps: Decimal = Decimal("200")


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class Config:
    pair: str = "XBT/USD"
    log_level: str = "INFO"
    data_dir: str = "data"
    ledger_path: str = "data/fifo_ledger.json"
    kraken: KrakenConfig = field(default_factory=KrakenConfig)
    grid: GridConfig = field(default_factory=GridConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    tax: TaxConfig = field(default_factory=TaxConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    ws: WSConfig = field(default_factory=WSConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    bollinger: BollingerConfig = field(default_factory=BollingerConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


def _apply_toml_section(obj: object, data: dict) -> None:  # type: ignore[type-arg]
    """Recursively apply TOML dict values onto a dataclass instance."""
    for key, value in data.items():
        if not hasattr(obj, key):
            logger.warning("Unknown config key: %s", key)
            continue
        current = getattr(obj, key)
        if isinstance(value, dict) and hasattr(current, "__dataclass_fields__"):
            _apply_toml_section(current, value)
        elif isinstance(current, Decimal):
            setattr(obj, key, Decimal(str(value)))
        else:
            setattr(obj, key, value)


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML file, falling back to defaults."""
    cfg = Config()
    config_path = path or DEFAULT_CONFIG_PATH
    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        _apply_toml_section(cfg, data)
        logger.info("Loaded config from %s", config_path)
    else:
        logger.info("No config file at %s, using defaults", config_path)
    return cfg
