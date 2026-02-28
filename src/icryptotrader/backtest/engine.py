"""Backtesting Engine — historical tick replay through the strategy loop.

Replays a sequence of historical price ticks through a simulated strategy
loop, tracking fills, P&L, drawdown, and tax implications. No real orders
are placed.

Usage:
    from icryptotrader.backtest.engine import BacktestEngine, BacktestConfig

    engine = BacktestEngine(
        config=BacktestConfig(
            initial_usd=5000,
            order_size_usd=Decimal("500"),
            grid_levels=5,
        ),
    )
    result = engine.run(prices)
    print(result.summary())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""

    initial_usd: Decimal = Decimal("5000")
    initial_btc: Decimal = Decimal("0")
    order_size_usd: Decimal = Decimal("500")
    grid_levels: int = 5
    spacing_bps: Decimal = Decimal("50")
    maker_fee_bps: Decimal = Decimal("16")
    auto_compound: bool = False


@dataclass
class BacktestTrade:
    """A simulated trade."""

    tick: int
    side: str  # "buy" or "sell"
    price: Decimal
    qty: Decimal
    fee: Decimal
    pnl: Decimal = Decimal("0")  # Only for sells


@dataclass
class BacktestResult:
    """Results from a backtest run."""

    config: BacktestConfig
    ticks: int = 0
    trades: list[BacktestTrade] = field(default_factory=list)
    final_btc: Decimal = Decimal("0")
    final_usd: Decimal = Decimal("0")
    final_portfolio_usd: Decimal = Decimal("0")
    initial_portfolio_usd: Decimal = Decimal("0")
    high_water_mark: Decimal = Decimal("0")
    max_drawdown_pct: float = 0.0
    total_fees_usd: Decimal = Decimal("0")
    total_pnl_usd: Decimal = Decimal("0")
    buy_count: int = 0
    sell_count: int = 0

    @property
    def return_pct(self) -> float:
        if self.initial_portfolio_usd <= 0:
            return 0.0
        return float(
            (self.final_portfolio_usd - self.initial_portfolio_usd)
            / self.initial_portfolio_usd,
        )

    def summary(self) -> str:
        """Human-readable backtest summary."""
        return (
            f"Backtest Results\n"
            f"{'=' * 50}\n"
            f"  Ticks:          {self.ticks:,}\n"
            f"  Trades:         {len(self.trades)} "
            f"({self.buy_count} buys, {self.sell_count} sells)\n"
            f"  Initial:        ${self.initial_portfolio_usd:,.2f}\n"
            f"  Final:          ${self.final_portfolio_usd:,.2f}\n"
            f"  Return:         {self.return_pct:.2%}\n"
            f"  P&L:            ${self.total_pnl_usd:,.2f}\n"
            f"  Fees:           ${self.total_fees_usd:,.2f}\n"
            f"  Max Drawdown:   {self.max_drawdown_pct:.2%}\n"
            f"  HWM:            ${self.high_water_mark:,.2f}\n"
            f"  Final BTC:      {self.final_btc:.8f}\n"
            f"  Final USD:      ${self.final_usd:,.2f}\n"
        )


class BacktestEngine:
    """Replays price history through a simulated grid strategy.

    Simulates a simplified grid bot: places buy orders below mid
    and sell orders above mid at regular spacing. When price crosses
    a level, the order fills.
    """

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self._cfg = config or BacktestConfig()

    def run(self, prices: list[Decimal]) -> BacktestResult:
        """Run backtest on a sequence of prices.

        Args:
            prices: List of mid-prices (e.g., hourly or minutely).

        Returns:
            BacktestResult with all metrics.
        """
        cfg = self._cfg
        btc = cfg.initial_btc
        usd = cfg.initial_usd
        initial_portfolio = usd + btc * (prices[0] if prices else Decimal("0"))
        hwm = initial_portfolio

        result = BacktestResult(
            config=cfg,
            initial_portfolio_usd=initial_portfolio,
            high_water_mark=hwm,
        )

        if len(prices) < 2:
            result.final_btc = btc
            result.final_usd = usd
            result.final_portfolio_usd = initial_portfolio
            return result

        # Build grid levels relative to first price
        spacing = cfg.spacing_bps / Decimal("10000")
        fee_rate = cfg.maker_fee_bps / Decimal("10000")

        # Track last-filled level to avoid re-filling
        last_fill_prices: dict[str, Decimal] = {}

        prev_price = prices[0]
        for tick_idx, price in enumerate(prices[1:], start=1):
            result.ticks += 1

            # Compute grid levels around current price
            order_size = cfg.order_size_usd
            if cfg.auto_compound and initial_portfolio > 0:
                portfolio = usd + btc * price
                order_size = cfg.order_size_usd * (
                    portfolio / initial_portfolio
                )

            for level in range(1, cfg.grid_levels + 1):
                offset = spacing * level

                # Grid levels are placed relative to previous price (the "mid"
                # at order placement time). A fill occurs when the new price
                # crosses through a level.
                buy_price = prev_price * (1 - offset)
                sell_price = prev_price * (1 + offset)

                # Check if price crossed buy level (price went down)
                if price <= buy_price < prev_price:
                    qty = order_size / buy_price
                    fee = qty * buy_price * fee_rate
                    cost = qty * buy_price + fee

                    if cost <= usd:
                        buy_key = f"buy_{level}"
                        # Avoid re-filling same level within 5 ticks
                        last_tick = last_fill_prices.get(buy_key)
                        if last_tick is None or abs(
                            float(price - last_tick) / float(price),
                        ) > float(spacing):
                            usd -= cost
                            btc += qty
                            result.total_fees_usd += fee
                            result.buy_count += 1
                            result.trades.append(BacktestTrade(
                                tick=tick_idx, side="buy",
                                price=buy_price, qty=qty, fee=fee,
                            ))
                            last_fill_prices[buy_key] = price

                # Check if price crossed sell level (price went up)
                if price >= sell_price > prev_price:
                    qty = order_size / sell_price
                    if qty <= btc:
                        fee = qty * sell_price * fee_rate
                        proceeds = qty * sell_price - fee
                        cost_basis = qty * prev_price  # Simplified

                        sell_key = f"sell_{level}"
                        last_tick = last_fill_prices.get(sell_key)
                        if last_tick is None or abs(
                            float(price - last_tick) / float(price),
                        ) > float(spacing):
                            btc -= qty
                            usd += proceeds
                            pnl = proceeds - cost_basis
                            result.total_fees_usd += fee
                            result.total_pnl_usd += pnl
                            result.sell_count += 1
                            result.trades.append(BacktestTrade(
                                tick=tick_idx, side="sell",
                                price=sell_price, qty=qty,
                                fee=fee, pnl=pnl,
                            ))
                            last_fill_prices[sell_key] = price

            # Update portfolio metrics
            portfolio = usd + btc * price
            if portfolio > hwm:
                hwm = portfolio

            if hwm > 0:
                dd = float((hwm - portfolio) / hwm)
                if dd > result.max_drawdown_pct:
                    result.max_drawdown_pct = dd

            prev_price = price

        result.final_btc = btc
        result.final_usd = usd
        result.final_portfolio_usd = usd + btc * prices[-1]
        result.high_water_mark = hwm
        result.ticks = len(prices) - 1

        return result
