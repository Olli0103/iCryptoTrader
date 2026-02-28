"""Shared types, enums, and dataclasses used across modules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(Enum):
    GTC = "GTC"  # Good till cancelled
    IOC = "IOC"  # Immediate or cancel
    GTD = "GTD"  # Good till date


class SlotState(Enum):
    """Order slot states for the amend-first state machine."""

    EMPTY = auto()
    PENDING_NEW = auto()
    LIVE = auto()
    AMEND_PENDING = auto()
    CANCEL_PENDING = auto()


class Regime(Enum):
    RANGE_BOUND = "range_bound"
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    CHAOS = "chaos"


class PauseState(Enum):
    ACTIVE_TRADING = auto()
    TAX_LOCK_ACTIVE = auto()
    RISK_PAUSE_ACTIVE = auto()
    DUAL_LOCK = auto()
    EMERGENCY_SELL = auto()


class TaxVetoDecision(Enum):
    ALLOW = "allow"
    ALLOW_PARTIAL = "allow_partial"
    VETO = "veto"
    OVERRIDE_EMERGENCY = "override"


class LotStatus(Enum):
    OPEN = "open"
    PARTIALLY_SOLD = "partial"
    CLOSED = "closed"


@dataclass(frozen=True)
class Pair:
    """Trading pair identifier."""

    base: str  # e.g. "XBT"
    quote: str  # e.g. "USD"

    @property
    def kraken_symbol(self) -> str:
        return f"{self.base}/{self.quote}"

    def __str__(self) -> str:
        return self.kraken_symbol


# Default trading pair
BTC_USD = Pair(base="XBT", quote="USD")


@dataclass
class HarvestRecommendation:
    """Recommendation to sell a losing lot for tax optimization."""

    lot_id: str
    qty_btc: Decimal
    estimated_loss_eur: Decimal
    current_price_usd: Decimal
    cost_basis_per_btc_eur: Decimal
    days_held: int
    reason: str  # e.g., "offset_gains", "freigrenze_optimization"


@dataclass(frozen=True)
class FeeTier:
    """A single fee tier with volume threshold and rates."""

    min_volume_usd: int
    maker_bps: Decimal
    taker_bps: Decimal

    @property
    def maker_pct(self) -> Decimal:
        return self.maker_bps / Decimal("10000")

    @property
    def taker_pct(self) -> Decimal:
        return self.taker_bps / Decimal("10000")

    @property
    def rt_cost_bps(self) -> Decimal:
        """Round-trip cost in basis points (maker + maker)."""
        return self.maker_bps * 2
