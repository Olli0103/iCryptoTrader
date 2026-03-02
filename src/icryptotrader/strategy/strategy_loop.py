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

import asyncio
import bisect
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from icryptotrader.metrics import MetricsRegistry
    from icryptotrader.notify.telegram import BotSnapshot
    from icryptotrader.strategy.ai_signal import AISignalEngine
    from icryptotrader.strategy.avellaneda_stoikov import AvellanedaStoikov
    from icryptotrader.strategy.bollinger import BollingerSpacing
    from icryptotrader.ws.book_manager import OrderBook

from icryptotrader.fee.fee_model import FeeModel  # noqa: TC001
from icryptotrader.fee.volume_quota import VolumeQuota
from icryptotrader.inventory.inventory_arbiter import InventoryArbiter  # noqa: TC001
from icryptotrader.order.order_manager import Action, DesiredLevel, OrderManager
from icryptotrader.risk.cross_exchange_oracle import CrossExchangeOracle
from icryptotrader.risk.delta_skew import DeltaSkew  # noqa: TC001
from icryptotrader.risk.hedge_manager import HedgeAction  # noqa: TC001
from icryptotrader.risk.mark_out_tracker import MarkOutTracker
from icryptotrader.risk.risk_manager import RiskManager  # noqa: TC001
from icryptotrader.risk.trade_flow_imbalance import TradeFlowImbalance
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

    # Time-decimation interval for price history deques.
    # At 1 sample/sec, maxlen=3600 covers exactly 1 hour and
    # maxlen=86400 covers exactly 24 hours regardless of tick rate.
    _PRICE_HISTORY_SAMPLE_SEC = 1.0

    # Cross-connection heartbeat: if WS1 book hasn't been updated in this
    # many seconds, we consider the public feed stale and issue emergency
    # cancel_all to protect against trading on outdated market data.
    _WS1_STALE_THRESHOLD_SEC = 2.5

    # Maximum orders per batch_add frame (Kraken WS v2 limit).
    _MAX_BATCH_SIZE = 15

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
        book: OrderBook | None = None,
        avellaneda_stoikov: AvellanedaStoikov | None = None,
        cross_exchange_oracle: CrossExchangeOracle | None = None,
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

        # L2 order book (for OBI and validated mid-price)
        self._book = book

        # Avellaneda-Stoikov optimal spacing model
        self._as = avellaneda_stoikov

        # Auto-compounding
        self._auto_compound = auto_compound
        self._compound_base_usd = compound_base_usd
        self._base_order_size_usd = base_order_size_usd

        # Trade Flow Imbalance: tracks taker buy/sell volume from public trades.
        # More resistant to L2 spoofing than naive OBI from resting book levels.
        self._tfi = TradeFlowImbalance()

        # Mark-out tracker: measures adverse selection at T+1s/T+10s/T+60s.
        self._mark_out = MarkOutTracker()

        # Cross-connection heartbeat: tracks whether WS1 data is stale.
        self._ws1_stale_cancel_sent = False

        # Cross-exchange oracle: monitors Binance for toxic flow detection.
        # When Binance mid-price drops sharply below Kraken mid, HFTs will
        # sweep our resting bids ~50-100ms later. Preemptive cancel protects.
        self._oracle = cross_exchange_oracle
        self._oracle_cancel_sent = False

        # Volume Quota: prevents fee-tier death spiral when mark-out tracker
        # forces wider spreads → lower fill rate → lower 30-day volume →
        # higher fees → even wider spreads.
        self._volume_quota = VolumeQuota(fee_model=fee_model)

        # Trade event buffer: holds public trades until the next tick when
        # the L2 book is confirmed fresh. Prevents acting on TFI signal
        # computed from a trade that caused a book update we haven't yet
        # received (WS trade vs book event race condition).
        self._trade_buffer: list[tuple[str, Decimal, Decimal]] = []

        # Debounced ledger persistence: single-thread executor + dirty flag.
        # Prevents thread-pool starvation during burst fills (flash crashes
        # can produce 50-100 fills in seconds, each of which triggers a save).
        self._ledger_dirty = False
        self._ledger_save_lock = threading.Lock()
        self._ledger_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ledger-save")
        self._ledger_save_pending = False  # A save task is already scheduled
        self._ledger_debounce_sec: float = 0.5  # Coalesce saves within 500ms

        # High/low tracker for Bollinger ATR feed
        self._price_window: deque[Decimal] = deque(maxlen=50)

        # Hedge action from HedgeManager (set before each tick)
        self._hedge_action: HedgeAction | None = None

        # Price history for 1h/24h change — time-decimated.
        # Only one entry per second is stored, so maxlen matches the actual
        # time horizon regardless of tick frequency.  At 10 ticks/sec the old
        # approach would fill maxlen=86400 in 2.4 hours, giving the AI and
        # regime classifiers a wildly compressed view of macro price action.
        self._price_history_1h: deque[tuple[float, Decimal]] = deque(maxlen=3600)
        self._price_history_24h: deque[tuple[float, Decimal]] = deque(maxlen=86400)
        self._price_history_1h_last_ts: float = 0.0
        self._price_history_24h_last_ts: float = 0.0

        # Metrics
        self.ticks: int = 0
        self.commands_issued: int = 0
        self.ticks_skipped_risk: int = 0
        self.ticks_skipped_velocity: int = 0
        self.last_tick_duration_ms: float = 0.0
        self.fills_today: int = 0
        self.profit_today_usd: Decimal = Decimal("0")
        self._start_time = time.time()

    @property
    def tfi(self) -> TradeFlowImbalance:
        """Trade Flow Imbalance tracker (for external wiring)."""
        return self._tfi

    @property
    def mark_out_tracker(self) -> MarkOutTracker:
        """Mark-out tracker (for external wiring)."""
        return self._mark_out

    @property
    def oracle(self) -> CrossExchangeOracle | None:
        """Cross-exchange oracle (for external wiring)."""
        return self._oracle

    @property
    def volume_quota(self) -> VolumeQuota:
        """Volume quota monitor (for external wiring)."""
        return self._volume_quota

    def record_public_trade(
        self, side: str, qty: Decimal, price: Decimal,
    ) -> None:
        """Buffer a public trade from the Kraken trade channel.

        Trades are buffered until the next tick when the L2 book is confirmed
        fresh.  This prevents the WS event race where a large taker trade
        triggers a TFI update before the corresponding book update arrives,
        causing the grid to be recalculated against a stale book state.
        """
        self._trade_buffer.append((side, qty, price))

    def set_eur_usd_rate(self, rate: Decimal) -> None:
        """Update EUR/USD rate from ECB service."""
        self._eur_usd_rate = rate

    def set_hedge_action(self, action: HedgeAction | None) -> None:
        """Set the current hedge action from HedgeManager."""
        self._hedge_action = action

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
        """Save FIFO ledger to disk (called automatically after fills).

        Uses debounced persistence: marks ledger dirty and schedules a save
        on a dedicated single-thread executor. Multiple fills within the
        debounce window (500ms) are coalesced into a single disk write,
        preventing thread pool starvation during burst fills (flash crashes).
        """
        if not self._ledger_path:
            return
        with self._ledger_save_lock:
            self._ledger_dirty = True
            if self._ledger_save_pending:
                return  # A save is already scheduled; it will pick up the dirty flag
            self._ledger_save_pending = True

        try:
            loop = asyncio.get_running_loop()
            loop.call_later(self._ledger_debounce_sec, self._submit_ledger_save)
        except RuntimeError:
            # No running loop — save synchronously (startup / shutdown)
            self._save_ledger_sync()

    def save_ledger_now(self) -> None:
        """Force an immediate synchronous ledger save (for shutdown)."""
        if not self._ledger_path:
            return
        self._save_ledger_sync()

    def _submit_ledger_save(self) -> None:
        """Submit the actual save to the dedicated single-thread executor."""
        self._ledger_executor.submit(self._save_ledger_sync)

    def _save_ledger_sync(self) -> None:
        """Synchronous ledger save on the dedicated executor thread.

        Drains the dirty flag so concurrent fills during write are coalesced
        into the next scheduled save.
        """
        if not self._ledger_path:
            return
        with self._ledger_save_lock:
            if not self._ledger_dirty:
                self._ledger_save_pending = False
                return
            self._ledger_dirty = False
            self._ledger_save_pending = False
        try:
            if self._persistence_backend == "sqlite":
                db_path = self._ledger_path.with_suffix(".db")
                self._ledger.save_sqlite(db_path)
            else:
                self._ledger.save(self._ledger_path)
        except Exception:
            logger.exception("Ledger save failed")
            # Re-mark dirty so next save attempt retries
            with self._ledger_save_lock:
                self._ledger_dirty = True

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

        # 0. Cross-connection heartbeat: if WS1 book data is stale, emergency
        # cancel all orders and enter risk pause.  Trading on stale book data
        # means our grid levels are anchored to an outdated mid-price while
        # the real market may have moved significantly.
        if self._book is not None and self._book.last_update_ts > 0:
            ws1_age = tick_start - self._book.last_update_ts
            if ws1_age > self._WS1_STALE_THRESHOLD_SEC:
                if not self._ws1_stale_cancel_sent:
                    logger.warning(
                        "WS1 stale (%.1fs since last update) — "
                        "issuing cancel_all and entering RISK_PAUSE",
                        ws1_age,
                    )
                    commands.append({"type": "cancel_all", "slot_id": -1, "params": {}})
                    self._risk.force_risk_pause()
                    self._ws1_stale_cancel_sent = True
                self.last_tick_duration_ms = (tick_start - tick_start) * 1000
                self._record_tick_metrics()
                return commands
            elif self._ws1_stale_cancel_sent:
                # WS1 recovered — clear the stale flag
                logger.info("WS1 recovered (age=%.1fs), resuming", ws1_age)
                self._ws1_stale_cancel_sent = False

        # 0b. Flush buffered public trades into TFI now that the book is
        # up-to-date.  This fixes the WS event race: a taker trade arrives
        # before the corresponding L2 book update, so we must not process
        # the TFI signal until the book reflects the trade's impact.
        if self._trade_buffer:
            for side, qty, price in self._trade_buffer:
                self._tfi.record_trade(side=side, qty=qty, price=price)
            self._trade_buffer.clear()

        # 0c. Mark-out tracker: check pending fills for T+X price marks.
        if self._book is not None and self._book.is_valid:
            self._mark_out.check_mark_outs(self._book.mid_price)

        # 0d. Cross-exchange oracle: if Binance mid-price has dropped sharply
        # below Kraken mid, HFT arbitrageurs will sweep our resting bids
        # within ~50-100ms. Issue preemptive cancel_all before they arrive.
        if (
            self._oracle is not None
            and self._book is not None
            and self._book.is_valid
        ):
            if self._oracle.should_preemptive_cancel(self._book.mid_price):
                if not self._oracle_cancel_sent:
                    logger.warning(
                        "Cross-exchange oracle: preemptive cancel_all "
                        "(Binance mid=%.2f, Kraken mid=%.2f)",
                        self._oracle.binance_mid, self._book.mid_price,
                    )
                    commands.append({"type": "cancel_all", "slot_id": -1, "params": {}})
                    self._oracle_cancel_sent = True
                    self.last_tick_duration_ms = (time.monotonic() - tick_start) * 1000
                    self._record_tick_metrics()
                    return commands
            elif self._oracle_cancel_sent:
                # Oracle divergence resolved
                logger.info("Cross-exchange oracle: divergence resolved, resuming")
                self._oracle_cancel_sent = False

        # 1. Update market data
        self._inv.update_price(mid_price)
        self._inv.update_deviation_tracker()
        self._regime.update_price(mid_price)

        # Track price history for 1h/24h change calculations.
        # Time-decimated: only one sample per _PRICE_HISTORY_SAMPLE_SEC to
        # ensure maxlen corresponds to real wall-clock time, not tick count.
        now_ts = time.time()
        interval = self._PRICE_HISTORY_SAMPLE_SEC
        if now_ts - self._price_history_1h_last_ts >= interval:
            self._price_history_1h.append((now_ts, mid_price))
            self._price_history_1h_last_ts = now_ts
        if now_ts - self._price_history_24h_last_ts >= interval:
            self._price_history_24h.append((now_ts, mid_price))
            self._price_history_24h_last_ts = now_ts

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

        # P0-3: Apply hedge action (buy level cap + sell boost + spacing tighten)
        sell_spacing_tighten = 0.0
        if self._hedge_action is not None and self._hedge_action.active:
            if self._hedge_action.buy_level_cap is not None:
                num_buy = min(num_buy, self._hedge_action.buy_level_cap)
            num_sell = num_sell + self._hedge_action.sell_level_boost
            sell_spacing_tighten = self._hedge_action.sell_spacing_tighten_pct

        # 7. Compute delta skew with blended microstructure signal.
        # Trade Flow Imbalance (TFI) from executed trades is more reliable
        # than naive L2 OBI which can be spoofed with phantom orders.
        # Blend: 70% TFI (unfakeable) + 30% OBI (faster reaction).
        # Dead-band: when allocation drift is within tolerance (±2%), suppress
        # the allocation-based skew to avoid constantly fighting trivial drift.
        limits = self._inv.current_limits()
        obi = 0.0
        tfi = self._tfi.compute()
        if self._book is not None and self._book.is_valid:
            obi = self._book.order_book_imbalance()

        # Blend TFI and OBI for a spoof-resistant microstructure signal.
        # TFI dominates when trades are flowing; OBI fills in during quiet
        # periods when trade stream is sparse.
        if self._tfi.trade_count > 0:
            blended_signal = 0.7 * tfi + 0.3 * obi
        else:
            blended_signal = obi  # No trades yet — fall back to OBI

        self._regime.update_order_book_imbalance(blended_signal)

        if self._inv.is_within_dead_band():
            # Within dead-band: zero out allocation deviation, keep signal only
            skew_result = self._skew.compute(
                btc_alloc_pct=limits.target_pct,  # pretend we're at target
                target_pct=limits.target_pct,
                obi=blended_signal,
            )
        else:
            skew_result = self._skew.compute(
                btc_alloc_pct=snap.btc_allocation_pct,
                target_pct=limits.target_pct,
                obi=blended_signal,
            )

        # 8. Compute grid levels with skewed spacings and regime-scaled sizing
        fee_floor = self._grid.optimal_spacing_bps()

        # Volume Quota: when 30-day volume is close to dropping a fee tier,
        # tighten the fee floor to generate volume and prevent the death
        # spiral (wider spreads → less volume → higher fees → wider spreads).
        vq_status = self._volume_quota.assess()
        if vq_status.tier_at_risk:
            fee_floor = fee_floor * vq_status.spacing_override_mult

        # Avellaneda-Stoikov: when enabled, computes optimal spread and
        # inventory skew in one model, replacing Bollinger + DeltaSkew.
        if self._as is not None:
            inv_delta = snap.btc_allocation_pct - limits.target_pct
            # Time-decay: scale inventory delta by duration-based urgency
            # multiplier so long-held deviations produce stronger mean-reversion.
            td_mult = self._inv.time_decay_multiplier()
            as_result = self._as.compute(
                volatility_bps=Decimal(str(self._regime.ewma_volatility * 10000)),
                inventory_delta=Decimal(str(inv_delta)),
                fee_floor_bps=fee_floor,
                obi=obi,
                time_decay_mult=td_mult,
            )
            base_spacing = as_result.buy_spacing_bps
            buy_spacing = as_result.buy_spacing_bps
            sell_spacing = as_result.sell_spacing_bps
        else:
            # Fallback: Bollinger+ATR dynamic spacing + DeltaSkew
            base_spacing = fee_floor
            if self._bollinger is not None:
                self._price_window.append(mid_price)
                high = max(self._price_window)
                low = min(self._price_window)
                bb_state = self._bollinger.update(mid_price, high=high, low=low)
                if bb_state is not None:
                    base_spacing = bb_state.suggested_spacing_bps
            buy_spacing, sell_spacing = self._skew.apply_to_spacing(
                base_spacing, skew_result,
            )

        # Apply hedge sell spacing tightening (inverse_grid strategy)
        if sell_spacing_tighten > 0:
            sell_spacing = sell_spacing * Decimal(str(1.0 - sell_spacing_tighten))
            sell_spacing = max(Decimal("1"), sell_spacing)

        # Apply AI signal bias to spacing
        ai_bias_bps = Decimal("0")
        if self._ai_signal is not None:
            sig = self._ai_signal.last_signal
            if sig.confidence > 0 and sig.suggested_bias_bps != 0:
                ai_bias_bps = sig.suggested_bias_bps * Decimal(
                    str(sig.confidence * self._ai_signal.weight),
                )
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
                # §42 AO: block buys during wash sale cooldown after harvest
                if self._tax.is_buy_blocked_by_wash_sale():
                    level = None
                else:
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

        # 11. Aggregate add commands into batch_add frames to reduce
        # rate limit consumption. Kraken WS v2 batch_add sends up to 15
        # orders per frame at the cost of 1 rate-limit counter increment.
        commands = self._aggregate_batch_adds(commands)

        self.commands_issued += len(commands)
        self.last_tick_duration_ms = (time.monotonic() - tick_start) * 1000
        self._record_tick_metrics()
        return commands

    def _aggregate_batch_adds(
        self, commands: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Aggregate individual add commands into batch_add frames.

        Kraken WS v2 batch_add sends up to 15 orders per frame,
        consuming only 1 rate-limit counter increment instead of N.
        Amend and cancel commands pass through unchanged.
        """
        adds: list[dict[str, Any]] = []
        others: list[dict[str, Any]] = []

        for cmd in commands:
            if cmd.get("type") == "add":
                adds.append(cmd)
            else:
                others.append(cmd)

        if len(adds) <= 1:
            return commands  # No batching benefit for 0-1 adds

        # Split adds into chunks of _MAX_BATCH_SIZE
        result = list(others)
        for i in range(0, len(adds), self._MAX_BATCH_SIZE):
            chunk = adds[i : i + self._MAX_BATCH_SIZE]
            batch_orders = [cmd["params"] for cmd in chunk]
            slot_ids = [cmd["slot_id"] for cmd in chunk]
            result.append({
                "type": "batch_add",
                "slot_ids": slot_ids,
                "params": {"orders": batch_orders},
            })

        return result

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

        # Record fill for T+X mark-out tracking (adverse selection measurement).
        # Uses the book mid-price at fill time as the T+0 reference.
        if side is not None and fill_price > 0:
            mid = (
                self._book.mid_price
                if self._book is not None and self._book.is_valid
                else fill_price
            )
            self._mark_out.record_fill(
                fill_price=fill_price,
                side=side.value,
                qty=fill_qty,
                mid_price=mid,
            )

        # Record fill volume for Volume Quota tier-defense tracking.
        if fill_price > 0 and fill_qty > 0:
            self._volume_quota.record_fill_volume(fill_price * fill_qty)

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
            "price_change_1h_pct": self._compute_price_change(self._price_history_1h, 3600),
            "price_change_24h_pct": self._compute_price_change(self._price_history_24h, 86400),
            "book_imbalance": self._book.order_book_imbalance()
            if self._book and self._book.is_valid
            else 0.0,
            "trade_flow_imbalance": self._tfi.compute(),
            "adverse_selection_bps": self._mark_out.stats().suggested_adverse_bps,
            "ytd_taxable_gain_eur": float(self._ledger.taxable_gain_ytd()),
        }

    @staticmethod
    def _compute_price_change(
        history: deque[tuple[float, Decimal]], window_sec: int,
    ) -> float:
        """Compute price change percentage over a time window.

        Uses bisect for O(log N) lookup instead of linear scan.
        Since the deque is time-ordered (monotonically increasing timestamps),
        we extract timestamps into a list for binary search.
        """
        if len(history) < 2:
            return 0.0
        now = history[-1][0]
        cutoff = now - window_sec

        # Binary search for the cutoff timestamp.
        # bisect_left finds the insertion point for cutoff in the sorted
        # timestamp sequence — the entry at that index is the oldest
        # sample within our desired window.
        timestamps = [entry[0] for entry in history]
        idx = bisect.bisect_left(timestamps, cutoff)
        if idx >= len(history):
            idx = len(history) - 1

        oldest_price = history[idx][1]
        current_price = history[-1][1]
        if oldest_price <= 0:
            return 0.0
        return float((current_price - oldest_price) / oldest_price) * 100

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
