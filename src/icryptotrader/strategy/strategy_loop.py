"""Strategy Loop — main integration wiring for the spot grid bot.

Ties together all components in the Strategy Process:
  - Market data ingestion (from WS1 via ZMQ or direct)
  - Regime classification -> allocation limits
  - Grid computation -> desired levels
  - Tax agent veto -> sell-level gating
  - Delta skew -> asymmetric spacing
  - Order manager -> amend/add/cancel decisions
  - WS2 command dispatch
  - Fill handling -> FIFO ledger updates
  - Risk monitoring -> pause state management
  - Auto-compounding -> reinvest profits into order sizing
  - Telegram + Metrics wiring -> real-time observability

This is the orchestrator — it owns the tick loop and coordinates
all subsystems. Each tick:
  1. Ingest latest market data (price, book, trades)
  2. Update risk manager (portfolio value, drawdown)
  3. Classify regime
  4. Compute grid levels
  5. Apply tax agent gating and delta skew
  6. Auto-compound order sizing
  7. Run order manager decide_action() per slot
  8. Dispatch commands to WS2
"""

from __future__ import annotations

import logging
import time
from collections import deque
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from icryptotrader.metrics import MetricsRegistry
    from icryptotrader.notify.telegram import BotSnapshot
    from icryptotrader.strategy.ai_signal import AISignalEngine
    from icryptotrader.strategy.bollinger import BollingerSpacing

from icryptotrader.fee.fee_model import FeeModel  # noqa: TC001
from icryptotrader.inventory.inventory_arbiter import InventoryArbiter  # noqa: TC001
from icryptotrader.order.order_manager import Action, DesiredLevel, OrderManager
from icryptotrader.risk.delta_skew import DeltaSkew  # noqa: TC001
from icryptotrader.risk.risk_manager import RiskManager  # noqa: TC001
from icryptotrader.strategy.grid_engine import GridEngine  # noqa: TC001
from icryptotrader.strategy.regime_router import RegimeRouter  # noqa: TC001
from icryptotrader.tax.fifo_ledger import FIFOLedger  # noqa: TC001
from icryptotrader.tax.tax_agent import TaxAgent  # noqa: TC001
from icryptotrader.types import PauseState, Side

logger = logging.getLogger(__name__)


class StrategyLoop:
    """Main strategy orchestrator that runs one tick at a time.

    Not async — designed to be called from an event loop or timer.
    Each call to tick() runs one complete strategy cycle.

    Usage:
        loop = StrategyLoop(
            fee_model=fee_model,
            order_manager=order_manager,
            grid_engine=grid_engine,
            tax_agent=tax_agent,
            risk_manager=risk_manager,
            delta_skew=delta_skew,
            inventory=inventory,
            regime_router=regime_router,
            ledger=ledger,
        )
        # On each strategy tick (e.g., every 100ms):
        commands = loop.tick(mid_price=Decimal("85000"))
        for cmd in commands:
            await ws2.send(cmd["frame"])
    """

    def __init__(
        self,
        fee_model: FeeModel,
        order_manager: OrderManager,
        grid_engine: GridEngine,
        tax_agent: TaxAgent,
        risk_manager: RiskManager,
        delta_skew: DeltaSkew,
        inventory: InventoryArbiter,
        regime_router: RegimeRouter,
        ledger: FIFOLedger,
        eur_usd_rate: Decimal = Decimal("1.08"),
        ledger_path: Path | None = None,
        auto_compound: bool = False,
        compound_base_usd: Decimal = Decimal("5000"),
        base_order_size_usd: Decimal = Decimal("500"),
        metrics: MetricsRegistry | None = None,
        bollinger: BollingerSpacing | None = None,
        ai_signal: AISignalEngine | None = None,
        persistence_backend: str = "json",
    ) -> None:
        self._fee = fee_model
        self._om = order_manager
        self._grid = grid_engine
        self._tax = tax_agent
        self._risk = risk_manager
        self._skew = delta_skew
        self._inv = inventory
        self._regime = regime_router
        self._ledger = ledger
        self._eur_usd_rate = eur_usd_rate
        self._ledger_path = ledger_path
        self._metrics = metrics

        # Bollinger + ATR dynamic spacing
        self._bollinger = bollinger

        # AI signal engine (reads cached last_signal)
        self._ai_signal = ai_signal

        # Persistence backend
        self._persistence_backend = persistence_backend

        # Auto-compounding
        self._auto_compound = auto_compound
        self._compound_base_usd = compound_base_usd
        self._base_order_size_usd = base_order_size_usd

        # High/low tracker for Bollinger ATR feed
        self._price_window: deque[Decimal] = deque(maxlen=50)

        # Metrics
        self.ticks: int = 0
        self.commands_issued: int = 0
        self.ticks_skipped_risk: int = 0
        self.ticks_skipped_velocity: int = 0
        self.last_tick_duration_ms: float = 0.0
        self.fills_today: int = 0
        self.profit_today_usd: Decimal = Decimal("0")
        self._start_time = time.time()

    def set_eur_usd_rate(self, rate: Decimal) -> None:
        """Update EUR/USD rate from ECB service."""
        self._eur_usd_rate = rate

    def load_ledger(self) -> None:
        """Load FIFO ledger from disk at startup."""
        if not self._ledger_path:
            return
        if self._persistence_backend == "sqlite":
            db_path = self._ledger_path.with_suffix(".db")
            self._ledger.load_sqlite(db_path)
        else:
            self._ledger.load(self._ledger_path)

    def save_ledger(self) -> None:
        """Save FIFO ledger to disk (called automatically after fills)."""
        if not self._ledger_path:
            return
        if self._persistence_backend == "sqlite":
            db_path = self._ledger_path.with_suffix(".db")
            self._ledger.save_sqlite(db_path)
        else:
            self._ledger.save(self._ledger_path)

    def compound_order_size(self) -> Decimal:
        """Compute current order size with auto-compounding.

        Scales order_size_usd proportionally to portfolio growth
        above compound_base_usd. E.g., if portfolio doubled from
        $5000 to $10000, order size doubles from $500 to $1000.
        """
        if not self._auto_compound or self._compound_base_usd <= 0:
            return self._base_order_size_usd

        portfolio = self._inv.portfolio_value_usd
        if portfolio <= 0:
            return self._base_order_size_usd

        scale = portfolio / self._compound_base_usd
        return self._base_order_size_usd * scale

    def tick(self, mid_price: Decimal) -> list[dict[str, Any]]:
        """Run one strategy tick. Returns list of commands to dispatch.

        Each command dict has keys:
            - "type": "add" | "amend" | "cancel"
            - "slot_id": int
            - "params": dict (kwargs for WS2 send methods)
        """
        tick_start = time.monotonic()
        self.ticks += 1
        commands: list[dict[str, Any]] = []

        # 1. Update market data
        self._inv.update_price(mid_price)
        self._regime.update_price(mid_price)

        # 2. Check price velocity circuit breaker
        if self._risk.check_price_velocity(mid_price):
            self.ticks_skipped_velocity += 1
            self.last_tick_duration_ms = (time.monotonic() - tick_start) * 1000
            self._record_tick_metrics()
            return commands

        # 3. Update risk manager
        snap = self._inv.snapshot()
        risk_snap = self._risk.update_portfolio(
            btc_value_usd=snap.btc_value_usd,
            usd_balance=snap.usd_balance,
        )

        # 4. Check pause state
        self._risk.set_tax_locked(self._tax.is_tax_locked())

        if not self._risk.is_trading_allowed:
            self.ticks_skipped_risk += 1
            self.last_tick_duration_ms = (time.monotonic() - tick_start) * 1000
            self._record_tick_metrics()
            return commands

        # 5. Classify regime
        regime_decision = self._regime.classify()

        # Apply risk manager regime override if suggested
        if risk_snap.suggested_regime is not None:
            self._regime.override_regime(
                risk_snap.suggested_regime, "risk_manager",
            )
            regime_decision = self._regime.classify()

        self._inv.set_regime(regime_decision.regime)

        # 6. Determine grid level counts
        num_buy = regime_decision.grid_levels_buy
        num_sell = regime_decision.grid_levels_sell

        # Apply risk-suggested level reduction
        if risk_snap.suggested_grid_levels is not None:
            num_buy = min(num_buy, risk_snap.suggested_grid_levels)
            num_sell = min(num_sell, risk_snap.suggested_grid_levels)

        # Apply tax agent sell-level gating
        rec_sell = self._tax.recommended_sell_levels()
        if rec_sell >= 0:
            num_sell = min(num_sell, rec_sell)

        # 7. Compute delta skew
        limits = self._inv.current_limits()
        skew_result = self._skew.compute(
            btc_alloc_pct=snap.btc_allocation_pct,
            target_pct=limits.target_pct,
        )

        # 8. Compute grid levels with skewed spacings and regime-scaled sizing
        # Use Bollinger+ATR dynamic spacing if available, else fee-model static
        base_spacing = self._grid.optimal_spacing_bps()
        if self._bollinger is not None:
            self._price_window.append(mid_price)
            high = max(self._price_window)
            low = min(self._price_window)
            bb_state = self._bollinger.update(mid_price, high=high, low=low)
            if bb_state is not None:
                base_spacing = bb_state.suggested_spacing_bps

        # Apply AI signal bias to spacing
        ai_bias_bps = Decimal("0")
        if self._ai_signal is not None:
            sig = self._ai_signal.last_signal
            if sig.confidence > 0 and sig.suggested_bias_bps != 0:
                ai_bias_bps = sig.suggested_bias_bps * Decimal(
                    str(sig.confidence * self._ai_signal.weight),
                )

        buy_spacing, sell_spacing = self._skew.apply_to_spacing(
            base_spacing, skew_result,
        )
        # AI bias: positive = bullish → tighter buys, wider sells
        if ai_bias_bps != 0:
            buy_spacing = max(Decimal("5"), buy_spacing - ai_bias_bps)
            sell_spacing = max(Decimal("5"), sell_spacing + ai_bias_bps)

        # Auto-compound: scale order size with portfolio growth
        size_scale = Decimal(str(regime_decision.order_size_scale))
        if self._auto_compound:
            compound_size = self.compound_order_size()
            if compound_size != self._base_order_size_usd:
                compound_factor = compound_size / self._base_order_size_usd
                size_scale = size_scale * compound_factor

        self._grid.compute_grid(
            mid_price=mid_price,
            num_buy_levels=num_buy,
            num_sell_levels=num_sell,
            spacing_bps=base_spacing,
            buy_spacing_bps=buy_spacing,
            sell_spacing_bps=sell_spacing,
            buy_qty_scale=size_scale,
            sell_qty_scale=size_scale,
        )

        # Deactivate sell levels if tax-locked
        if self._risk.pause_state == PauseState.TAX_LOCK_ACTIVE:
            self._grid.deactivate_sell_levels(keep=0)

        # 9. Get desired levels
        desired = self._grid.desired_levels()
        slots = self._om.slots

        # 10. Run order manager per slot
        num_slots = min(len(desired), len(slots))
        for i in range(num_slots):
            slot = slots[i]
            level = desired[i]

            # Check allocation before issuing buys
            if level is not None and level.side == Side.BUY:
                allowed = self._inv.check_buy(level.qty)
                if allowed <= 0:
                    level = None
                elif allowed < level.qty:
                    level = DesiredLevel(
                        price=level.price, qty=allowed, side=Side.BUY,
                    )

            # Check allocation before issuing sells
            if level is not None and level.side == Side.SELL:
                allowed = self._inv.check_sell(level.qty)
                if allowed <= 0:
                    level = None
                elif allowed < level.qty:
                    level = DesiredLevel(
                        price=level.price, qty=allowed, side=Side.SELL,
                    )

            action = self._om.decide_action(slot, level)
            cmd = self._dispatch_action(slot, action, i)
            if cmd is not None:
                commands.append(cmd)

        # Cancel excess slots (if we have more slots than desired levels)
        for i in range(num_slots, len(slots)):
            slot = slots[i]
            action = self._om.decide_action(slot, None)
            cmd = self._dispatch_action(slot, action, i)
            if cmd is not None:
                commands.append(cmd)

        self.commands_issued += len(commands)
        self.last_tick_duration_ms = (time.monotonic() - tick_start) * 1000
        self._record_tick_metrics()
        return commands

    def on_fill(self, slot: Any, fill_data: dict[str, Any]) -> None:
        """Handle a fill event — update FIFO ledger.

        Register this as the OrderManager's fill callback.
        """
        side = getattr(slot, "side", None)
        fill_qty = Decimal(str(fill_data.get("last_qty", "0")))
        fill_price = Decimal(str(fill_data.get("last_price", "0")))
        fee_usd = Decimal(str(fill_data.get("fee", "0")))
        order_id = fill_data.get("order_id", "")
        trade_id = fill_data.get("trade_id", "")

        self.fills_today += 1

        if side == Side.BUY:
            self._ledger.add_lot(
                quantity_btc=fill_qty,
                purchase_price_usd=fill_price,
                purchase_fee_usd=fee_usd,
                eur_usd_rate=self._eur_usd_rate,
                exchange_order_id=order_id,
                exchange_trade_id=trade_id,
                source_engine="grid",
                grid_level=getattr(slot, "slot_id", None),
            )
        elif side == Side.SELL:
            try:
                disposals = self._ledger.sell_fifo(
                    quantity_btc=fill_qty,
                    sale_price_usd=fill_price,
                    sale_fee_usd=fee_usd,
                    eur_usd_rate=self._eur_usd_rate,
                    exchange_order_id=order_id,
                    exchange_trade_id=trade_id,
                )
                # Track P&L from disposals
                for d in disposals:
                    self.profit_today_usd += d.gain_loss_eur * self._eur_usd_rate
            except ValueError:
                logger.exception(
                    "FIFO sell failed — ledger mismatch, triggering risk "
                    "pause. fill_qty=%s fill_price=%s order_id=%s",
                    fill_qty, fill_price, order_id,
                )
                self._risk.force_risk_pause()

        # Record fill in metrics
        if self._metrics:
            side_label = side.value if side else "unknown"
            self._metrics.counter_inc(
                "fills_total", labels={"side": side_label},
            )

        # Persist ledger to disk after every fill
        self.save_ledger()

    def build_ai_context(self) -> dict[str, Any]:
        """Build market context dict for the AI signal engine."""
        snap = self._inv.snapshot()
        regime_decision = self._regime.classify()
        return {
            "mid_price": snap.btc_price_usd,
            "spread_bps": self._grid.spacing_bps,
            "volatility_pct": self._regime._ewma_vol
            if hasattr(self._regime, "_ewma_vol")
            else 0.0,
            "regime": regime_decision.regime.value,
            "btc_allocation_pct": snap.btc_allocation_pct * 100,
            "drawdown_pct": self._risk.drawdown_pct * 100,
            "price_change_1h_pct": 0.0,
            "price_change_24h_pct": 0.0,
            "book_imbalance": 0.0,
            "ytd_taxable_gain_eur": float(self._ledger.taxable_gain_ytd()),
        }

    def bot_snapshot(self) -> BotSnapshot:
        """Provide a snapshot for the Telegram bot (BotDataProvider).

        This implements the BotDataProvider protocol so the strategy
        loop can be directly set as the Telegram bot's data provider.
        """
        from icryptotrader.notify.telegram import BotSnapshot

        snap = self._inv.snapshot()
        return BotSnapshot(
            portfolio_value_usd=snap.portfolio_value_usd,
            btc_balance=snap.btc_balance,
            usd_balance=snap.usd_balance,
            btc_allocation_pct=snap.btc_allocation_pct,
            drawdown_pct=self._risk.drawdown_pct,
            pause_state=self._risk.pause_state.name,
            high_water_mark_usd=self._risk.high_water_mark,
            regime=self._regime.current_regime.value
            if hasattr(self._regime, "current_regime")
            else snap.regime.value,
            active_orders=sum(
                1 for s in self._om.slots
                if hasattr(s, "state") and s.state.name == "LIVE"
            ),
            grid_levels=self._grid._num_levels
            if hasattr(self._grid, "_num_levels")
            else 0,
            ticks=self.ticks,
            commands_issued=self.commands_issued,
            last_tick_ms=self.last_tick_duration_ms,
            uptime_sec=time.time() - self._start_time,
            ytd_taxable_gain_eur=self._ledger.taxable_gain_ytd(),
            tax_free_btc=self._ledger.tax_free_btc(),
            locked_btc=self._ledger.locked_btc(),
            sellable_ratio=self._ledger.sellable_ratio(),
            days_until_unlock=self._ledger.days_until_next_free(),
            open_lots=len(self._ledger.open_lots()),
            ai_direction=self._ai_signal.last_signal.direction.name
            if self._ai_signal
            else "NEUTRAL",
            ai_confidence=self._ai_signal.last_signal.confidence
            if self._ai_signal
            else 0.0,
            ai_last_latency_ms=self._ai_signal.last_signal.latency_ms
            if self._ai_signal
            else 0.0,
            ai_provider=self._ai_signal._provider
            if self._ai_signal
            else "",
            ai_call_count=self._ai_signal._call_count
            if self._ai_signal
            else 0,
            ai_error_count=self._ai_signal._error_count
            if self._ai_signal
            else 0,
            fills_today=self.fills_today,
            profit_today_usd=self.profit_today_usd,
            eur_usd_rate=self._eur_usd_rate,
            # Blow-through overhaul fields
            blow_through_mode=self._tax._blow_through_mode,
            vault_btc=self._tax.vault_lot_btc(),
            vault_lock_priority=self._tax._vault_lock_priority,
            geometric_spacing=getattr(self._grid, "_geometric", True),
            grid_spacing_bps=self._grid.spacing_bps,
            btc_price_usd=self._inv.btc_price,
            twap_budget_remaining_pct=self._twap_budget_pct(),
            wash_sale_active_lots=len(self._tax._harvest_timestamps),
            grid_orders=self._grid_order_tuples(),
            is_paused=self._risk.pause_state.name != "ACTIVE_TRADING",
        )

    def _twap_budget_pct(self) -> float:
        """Return TWAP budget remaining as a fraction 0..1."""
        total_usd = self._inv.portfolio_value_usd
        if total_usd <= 0:
            return 1.0
        remaining = self._inv._twap_remaining_usd(total_usd)
        budget = total_usd * Decimal(str(self._inv._max_rebalance_pct_per_min))
        if budget <= 0:
            return 1.0
        return float(min(remaining / budget, Decimal("1")))

    def _grid_order_tuples(self) -> list[tuple[str, str, str, str]]:
        """Return grid orders as (side, price, qty, state) string tuples."""
        orders = []
        for slot in self._om.slots:
            if hasattr(slot, "state") and slot.state.name != "EMPTY":
                orders.append((
                    slot.side.value,
                    f"${slot.price:,.1f}" if slot.price else "—",
                    f"{slot.qty:.6f}" if slot.qty else "—",
                    slot.state.name.lower(),
                ))
        return orders

    def _record_tick_metrics(self) -> None:
        """Push tick metrics to the metrics registry."""
        if not self._metrics:
            return
        self._metrics.histogram_observe(
            "tick_latency_ms", self.last_tick_duration_ms,
        )
        self._metrics.gauge_set("ticks_total", float(self.ticks))
        self._metrics.gauge_set(
            "drawdown_pct", self._risk.drawdown_pct,
        )
        self._metrics.gauge_set(
            "pause_state",
            float(self._risk.pause_state.value),
        )

    def _dispatch_action(
        self, slot: Any, action: Any, slot_index: int,
    ) -> dict[str, Any] | None:
        """Convert an Action into a command dict for WS2 dispatch.

        Checks the rate limiter before dispatching add/amend commands.
        Cancels are never throttled (Kraken always accepts them).
        """
        if isinstance(action, Action.AddOrder):
            if self._om._rate_limiter.should_throttle("add_order"):
                return None
            params = self._om.prepare_add(slot, action)
            return {
                "type": "add", "slot_id": slot_index, "params": params,
            }

        if isinstance(action, Action.AmendOrder):
            if self._om._rate_limiter.should_throttle("amend_order"):
                return None
            params = self._om.prepare_amend(slot, action)
            return {
                "type": "amend", "slot_id": slot_index, "params": params,
            }

        if isinstance(action, Action.CancelOrder):
            params = self._om.prepare_cancel(slot, action)
            return {
                "type": "cancel", "slot_id": slot_index, "params": params,
            }

        return None  # Noop
