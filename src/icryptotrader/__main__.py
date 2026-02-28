"""Entry point — ``python -m icryptotrader [run|backtest|setup]``.

Wires every component together based on config, then runs the async event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from decimal import Decimal
from pathlib import Path

from icryptotrader.config import Config, load_config
from icryptotrader.logging_setup import setup_logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Component construction
# ---------------------------------------------------------------------------


def _build_components(cfg: Config) -> dict:  # type: ignore[type-arg]
    """Construct all components from config.  Returns a dict of named objects."""
    from icryptotrader.fee.fee_model import FeeModel
    from icryptotrader.inventory.inventory_arbiter import AllocationLimits, InventoryArbiter
    from icryptotrader.order.order_manager import OrderManager
    from icryptotrader.order.rate_limiter import RateLimiter
    from icryptotrader.risk.delta_skew import DeltaSkew
    from icryptotrader.risk.risk_manager import RiskManager
    from icryptotrader.strategy.grid_engine import GridEngine
    from icryptotrader.strategy.regime_router import RegimeRouter
    from icryptotrader.strategy.strategy_loop import StrategyLoop
    from icryptotrader.tax.fifo_ledger import FIFOLedger
    from icryptotrader.tax.tax_agent import TaxAgent
    from icryptotrader.types import Regime
    from icryptotrader.ws.ws_private import WSPrivate
    from icryptotrader.ws.ws_public import WSPublicFeed

    # Fee model (Kraken spot defaults)
    fee_model = FeeModel()

    # Rate limiter
    rate_limiter = RateLimiter(
        max_counter=cfg.rate_limit.max_counter,
        decay_rate=cfg.rate_limit.decay_rate,
        headroom_pct=cfg.rate_limit.headroom_pct,
    )

    # Grid engine (geometric spacing prevents negative price crashes)
    grid_engine = GridEngine(
        fee_model=fee_model,
        order_size_usd=cfg.grid.order_size_usd,
        min_spacing_bps=cfg.grid.min_spacing_bps,
        geometric=cfg.grid.geometric_spacing,
    )

    # Order manager (amend threshold preserves queue priority)
    order_manager = OrderManager(
        num_slots=(cfg.grid.levels * 2) or 10,
        rate_limiter=rate_limiter,
        pair=cfg.pair,
        pending_timeout_ms=cfg.ws.pending_ack_timeout_ms,
        amend_threshold_bps=cfg.grid.amend_threshold_bps,
        post_only=cfg.grid.post_only,
    )

    # FIFO ledger
    ledger = FIFOLedger()

    # Tax agent (blow-through mode, vault lock priority, wash sale cooldown)
    tax_agent = TaxAgent(
        ledger=ledger,
        annual_exemption_eur=cfg.tax.annual_exemption_eur,
        near_threshold_days=cfg.tax.near_threshold_days,
        emergency_dd_pct=cfg.tax.emergency_dd_override_pct,
        blow_through_mode=cfg.tax.blow_through_mode,
        vault_lock_priority=cfg.tax.vault_lock_priority,
        wash_sale_cooldown_hours=cfg.tax.harvest_wash_sale_cooldown_hours,
    )

    # Risk manager — pass trailing stop config from cfg
    risk_manager = RiskManager(
        initial_portfolio_usd=cfg.grid.compound_base_usd,
        max_drawdown_pct=cfg.risk.max_portfolio_drawdown_pct,
        emergency_drawdown_pct=cfg.risk.emergency_drawdown_pct,
        price_velocity_freeze_pct=cfg.risk.price_velocity_freeze_pct,
        price_velocity_window_sec=cfg.risk.price_velocity_window_sec,
        price_velocity_cooldown_sec=cfg.risk.price_velocity_cooldown_sec,
        trailing_stop_enabled=cfg.risk.trailing_stop_enabled,
        trailing_stop_tighten_pct=cfg.risk.trailing_stop_tighten_pct,
    )

    # Delta skew
    delta_skew = DeltaSkew()

    # Inventory arbiter — build limits from regime config
    regime_limits = {
        Regime.RANGE_BOUND: AllocationLimits(
            target_pct=cfg.regime.range_bound.btc_target_pct,
            max_pct=cfg.regime.range_bound.btc_max_pct,
            min_pct=cfg.regime.range_bound.btc_min_pct,
        ),
        Regime.TRENDING_UP: AllocationLimits(
            target_pct=cfg.regime.trending_up.btc_target_pct,
            max_pct=cfg.regime.trending_up.btc_max_pct,
            min_pct=cfg.regime.trending_up.btc_min_pct,
        ),
        Regime.TRENDING_DOWN: AllocationLimits(
            target_pct=cfg.regime.trending_down.btc_target_pct,
            max_pct=cfg.regime.trending_down.btc_max_pct,
            min_pct=cfg.regime.trending_down.btc_min_pct,
        ),
        Regime.CHAOS: AllocationLimits(
            target_pct=cfg.regime.chaos.btc_target_pct,
            max_pct=cfg.regime.chaos.btc_max_pct,
            min_pct=cfg.regime.chaos.btc_min_pct,
        ),
    }
    inventory = InventoryArbiter(
        limits=regime_limits,
        max_rebalance_pct_per_min=cfg.risk.max_rebalance_pct_per_min,
    )

    # Regime router
    regime_router = RegimeRouter()

    # Bollinger spacing (optional)
    bollinger = None
    if cfg.bollinger.enabled:
        from icryptotrader.strategy.bollinger import BollingerSpacing

        bollinger = BollingerSpacing(
            window=cfg.bollinger.window,
            multiplier=Decimal(str(cfg.bollinger.multiplier)),
            spacing_scale=Decimal(str(cfg.bollinger.spacing_scale)),
            min_spacing_bps=cfg.bollinger.min_spacing_bps,
            max_spacing_bps=cfg.bollinger.max_spacing_bps,
            atr_enabled=cfg.bollinger.atr_enabled,
            atr_window=cfg.bollinger.atr_window,
            atr_weight=cfg.bollinger.atr_weight,
        )

    # Avellaneda-Stoikov optimal spacing (optional — replaces Bollinger + DeltaSkew)
    as_model = None
    if cfg.avellaneda_stoikov.enabled:
        from icryptotrader.strategy.avellaneda_stoikov import AvellanedaStoikov

        as_model = AvellanedaStoikov(
            gamma=Decimal(str(cfg.avellaneda_stoikov.gamma)),
            max_spread_bps=cfg.avellaneda_stoikov.max_spread_bps,
            max_skew_bps=cfg.avellaneda_stoikov.max_skew_bps,
            obi_sensitivity_bps=cfg.avellaneda_stoikov.obi_sensitivity_bps,
        )

    # AI signal engine (optional)
    ai_signal = None
    if cfg.ai_signal.enabled:
        from icryptotrader.strategy.ai_signal import AISignalEngine

        ai_signal = AISignalEngine(
            provider=cfg.ai_signal.provider,
            api_key=cfg.ai_signal.api_key,
            model=cfg.ai_signal.model,
            temperature=cfg.ai_signal.temperature,
            max_tokens=cfg.ai_signal.max_tokens,
            cooldown_sec=cfg.ai_signal.cooldown_sec,
            weight=cfg.ai_signal.weight,
            timeout_sec=cfg.ai_signal.timeout_sec,
        )

    # Metrics (optional)
    metrics_registry = None
    metrics_server = None
    if cfg.metrics.enabled:
        from icryptotrader.metrics import MetricsRegistry, MetricsServer

        metrics_registry = MetricsRegistry(prefix=cfg.metrics.prefix)
        metrics_server = MetricsServer(registry=metrics_registry, port=cfg.metrics.port)

    # Hedge manager (optional)
    hedge_manager = None
    if cfg.hedge.enabled:
        from icryptotrader.risk.hedge_manager import HedgeManager

        hedge_manager = HedgeManager(
            trigger_drawdown_pct=cfg.hedge.trigger_drawdown_pct,
            strategy=cfg.hedge.strategy,
            max_reduction_pct=cfg.hedge.max_reduction_pct,
        )

    # Pair manager (optional — multi-pair diversification)
    pair_manager = None
    if cfg.pairs:
        from icryptotrader.pair_manager import PairManager

        pair_manager = PairManager(total_capital_usd=cfg.grid.compound_base_usd)
        for pair_alloc in cfg.pairs:
            pair_manager.add_pair(pair_alloc.symbol, weight=pair_alloc.weight)
        pair_manager.allocate()
        logger.info(
            "PairManager: %d pairs configured", pair_manager.pair_count,
        )

    # Web dashboard (optional)
    web_dashboard = None
    if cfg.web.enabled:
        from icryptotrader.web.dashboard import WebDashboard

        web_dashboard = WebDashboard(
            risk_manager=risk_manager,
            metrics_registry=metrics_registry,
            host=cfg.web.host,
            port=cfg.web.port,
            username=cfg.web.username,
            password=cfg.web.password,
        )

    # Persistence path
    ledger_path = Path(cfg.ledger_path)
    persistence_backend = cfg.persistence_backend

    # Order book for OBI feed (used by strategy loop for skew and AI context)
    from icryptotrader.ws.book_manager import OrderBook

    order_book = OrderBook(symbol=cfg.pair)

    # Strategy loop
    strategy_loop = StrategyLoop(
        fee_model=fee_model,
        order_manager=order_manager,
        grid_engine=grid_engine,
        tax_agent=tax_agent,
        risk_manager=risk_manager,
        delta_skew=delta_skew,
        inventory=inventory,
        regime_router=regime_router,
        ledger=ledger,
        ledger_path=ledger_path,
        auto_compound=cfg.grid.auto_compound,
        compound_base_usd=cfg.grid.compound_base_usd,
        base_order_size_usd=cfg.grid.order_size_usd,
        metrics=metrics_registry,
        bollinger=bollinger,
        ai_signal=ai_signal,
        persistence_backend=persistence_backend,
        book=order_book,
        avellaneda_stoikov=as_model,
    )

    # P0-1: Wire fill callback so FIFO ledger is updated on every fill
    order_manager.on_fill(strategy_loop.on_fill)

    # Telegram bot (optional)
    telegram_bot = None
    if cfg.telegram.enabled and cfg.telegram.bot_token:
        from icryptotrader.notify.telegram import TelegramBot

        telegram_bot = TelegramBot(
            bot_token=cfg.telegram.bot_token,
            chat_id=cfg.telegram.chat_id,
            enabled=True,
        )
        telegram_bot.set_data_provider(strategy_loop)

    # WebSocket connections
    ws_private = WSPrivate(
        rest_url=cfg.kraken.rest_url,
        ws_url=cfg.kraken.ws_private_url,
        api_key=cfg.kraken.api_key,
        api_secret=cfg.kraken.api_secret,
        cancel_after_sec=cfg.ws.cancel_after_timeout_sec,
        heartbeat_interval_sec=cfg.ws.heartbeat_interval_sec,
    )

    ws_public = WSPublicFeed(url=cfg.kraken.ws_public_url)
    ws_public.subscribe("ticker", symbol=[cfg.pair])
    ws_public.subscribe("trade", symbol=[cfg.pair])

    return {
        "cfg": cfg,
        "strategy_loop": strategy_loop,
        "order_manager": order_manager,
        "ws_private": ws_private,
        "ws_public": ws_public,
        "metrics_registry": metrics_registry,
        "metrics_server": metrics_server,
        "telegram_bot": telegram_bot,
        "ai_signal": ai_signal,
        "bollinger": bollinger,
        "inventory": inventory,
        "risk_manager": risk_manager,
        "ledger": ledger,
        "hedge_manager": hedge_manager,
        "web_dashboard": web_dashboard,
        "pair_manager": pair_manager,
        "order_book": order_book,
    }


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------


async def _run_bot(cfg: Config) -> None:
    """Construct components and run the main trading loop."""
    from icryptotrader.lifecycle import LifecycleManager
    from icryptotrader.watchdog import Watchdog

    c = _build_components(cfg)

    strategy_loop = c["strategy_loop"]
    ws_private = c["ws_private"]
    ws_public = c["ws_public"]
    order_manager = c["order_manager"]
    metrics_server = c["metrics_server"]
    telegram_bot = c["telegram_bot"]
    ai_signal = c["ai_signal"]
    hedge_manager = c["hedge_manager"]
    web_dashboard = c["web_dashboard"]
    pair_manager = c["pair_manager"]

    # Lifecycle manager
    lm = LifecycleManager(
        strategy_loop=strategy_loop,
        ws_private=ws_private,
        ws_public=ws_public,
        order_manager=order_manager,
    )

    loop = asyncio.get_event_loop()
    lm.install_signal_handlers(loop)

    # Start optional services
    if metrics_server:
        await metrics_server.start()
        logger.info("Metrics server started on port %d", cfg.metrics.port)

    if telegram_bot:
        await telegram_bot.start()
        logger.info("Telegram bot started")

    if web_dashboard:
        web_dashboard.set_loop(strategy_loop)
        await web_dashboard.start()
        logger.info("Web dashboard started on %s:%d", cfg.web.host, cfg.web.port)

    # P1-4: Watchdog — pass lifecycle manager for graceful shutdown
    watchdog = Watchdog(
        strategy_loop=strategy_loop,
        ws_private=ws_private,
        lifecycle_manager=lm,
    )
    watchdog_task = asyncio.create_task(watchdog.run())

    # Start WS connections
    ws_public_task = asyncio.create_task(ws_public.run())
    ws_private_task = asyncio.create_task(ws_private.run())

    # Run startup sequence
    await lm.startup()

    # Event-driven tick: wake on WS events instead of fixed 100ms polling.
    # The tick_event is set by WS callbacks (book update, trade, fill, balance)
    # and the loop awaits it with a 1-second max timeout fallback.
    tick_event = asyncio.Event()
    order_book = c["order_book"]

    # P2: Move import to module scope for the closure
    from icryptotrader.ws.ws_codec import MessageType, WSMessage

    def _on_book_msg(msg: object) -> None:
        """Route book channel data to OrderBook and signal tick."""
        assert isinstance(msg, WSMessage)
        if not msg.data:
            return
        book_data = msg.data[0] if isinstance(msg.data, list) else msg.data
        if msg.data_type == "snapshot":
            order_book.apply_snapshot(book_data)
        else:
            order_book.apply_update(book_data)
        tick_event.set()

    def _on_trade_msg(_msg: object) -> None:
        """Signal tick on public trade."""
        tick_event.set()

    def _on_execution_msg(msg: object) -> None:
        """Route execution events to OrderManager and signal tick."""
        assert isinstance(msg, WSMessage)
        for exec_data in msg.data:
            order_manager.on_execution_event(exec_data)
        tick_event.set()

    def _on_ack_msg(msg: object) -> None:
        """Route command ack responses to OrderManager."""
        assert isinstance(msg, WSMessage)
        req_id = msg.req_id or 0
        order_id = msg.result.get("order_id", "")
        success = msg.success or False
        error = msg.error or ""

        if msg.msg_type == MessageType.ADD_ORDER_RESP:
            order_manager.on_add_order_ack(req_id, order_id, success, error)
        elif msg.msg_type == MessageType.AMEND_ORDER_RESP:
            order_manager.on_amend_order_ack(order_id, success, error)
        elif msg.msg_type == MessageType.CANCEL_ORDER_RESP:
            order_manager.on_cancel_ack(order_id, success, error)

    # Register WS callbacks that drive the event loop
    ws_public.on_channel("book", _on_book_msg)
    ws_public.on_channel("trade", _on_trade_msg)
    # P0-2: Wire execution and ack events from ws_private to OrderManager
    ws_private.on_execution(_on_execution_msg)
    ws_private.on_ack(_on_ack_msg)

    # Subscribe to L2 book channel for OBI and validated mid-price
    ws_public.subscribe("book", symbol=[cfg.pair], depth=10)

    # P1-1: ECB rate service — periodically fetch EUR/USD rate
    ecb_task = asyncio.create_task(
        _ecb_rate_loop(strategy_loop),
    )

    # AI signal background loop
    ai_task = None
    if ai_signal:
        ai_task = asyncio.create_task(
            _ai_signal_loop(ai_signal, strategy_loop),
        )

    # P1-2: Wire BotActionProvider for Telegram interactive actions
    if telegram_bot:
        action_provider = _BotActionProviderImpl(
            risk_manager=c["risk_manager"],
            tax_agent=strategy_loop._tax,
            strategy_loop=strategy_loop,
        )
        telegram_bot.set_action_provider(action_provider)

    logger.info("Bot running (event-driven). Press Ctrl+C to stop.")

    # Main tick loop — event-driven with timeout fallback
    try:
        while not lm.is_shutting_down:
            # Wait for a market event or 1-second timeout (periodic fallback)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(tick_event.wait(), timeout=1.0)
            tick_event.clear()

            try:
                price = c["inventory"].btc_price
                if price > 0:
                    # P0-3: Evaluate hedge before tick and pass action to strategy loop
                    if hedge_manager:
                        risk_mgr = c["risk_manager"]
                        regime_decision = strategy_loop._regime.classify()
                        inv_snap = c["inventory"].snapshot()
                        hedge_action = hedge_manager.evaluate(
                            drawdown_pct=risk_mgr.drawdown_pct,
                            regime=regime_decision.regime,
                            pause_state=risk_mgr.pause_state,
                            btc_allocation_pct=inv_snap.btc_allocation_pct,
                            target_allocation_pct=0.5,
                        )
                        strategy_loop.set_hedge_action(hedge_action)
                    else:
                        strategy_loop.set_hedge_action(None)

                    commands = strategy_loop.tick(mid_price=price)

                    # Update pair manager with current state
                    if pair_manager:
                        inv_snap = c["inventory"].snapshot()
                        pair_manager.update_pair(
                            symbol=cfg.pair,
                            current_value_usd=inv_snap.portfolio_value_usd,
                            drawdown_pct=c["risk_manager"].drawdown_pct,
                            price=price,
                        )

                    for cmd in commands:
                        params = cmd.get("params", {})
                        cmd_type = cmd.get("type")
                        if cmd_type == "add":
                            await ws_private.send_add_order(**params)
                        elif cmd_type == "amend":
                            await ws_private.send_amend_order(**params)
                        elif cmd_type == "cancel":
                            await ws_private.send_cancel_order(**params)
            except Exception:
                logger.exception("Tick error")
    except asyncio.CancelledError:
        pass

    # Shutdown
    logger.info("Shutting down...")
    watchdog.stop()

    ecb_task.cancel()
    if ai_task:
        ai_task.cancel()
    if web_dashboard:
        await web_dashboard.stop()
    if telegram_bot:
        await telegram_bot.stop()
    if metrics_server:
        await metrics_server.stop()

    await lm.shutdown()

    watchdog_task.cancel()
    ws_public_task.cancel()
    ws_private_task.cancel()

    for task in (watchdog_task, ws_public_task, ws_private_task):
        with contextlib.suppress(asyncio.CancelledError):
            await task

    logger.info("Shutdown complete.")


async def _ai_signal_loop(
    ai_signal: object,
    strategy_loop: object,
) -> None:
    """Background loop that periodically refreshes AI signals."""
    from icryptotrader.strategy.ai_signal import AISignalEngine
    from icryptotrader.strategy.strategy_loop import StrategyLoop
    from icryptotrader.types import Regime

    assert isinstance(ai_signal, AISignalEngine)
    assert isinstance(strategy_loop, StrategyLoop)

    hint_map = {
        "range_bound": Regime.RANGE_BOUND,
        "trending_up": Regime.TRENDING_UP,
        "trending_down": Regime.TRENDING_DOWN,
        "chaos": Regime.CHAOS,
    }

    while True:
        try:
            if ai_signal.is_ready:
                ctx = strategy_loop.build_ai_context()
                signal = await ai_signal.generate_signal(ctx)
                # P1-6: Consume AI regime_hint
                if signal and signal.regime_hint:
                    hint_regime = hint_map.get(signal.regime_hint.lower())
                    if hint_regime is not None and signal.confidence >= 0.5:
                        strategy_loop._regime.override_regime(
                            hint_regime, f"ai_signal({signal.confidence:.0%})",
                        )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("AI signal loop error")
        await asyncio.sleep(10)


async def _ecb_rate_loop(strategy_loop: object) -> None:
    """Background loop that periodically fetches ECB EUR/USD rate.

    Runs every 4 hours. Uses synchronous httpx in an executor to avoid
    blocking the event loop.
    """
    import functools
    from datetime import date as date_type

    from icryptotrader.strategy.strategy_loop import StrategyLoop
    from icryptotrader.tax.ecb_rates import ECBRateService

    assert isinstance(strategy_loop, StrategyLoop)

    # Initial delay to let WS connections settle
    await asyncio.sleep(5)

    service = ECBRateService()
    loop = asyncio.get_event_loop()

    while True:
        try:
            today = date_type.today()
            rate = await loop.run_in_executor(
                None, functools.partial(service.get_rate, today),
            )
            strategy_loop.set_eur_usd_rate(rate)
            logger.info("ECB EUR/USD rate updated: %s", rate)
        except asyncio.CancelledError:
            service.close()
            break
        except Exception:
            logger.warning("ECB rate fetch failed, keeping previous rate", exc_info=True)
        await asyncio.sleep(4 * 3600)  # Refresh every 4 hours


class _BotActionProviderImpl:
    """Concrete BotActionProvider wiring Telegram actions to bot components."""

    def __init__(
        self,
        risk_manager: object,
        tax_agent: object,
        strategy_loop: object,
    ) -> None:
        from icryptotrader.risk.risk_manager import RiskManager
        from icryptotrader.strategy.strategy_loop import StrategyLoop
        from icryptotrader.tax.tax_agent import TaxAgent

        assert isinstance(risk_manager, RiskManager)
        assert isinstance(tax_agent, TaxAgent)
        assert isinstance(strategy_loop, StrategyLoop)

        self._risk = risk_manager
        self._tax = tax_agent
        self._strategy = strategy_loop

    async def action_pause_trading(self) -> str:
        self._risk.force_risk_pause()
        return "Trading paused (RISK_PAUSE_ACTIVE)"

    async def action_resume_trading(self) -> str:
        self._risk.force_active()
        return "Trading resumed (ACTIVE_TRADING)"

    async def action_trigger_harvest(self) -> str:
        recs = self._tax.recommend_loss_harvest(
            current_price_usd=self._strategy._inv.btc_price,
            eur_usd_rate=self._strategy._eur_usd_rate,
        )
        if not recs:
            return "No harvest candidates found"
        return f"Found {len(recs)} harvest candidates (manual review required)"

    async def action_reset_risk(self) -> str:
        self._risk.force_active()
        return "Risk state reset to ACTIVE_TRADING"

    async def action_toggle_blow_through(self) -> str:
        current = self._tax._blow_through_mode
        self._tax._blow_through_mode = not current
        state = "ON" if not current else "OFF"
        return f"Blow-through mode: {state}"


# ---------------------------------------------------------------------------
# Backtest CLI
# ---------------------------------------------------------------------------


def _run_backtest(args: argparse.Namespace) -> None:
    """Run a backtest from CLI arguments."""
    import csv

    from icryptotrader.backtest.engine import BacktestConfig, BacktestEngine

    # Load prices
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"Error: data file not found: {data_path}", file=sys.stderr)
        sys.exit(1)

    prices: list[Decimal] = []
    with open(data_path) as f:
        reader = csv.reader(f)
        header = next(reader, None)
        # Auto-detect price column
        price_col = 0
        if header:
            for i, col in enumerate(header):
                if col.lower() in ("close", "price", "mid_price", "mid"):
                    price_col = i
                    break

        for row in reader:
            if row and len(row) > price_col:
                try:
                    prices.append(Decimal(row[price_col].strip()))
                except Exception:
                    continue

    if len(prices) < 2:
        print("Error: need at least 2 price points", file=sys.stderr)
        sys.exit(1)

    # Load config for backtest params
    config_path = Path(args.config) if args.config else None
    cfg = load_config(config_path)

    bt_config = BacktestConfig(
        initial_usd=(
            Decimal(str(args.initial_usd)) if args.initial_usd
            else cfg.grid.compound_base_usd
        ),
        order_size_usd=cfg.grid.order_size_usd,
        grid_levels=cfg.grid.levels,
        spacing_bps=cfg.grid.min_spacing_bps,
        maker_fee_bps=Decimal("16"),
        auto_compound=cfg.grid.auto_compound,
    )

    engine = BacktestEngine(config=bt_config)
    result = engine.run(prices)

    print(result.summary())

    if args.output:
        import json

        out = {
            "ticks": result.ticks,
            "trades": len(result.trades),
            "buys": result.buy_count,
            "sells": result.sell_count,
            "initial_usd": str(result.initial_portfolio_usd),
            "final_usd": str(result.final_portfolio_usd),
            "return_pct": result.return_pct,
            "pnl_usd": str(result.total_pnl_usd),
            "fees_usd": str(result.total_fees_usd),
            "max_drawdown_pct": result.max_drawdown_pct,
        }
        Path(args.output).write_text(json.dumps(out, indent=2))
        print(f"\nResults saved to {args.output}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="icryptotrader",
        description="Tax-optimized spot grid trading bot for Kraken BTC/USD",
    )
    sub = parser.add_subparsers(dest="command")

    # run (default)
    run_parser = sub.add_parser("run", help="Start the trading bot")
    run_parser.add_argument("--config", "-c", type=str, help="Config file path")
    run_parser.add_argument("--json-log", action="store_true", help="JSON log output")

    # backtest
    bt_parser = sub.add_parser("backtest", help="Run a backtest")
    bt_parser.add_argument("--data", "-d", required=True, help="CSV price data file")
    bt_parser.add_argument("--config", "-c", type=str, help="Config file path")
    bt_parser.add_argument("--output", "-o", type=str, help="Output JSON file")
    bt_parser.add_argument("--initial-usd", type=float, help="Starting USD balance")

    # setup
    sub.add_parser("setup", help="Run interactive setup wizard")

    args = parser.parse_args()

    # Default to "run" when no subcommand given
    command = args.command or "run"

    if command == "setup":
        from icryptotrader.setup_wizard import run_wizard

        run_wizard()
        return

    if command == "backtest":
        config_path = Path(args.config) if args.config else None
        cfg = load_config(config_path)
        setup_logging(level=cfg.log_level)
        _run_backtest(args)
        return

    # command == "run"
    config_path = Path(args.config) if hasattr(args, "config") and args.config else None
    json_log = getattr(args, "json_log", False)
    cfg = load_config(config_path)
    setup_logging(level=cfg.log_level, json_output=json_log)

    logger.info("iCryptoTrader starting (pair=%s)", cfg.pair)
    asyncio.run(_run_bot(cfg))


if __name__ == "__main__":
    main()
