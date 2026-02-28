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
    auto_compound: bool = False  # Reinvest profits into order size
    compound_base_usd: Decimal = Decimal("5000")  # Starting portfolio for scaling
    geometric_spacing: bool = True  # Geometric (safe) vs linear (can go negative)
    amend_threshold_bps: Decimal = Decimal("3")  # Min price move before amending


@dataclass
class RiskConfig:
    max_portfolio_drawdown_pct: float = 0.15
    emergency_drawdown_pct: float = 0.20
    price_velocity_freeze_pct: float = 0.03
    price_velocity_window_sec: int = 60
    price_velocity_cooldown_sec: int = 30
    trailing_stop_enabled: bool = True  # Dynamic trailing stop
    trailing_stop_tighten_pct: float = 0.02  # Tighten 2% per new HWM
    max_rebalance_pct_per_min: float = 0.01  # TWAP: max 1% portfolio per minute


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
    blow_through_mode: bool = False  # Skip Freigrenze gating, maximize gross
    harvest_wash_sale_cooldown_hours: int = 24  # §42 AO safe harbor
    vault_lock_priority: bool = True  # Prioritize selling >365-day lots


@dataclass
class RegimeAllocation:
    btc_target_pct: float = 0.50
    btc_max_pct: float = 0.60
    btc_min_pct: float = 0.40
    grid_levels: int = 5
    signal_enabled: bool = True
    order_size_scale: float = 1.0  # Multiplier for order_size_usd per regime


@dataclass
class RegimeConfig:
    range_bound: RegimeAllocation = field(
        default_factory=lambda: RegimeAllocation(
            btc_target_pct=0.50, btc_max_pct=0.60, btc_min_pct=0.40,
            grid_levels=5, signal_enabled=True, order_size_scale=1.0,
        )
    )
    trending_up: RegimeAllocation = field(
        default_factory=lambda: RegimeAllocation(
            btc_target_pct=0.70, btc_max_pct=0.80, btc_min_pct=0.55,
            grid_levels=3, signal_enabled=True, order_size_scale=0.75,
        )
    )
    trending_down: RegimeAllocation = field(
        default_factory=lambda: RegimeAllocation(
            btc_target_pct=0.30, btc_max_pct=0.40, btc_min_pct=0.15,
            grid_levels=3, signal_enabled=True, order_size_scale=0.75,
        )
    )
    chaos: RegimeAllocation = field(
        default_factory=lambda: RegimeAllocation(
            btc_target_pct=0.00, btc_max_pct=0.05, btc_min_pct=0.00,
            grid_levels=0, signal_enabled=False, order_size_scale=0.5,
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
    atr_enabled: bool = True  # Combine ATR with Bollinger for spacing
    atr_window: int = 14  # ATR lookback period
    atr_weight: float = 0.3  # Weight of ATR vs Bollinger (0=BB only, 1=ATR only)


@dataclass
class AvellanedaStoikovConfig:
    """Configuration for the Avellaneda-Stoikov optimal market making model."""

    enabled: bool = False  # Opt-in; when enabled replaces Bollinger + DeltaSkew
    gamma: float = 0.3  # Risk aversion [0.01, 2.0]. Higher = wider spread
    max_spread_bps: Decimal = Decimal("500")
    max_skew_bps: Decimal = Decimal("50")
    obi_sensitivity_bps: Decimal = Decimal("10")


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class AISignalConfig:
    """Configuration for the AI Signal Engine (multi-provider)."""

    enabled: bool = False
    provider: str = "gemini"  # "gemini", "anthropic", "openai"
    api_key: str = ""
    model: str = "gemini-2.0-flash"
    temperature: float = 0.2
    max_tokens: int = 512
    cooldown_sec: int = 300  # Min seconds between AI calls
    weight: float = 0.3  # Signal weight vs grid (0.0-1.0)
    timeout_sec: int = 10  # HTTP timeout for AI provider


@dataclass
class MetricsConfig:
    """Configuration for structured metrics export."""

    enabled: bool = False
    port: int = 9090
    prefix: str = "icryptotrader"


@dataclass
class HedgeConfig:
    """Configuration for the hedge manager."""

    enabled: bool = False
    trigger_drawdown_pct: float = 0.10
    strategy: str = "reduce_exposure"  # "reduce_exposure" or "inverse_grid"
    max_reduction_pct: float = 0.50  # Max portion of buys to cancel


@dataclass
class WebConfig:
    """Configuration for the web dashboard."""

    enabled: bool = False
    port: int = 8080
    host: str = "127.0.0.1"
    username: str = ""
    password: str = ""


@dataclass
class PairAllocation:
    """Allocation weight for a single trading pair."""

    symbol: str = "XBT/USD"
    weight: float = 1.0


@dataclass
class Config:
    pair: str = "XBT/USD"
    log_level: str = "INFO"
    data_dir: str = "data"
    ledger_path: str = "data/fifo_ledger.json"
    persistence_backend: str = "json"  # "json" or "sqlite"
    kraken: KrakenConfig = field(default_factory=KrakenConfig)
    grid: GridConfig = field(default_factory=GridConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    tax: TaxConfig = field(default_factory=TaxConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    ws: WSConfig = field(default_factory=WSConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    bollinger: BollingerConfig = field(default_factory=BollingerConfig)
    avellaneda_stoikov: AvellanedaStoikovConfig = field(default_factory=AvellanedaStoikovConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    ai_signal: AISignalConfig = field(default_factory=AISignalConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    hedge: HedgeConfig = field(default_factory=HedgeConfig)
    web: WebConfig = field(default_factory=WebConfig)
    pairs: list[PairAllocation] = field(default_factory=list)


def _apply_toml_section(obj: object, data: dict) -> None:  # type: ignore[type-arg]
    """Recursively apply TOML dict values onto a dataclass instance."""
    for key, value in data.items():
        if not hasattr(obj, key):
            logger.warning("Unknown config key: %s", key)
            continue
        current = getattr(obj, key)
        # Handle list of PairAllocation from TOML [[pairs]]
        if key == "pairs" and isinstance(value, list):
            setattr(obj, key, [
                PairAllocation(**item) if isinstance(item, dict) else item
                for item in value
            ])
        elif isinstance(value, dict) and hasattr(current, "__dataclass_fields__"):
            _apply_toml_section(current, value)
        elif isinstance(current, Decimal):
            setattr(obj, key, Decimal(str(value)))
        else:
            setattr(obj, key, value)


class ConfigError(ValueError):
    """Raised when configuration validation fails."""


def validate_config(cfg: Config) -> list[str]:
    """Validate config values and return list of errors (empty = valid)."""
    errors: list[str] = []

    # Grid
    if cfg.grid.levels < 0:
        errors.append("grid.levels must be >= 0")
    if cfg.grid.order_size_usd <= 0:
        errors.append("grid.order_size_usd must be > 0")
    if cfg.grid.min_spacing_bps <= 0:
        errors.append("grid.min_spacing_bps must be > 0")

    # Risk — ordering and ranges
    if not (0 < cfg.risk.max_portfolio_drawdown_pct <= 1.0):
        errors.append("risk.max_portfolio_drawdown_pct must be in (0, 1.0]")
    if not (0 < cfg.risk.emergency_drawdown_pct <= 1.0):
        errors.append("risk.emergency_drawdown_pct must be in (0, 1.0]")
    if cfg.risk.emergency_drawdown_pct < cfg.risk.max_portfolio_drawdown_pct:
        errors.append("risk.emergency_drawdown_pct must be >= max_portfolio_drawdown_pct")
    if cfg.risk.price_velocity_freeze_pct <= 0:
        errors.append("risk.price_velocity_freeze_pct must be > 0")
    if cfg.risk.price_velocity_window_sec <= 0:
        errors.append("risk.price_velocity_window_sec must be > 0")

    # Tax
    if cfg.tax.holding_period_days < 1:
        errors.append("tax.holding_period_days must be >= 1")
    if cfg.tax.near_threshold_days >= cfg.tax.holding_period_days:
        errors.append("tax.near_threshold_days must be < holding_period_days")
    if cfg.tax.annual_exemption_eur < 0:
        errors.append("tax.annual_exemption_eur must be >= 0")
    if cfg.tax.harvest_max_per_day < 1:
        errors.append("tax.harvest_max_per_day must be >= 1")

    # Regime — allocation ordering
    for name in ("range_bound", "trending_up", "trending_down", "chaos"):
        alloc = getattr(cfg.regime, name)
        if alloc.btc_min_pct > alloc.btc_target_pct:
            errors.append(f"regime.{name}: btc_min_pct must be <= btc_target_pct")
        if alloc.btc_target_pct > alloc.btc_max_pct:
            errors.append(f"regime.{name}: btc_target_pct must be <= btc_max_pct")
        if not (0 < alloc.order_size_scale <= 5.0):
            errors.append(f"regime.{name}: order_size_scale must be in (0, 5.0]")

    # Avellaneda-Stoikov
    if cfg.avellaneda_stoikov.enabled and cfg.avellaneda_stoikov.gamma <= 0:
        errors.append("avellaneda_stoikov.gamma must be > 0")

    # Bollinger
    if cfg.bollinger.window < 2:
        errors.append("bollinger.window must be >= 2")
    if cfg.bollinger.min_spacing_bps >= cfg.bollinger.max_spacing_bps:
        errors.append("bollinger.min_spacing_bps must be < max_spacing_bps")

    # WS
    if cfg.ws.cancel_after_timeout_sec < 1:
        errors.append("ws.cancel_after_timeout_sec must be >= 1")
    if cfg.ws.heartbeat_interval_sec < 1:
        errors.append("ws.heartbeat_interval_sec must be >= 1")

    # Rate limit
    if cfg.rate_limit.max_counter < 1:
        errors.append("rate_limit.max_counter must be >= 1")
    if not (0 < cfg.rate_limit.headroom_pct <= 1.0):
        errors.append("rate_limit.headroom_pct must be in (0, 1.0]")

    # AI Signal
    if cfg.ai_signal.enabled:
        if not cfg.ai_signal.api_key:
            errors.append("ai_signal.api_key required when ai_signal.enabled=true")
        if cfg.ai_signal.provider not in ("gemini", "anthropic", "openai"):
            errors.append("ai_signal.provider must be 'gemini', 'anthropic', or 'openai'")
        if not (0.0 <= cfg.ai_signal.weight <= 1.0):
            errors.append("ai_signal.weight must be in [0.0, 1.0]")

    # Persistence
    if cfg.persistence_backend not in ("json", "sqlite"):
        errors.append("persistence_backend must be 'json' or 'sqlite'")

    # Hedge
    if cfg.hedge.enabled:
        if not (0 < cfg.hedge.trigger_drawdown_pct <= 1.0):
            errors.append("hedge.trigger_drawdown_pct must be in (0, 1.0]")
        if cfg.hedge.strategy not in ("reduce_exposure", "inverse_grid"):
            errors.append("hedge.strategy must be 'reduce_exposure' or 'inverse_grid'")

    return errors


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

    errors = validate_config(cfg)
    if errors:
        for err in errors:
            logger.error("Config validation error: %s", err)
        raise ConfigError(f"Invalid configuration: {'; '.join(errors)}")

    return cfg
