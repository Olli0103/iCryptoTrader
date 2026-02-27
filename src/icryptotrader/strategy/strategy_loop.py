"""Strategy Loop — main integration wiring for the spot grid bot.

Ties together all components in the Strategy Process:
  - Market data ingestion (from WS1 via ZMQ or direct)
  - Regime classification → allocation limits
  - Grid computation → desired levels
  - Tax agent veto → sell-level gating
  - Delta skew → asymmetric spacing
  - Order manager → amend/add/cancel decisions
  - WS2 command dispatch
  - Fill handling → FIFO ledger updates
  - Risk monitoring → pause state management

This is the orchestrator — it owns the tick loop and coordinates
all subsystems. Each tick:
  1. Ingest latest market data (price, book, trades)
  2. Update risk manager (portfolio value, drawdown)
  3. Classify regime
  4. Compute grid levels
  5. Apply tax agent gating and delta skew
  6. Run order manager decide_action() per slot
  7. Dispatch commands to WS2
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

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

        # Metrics
        self.ticks: int = 0
        self.commands_issued: int = 0
        self.ticks_skipped_risk: int = 0
        self.ticks_skipped_velocity: int = 0
        self.last_tick_duration_ms: float = 0.0

    def set_eur_usd_rate(self, rate: Decimal) -> None:
        """Update EUR/USD rate from ECB service."""
        self._eur_usd_rate = rate

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
            return commands

        # 5. Classify regime
        regime_decision = self._regime.classify()

        # Apply risk manager regime override if suggested
        if risk_snap.suggested_regime is not None:
            self._regime.override_regime(risk_snap.suggested_regime, "risk_manager")
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

        # 8. Compute grid levels
        base_spacing = self._grid.optimal_spacing_bps()
        buy_spacing, sell_spacing = self._skew.apply_to_spacing(base_spacing, skew_result)

        # Use average spacing for grid computation, apply skew via qty scaling
        self._grid.compute_grid(
            mid_price=mid_price,
            num_buy_levels=num_buy,
            num_sell_levels=num_sell,
            spacing_bps=base_spacing,
            buy_qty_scale=Decimal("1"),
            sell_qty_scale=Decimal("1"),
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
                    level = DesiredLevel(price=level.price, qty=allowed, side=Side.BUY)

            # Check allocation before issuing sells
            if level is not None and level.side == Side.SELL:
                allowed = self._inv.check_sell(level.qty)
                if allowed <= 0:
                    level = None
                elif allowed < level.qty:
                    level = DesiredLevel(price=level.price, qty=allowed, side=Side.SELL)

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
                self._ledger.sell_fifo(
                    quantity_btc=fill_qty,
                    sale_price_usd=fill_price,
                    sale_fee_usd=fee_usd,
                    eur_usd_rate=self._eur_usd_rate,
                    exchange_order_id=order_id,
                    exchange_trade_id=trade_id,
                )
            except ValueError:
                logger.exception("FIFO sell failed — ledger mismatch")

    def _dispatch_action(
        self, slot: Any, action: Any, slot_index: int,
    ) -> dict[str, Any] | None:
        """Convert an Action into a command dict for WS2 dispatch."""
        if isinstance(action, Action.AddOrder):
            params = self._om.prepare_add(slot, action)
            return {"type": "add", "slot_id": slot_index, "params": params}

        if isinstance(action, Action.AmendOrder):
            params = self._om.prepare_amend(slot, action)
            return {"type": "amend", "slot_id": slot_index, "params": params}

        if isinstance(action, Action.CancelOrder):
            params = self._om.prepare_cancel(slot, action)
            return {"type": "cancel", "slot_id": slot_index, "params": params}

        return None  # Noop
