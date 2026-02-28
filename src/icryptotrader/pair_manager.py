"""Pair Manager — multi-pair diversification across independent strategy loops.

Manages multiple StrategyLoop instances, one per trading pair. Provides:
  - Capital allocation by configurable weights
  - Portfolio-level risk aggregation
  - Cross-pair correlation tracking for hedging decisions
  - Combined metrics and snapshots

Config example (TOML):
    [[pairs]]
    symbol = "XBT/USD"
    weight = 0.6

    [[pairs]]
    symbol = "ETH/USD"
    weight = 0.4
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)

# Rolling window for return correlation
_CORRELATION_WINDOW = 50


@dataclass
class PairState:
    """Runtime state for a single managed pair."""

    symbol: str = ""
    weight: float = 1.0
    allocated_usd: Decimal = Decimal("0")
    current_value_usd: Decimal = Decimal("0")
    drawdown_pct: float = 0.0
    returns: deque[float] = field(default_factory=lambda: deque(maxlen=_CORRELATION_WINDOW))
    last_price: Decimal = Decimal("0")


@dataclass
class PortfolioRisk:
    """Aggregated risk across all pairs."""

    total_value_usd: Decimal = Decimal("0")
    combined_drawdown_pct: float = 0.0
    pair_count: int = 0
    max_pair_drawdown_pct: float = 0.0
    correlation: float = 0.0  # Average pairwise correlation


class PairManager:
    """Orchestrates multiple trading pairs with weighted allocation.

    Usage:
        pm = PairManager(total_capital_usd=Decimal("10000"))
        pm.add_pair("XBT/USD", weight=0.6)
        pm.add_pair("ETH/USD", weight=0.4)
        pm.allocate()
        risk = pm.portfolio_risk()
    """

    def __init__(self, total_capital_usd: Decimal = Decimal("10000")) -> None:
        self._total_capital = total_capital_usd
        self._pairs: dict[str, PairState] = {}
        self._high_water_mark = total_capital_usd

    @property
    def pairs(self) -> dict[str, PairState]:
        return self._pairs

    @property
    def pair_count(self) -> int:
        return len(self._pairs)

    def add_pair(self, symbol: str, weight: float = 1.0) -> None:
        """Register a trading pair with an allocation weight."""
        self._pairs[symbol] = PairState(symbol=symbol, weight=weight)
        logger.info("PairManager: added %s (weight=%.2f)", symbol, weight)

    def allocate(self) -> dict[str, Decimal]:
        """Distribute capital across pairs by weight. Returns {symbol: usd_amount}."""
        total_weight = sum(p.weight for p in self._pairs.values())
        if total_weight <= 0:
            return {}

        result: dict[str, Decimal] = {}
        for symbol, state in self._pairs.items():
            alloc = self._total_capital * Decimal(str(state.weight / total_weight))
            state.allocated_usd = alloc
            result[symbol] = alloc

        logger.info(
            "PairManager: allocated %d pairs, total $%s",
            len(result), self._total_capital,
        )
        return result

    def update_pair(
        self,
        symbol: str,
        current_value_usd: Decimal,
        drawdown_pct: float,
        price: Decimal,
    ) -> None:
        """Update a pair's current state from its strategy loop."""
        state = self._pairs.get(symbol)
        if not state:
            return

        # Track returns for correlation
        if state.last_price > 0 and price > 0:
            ret = float((price - state.last_price) / state.last_price)
            state.returns.append(ret)

        state.current_value_usd = current_value_usd
        state.drawdown_pct = drawdown_pct
        state.last_price = price

    def portfolio_risk(self) -> PortfolioRisk:
        """Compute aggregate portfolio risk across all pairs."""
        if not self._pairs:
            return PortfolioRisk()

        total = sum(p.current_value_usd for p in self._pairs.values())
        if total > self._high_water_mark:
            self._high_water_mark = total

        combined_dd = 0.0
        if self._high_water_mark > 0:
            combined_dd = float(
                (self._high_water_mark - total) / self._high_water_mark,
            )

        max_dd = max((p.drawdown_pct for p in self._pairs.values()), default=0.0)
        corr = self._average_correlation()

        return PortfolioRisk(
            total_value_usd=total,
            combined_drawdown_pct=combined_dd,
            pair_count=len(self._pairs),
            max_pair_drawdown_pct=max_dd,
            correlation=corr,
        )

    def position_limit_usd(self, symbol: str) -> Decimal:
        """Max position value for a pair based on its weight allocation."""
        state = self._pairs.get(symbol)
        if not state:
            return Decimal("0")
        total_weight = sum(p.weight for p in self._pairs.values())
        if total_weight <= 0:
            return Decimal("0")
        return self._total_capital * Decimal(str(state.weight / total_weight))

    def _average_correlation(self) -> float:
        """Compute average pairwise return correlation."""
        symbols = [s for s, p in self._pairs.items() if len(p.returns) >= 10]
        if len(symbols) < 2:
            return 0.0

        correlations: list[float] = []
        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                corr = _pearson_correlation(
                    list(self._pairs[symbols[i]].returns),
                    list(self._pairs[symbols[j]].returns),
                )
                if corr is not None:
                    correlations.append(corr)

        return sum(correlations) / len(correlations) if correlations else 0.0


def _pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    """Simple Pearson correlation between two return series."""
    n = min(len(xs), len(ys))
    if n < 5:
        return None

    xs, ys = xs[-n:], ys[-n:]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)

    denom = (var_x * var_y) ** 0.5
    if denom == 0:
        return 0.0
    return cov / denom
