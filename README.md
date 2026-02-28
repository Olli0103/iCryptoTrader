# iCryptoTrader

Tax-optimized spot grid trading bot for **Kraken BTC/USD**, built for German tax law compliance (§23 EStG).

Runs a mean-reversion grid strategy on Kraken's WebSocket v2 API, with every buy and sell decision gated by a FIFO tax ledger, a risk manager, and an inventory arbiter. Designed to hold BTC lots for 365+ days whenever possible, selling only tax-free positions unless an emergency drawdown override is triggered.

## Key Features

- **Grid Trading Engine** — Symmetric buy/sell grid with fee-aware spacing auto-calibration
- **Bollinger Band Spacing** — Volatility-adaptive grid density using rolling Bollinger Bands
- **FIFO Tax Ledger** — Per-lot tracking with cost basis in USD and EUR, §23 EStG Haltefrist enforcement
- **Tax Agent Veto** — Blocks taxable sells, allows tax-free lots, respects the annual Freigrenze (EUR 1,000)
- **Tax-Loss Harvesting** — Proactive selling of underwater lots to offset gains and optimize Freigrenze
- **Delta Skew** — Asymmetric grid spacing based on inventory deviation from target allocation
- **Risk Manager** — Drawdown classification (Healthy/Warning/Problem/Critical/Emergency), pause state machine, price velocity circuit breaker with hysteresis
- **Regime Router** — EWMA volatility + momentum-based regime classification (Range-bound, Trending Up/Down, Chaos)
- **Inventory Arbiter** — Per-regime BTC allocation limits with single-tick rebalance caps
- **Amend-First Order Manager** — Prefers atomic `amend_order` over cancel+new to preserve queue priority
- **L2 Order Book Manager** — CRC32 checksum validation against Kraken WS v2 spec to detect stale data
- **Dead Man's Switch** — `cancel_after` heartbeat automatically cancels all orders if the bot disconnects
- **Telegram Notifications** — Fill alerts, risk state changes, tax unlock countdowns, daily P&L summaries
- **ECB Rate Service** — Daily EUR/USD reference rates from the ECB for Finanzamt-accepted tax calculations
- **Tax Report Generator** — Anlage SO export in CSV and JSON with per-disposal fields
- **Auto-Save Ledger** — FIFO ledger persisted to disk after every fill for crash safety
- **Graceful Shutdown** — SIGTERM/SIGINT handler: cancel all orders, disarm DMS, save ledger, close connections
- **Startup Reconciliation** — Load ledger, reconnect, reconcile order slots against exchange snapshots, cancel orphans
- **Dynamic Grid Sizing** — Per-regime `order_size_scale` adjusts order notional (1.0x range-bound, 0.75x trending, 0.5x chaos)
- **Lot Age Viewer** — CLI visualization: per-lot table, ASCII age histogram, tax-free unlock schedule, portfolio summary

## Architecture

```
                  +-----------+
                  |  Kraken   |
                  | Exchange  |
                  +-----+-----+
                        |
             +----------+----------+
             |                     |
        WS1 (Public)          WS2 (Private)
        book, trade,          executions, balances,
        ticker, ohlc          add/amend/cancel_order
             |                     |
             v                     v
    +--------+--------+   +-------+--------+
    |  Feed Process   |   | Strategy Process|
    |  (ws_public.py) |   | (strategy_loop) |
    +--------+--------+   +-------+--------+
             |  ZMQ PUB            |
             +----------+----------+
                        |
               +--------v---------+
               |   Strategy Loop  |
               |   (orchestrator) |
               +--------+---------+
                        |
         +--------------+--------------+
         |        |        |           |
    +----v---+ +--v---+ +--v------+ +--v---------+
    | Regime | | Grid | | Delta   | | Inventory  |
    | Router | | Eng. | | Skew    | | Arbiter    |
    +--------+ +------+ +---------+ +------------+
         |        |        |           |
         +--------+--------+-----------+
                        |
               +--------v---------+
               |  Order Manager   |
               |  (amend-first    |
               |   state machine) |
               +--------+---------+
                        |
         +--------------+--------------+
         |              |              |
    +----v---+    +-----v----+   +-----v------+
    | Tax    |    | Risk     |   | Rate       |
    | Agent  |    | Manager  |   | Limiter    |
    +----+---+    +----------+   +------------+
         |
    +----v--------+
    | FIFO Ledger |---> Tax Report (CSV/JSON)
    +----+--------+     Anlage SO format
         |
    +----v--------+
    | ECB Rates   |
    +-------------+
```

### Tick Cycle

Each strategy tick (~100ms) runs the following pipeline:

1. **Market data** — Update inventory price, regime router
2. **Circuit breaker** — Check price velocity; freeze if >3% move in 60s
3. **Risk update** — Compute portfolio drawdown, update pause state
4. **Regime classification** — EWMA vol + momentum -> Regime enum
5. **Grid computation** — N buy/sell levels with fee-aware spacing
6. **Tax gating** — Tax Agent recommends sell-level count based on sellable ratio
7. **Delta skew** — Skew buy/sell spacing based on allocation deviation
8. **Order decisions** — Per-slot decide_action(): Add / Amend / Cancel / Noop
9. **Rate limiting** — Gate add/amend commands against Kraken's rate counter
10. **Dispatch** — Commands sent to WS2; fills update FIFO ledger

### Pause State Machine

```
ACTIVE_TRADING ──tax lock──> TAX_LOCK_ACTIVE (buy-only)
       |                            |
   DD >= 15%                    DD >= 15%
       |                            |
       v                            v
RISK_PAUSE_ACTIVE            DUAL_LOCK (full stop)
                                    |
                                DD >= 20%
                                    |
                                    v
                             EMERGENCY_SELL
                          (tax override, force sell)
```

### Order Slot State Machine

```
EMPTY ──add_order──> PENDING_NEW ──ack──> LIVE
                                           |
                          amend_order ─────+──> AMEND_PENDING ──ack──> LIVE
                          cancel_order ────+──> CANCEL_PENDING ──ack──> EMPTY
                          full fill ───────+──> EMPTY
                          timeout ─────────+──> CANCEL_PENDING
```

### German Tax Logic (§23 EStG)

- **Haltefrist**: BTC held >365 days is tax-free (steuerfrei)
- **FIFO**: Oldest lots sold first (per BMF circular 10.05.2022)
- **Freigrenze**: Annual gains under EUR 1,000 are fully exempt
- **Near-threshold protection**: Lots 330-365 days old are protected from sale
- **Emergency override**: Portfolio drawdown >20% overrides all tax locks
- **ECB reference rate**: All EUR conversions use the official ECB daily rate
- **Tax-loss harvesting**: Proactive selling of underwater lots to offset YTD gains, targeting net below Freigrenze

## Project Structure

```
src/icryptotrader/
  __init__.py              # Package root, version
  types.py                 # Shared enums, dataclasses (HarvestRecommendation, FeeTier, ...)
  config.py                # TOML config loader with typed dataclasses
  logging_setup.py         # Structured JSON / dev logging
  lifecycle.py             # Graceful shutdown, startup reconciliation, reconnect recovery

  strategy/
    strategy_loop.py       # Main tick orchestrator with ledger auto-save
    grid_engine.py         # Grid level computation
    regime_router.py       # EWMA vol / momentum regime classifier
    bollinger.py           # Bollinger Band volatility-adaptive grid spacing

  order/
    order_manager.py       # Amend-first slot state machine
    rate_limiter.py        # Kraken per-pair rate counter tracker

  risk/
    risk_manager.py        # Drawdown tracking, pause states, circuit breaker (with hysteresis)
    delta_skew.py          # Allocation deviation -> quote asymmetry

  inventory/
    inventory_arbiter.py   # BTC/USD allocation enforcement per regime

  tax/
    fifo_ledger.py         # FIFO lot tracking, cost basis, persistence, underwater_lots()
    tax_agent.py           # Sell veto, Freigrenze, near-threshold, tax-loss harvesting
    tax_report.py          # Anlage SO report generator (CSV, JSON, text)
    ecb_rates.py           # ECB EUR/USD reference rate fetcher
    lot_viewer.py          # Lot age visualization (table, histogram, unlock schedule)

  fee/
    fee_model.py           # Kraken fee tier schedule and profitability gate

  notify/
    telegram.py            # Telegram notifications (fills, risk, tax, daily summary)

  ws/
    ws_codec.py            # Kraken WS v2 message encode/decode (orjson)
    ws_public.py           # WS1: public market data feed
    ws_private.py          # WS2: authenticated trading + executions + balances
    book_manager.py        # L2 order book with CRC32 checksum validation

config/
  default.toml             # Default configuration

tests/                     # 385 tests across 24 test files
```

## Configuration

All settings are defined in `config/default.toml`. Copy and customize:

```toml
pair = "XBT/USD"
log_level = "INFO"

[kraken]
api_key = ""        # Your Kraken API key
api_secret = ""     # Your Kraken API secret

[grid]
levels = 5                # Grid levels per side
order_size_usd = "500"    # Notional per level
min_spacing_bps = "20"    # Minimum spacing (basis points)
post_only = true           # Maker-only orders

[risk]
max_portfolio_drawdown_pct = 0.15    # 15% -> RISK_PAUSE
emergency_drawdown_pct = 0.20       # 20% -> EMERGENCY_SELL
price_velocity_freeze_pct = 0.03    # 3% in 60s -> circuit breaker
price_velocity_cooldown_sec = 30

[tax]
holding_period_days = 365
near_threshold_days = 330
annual_exemption_eur = "1000"
emergency_dd_override_pct = 0.20
harvest_enabled = false           # Tax-loss harvesting (opt-in)
harvest_min_loss_eur = "50"       # Minimum loss to bother harvesting
harvest_max_per_day = 3           # Max harvest sells per day
harvest_target_net_eur = "800"    # Target net below Freigrenze

[regime.range_bound]
btc_target_pct = 0.50
btc_max_pct = 0.60
btc_min_pct = 0.40
grid_levels = 5
order_size_scale = 1.0     # Full size in range-bound

[regime.trending_up]
order_size_scale = 0.75    # Reduced in trending

[regime.chaos]
btc_target_pct = 0.00
btc_max_pct = 0.05
grid_levels = 0
signal_enabled = false
order_size_scale = 0.5     # Half size in chaos

[ws]
cancel_after_timeout_sec = 60    # Dead man's switch timeout
heartbeat_interval_sec = 20     # DMS re-arm interval

[rate_limit]
max_counter = 180       # Kraken Pro tier
decay_rate = 3.75       # Counter decay per second
headroom_pct = 0.80     # Throttle at 80% of max

[bollinger]
enabled = true
window = 20                # Rolling price window (ticks)
multiplier = 2.0           # Band multiplier (k * std_dev)
spacing_scale = 0.5        # band_width_bps * scale = spacing
min_spacing_bps = "15"     # Hard floor
max_spacing_bps = "200"    # Hard cap

[telegram]
enabled = false
bot_token = ""
chat_id = ""
```

## Installation

```bash
# Clone and install
git clone <repo-url> && cd iCryptoTrader
pip install -e ".[dev]"

# Run tests
pytest

# Type checking
mypy src/

# Linting
ruff check src/ tests/
```

**Requirements**: Python 3.11+, websockets, orjson, pyzmq, httpx

## Fee Model

Grid spacing is auto-calibrated to be profitable at your current Kraken fee tier:

| 30-Day Volume (USD) | Maker (bps) | Taker (bps) | Min Profitable Spacing |
|---------------------|-------------|-------------|----------------------|
| $0 - $10K           | 25          | 40          | 65 bps               |
| $10K - $50K         | 20          | 35          | 55 bps               |
| $50K - $100K        | 14          | 24          | 43 bps               |
| $100K - $250K       | 12          | 20          | 39 bps               |
| $1M - $5M           | 4           | 14          | 23 bps               |
| $10M+               | 0           | 10          | 15 bps               |

*Min profitable spacing = 2x maker fee + 10 bps adverse selection + 5 bps min edge*

## Testing

```bash
# Full suite (385 tests)
pytest -v

# With coverage
pytest --cov=icryptotrader --cov-report=term-missing

# Specific module
pytest tests/test_fifo_ledger.py -v     # FIFO ledger + underwater lots
pytest tests/test_tax_agent.py -v       # Tax veto + harvest recommendations
pytest tests/test_bollinger.py -v       # Bollinger Band spacing
pytest tests/test_book_manager.py -v    # L2 book + CRC32 checksums
pytest tests/test_order_manager.py -v
pytest tests/test_strategy_loop.py -v
pytest tests/test_lifecycle.py -v      # Graceful shutdown + reconciliation
pytest tests/test_lot_viewer.py -v     # Lot age visualization
```

Test coverage spans all critical paths: FIFO lot accounting, underwater lot identification, tax-loss harvest recommendations, order state transitions, risk pause states, circuit breaker hysteresis, regime classification, fee tier resolution, rate limiting, WS codec, grid computation, delta skew, inventory allocation, tax agent veto logic, ECB rates, tax reporting, Bollinger Band spacing, L2 book checksums, Telegram notifications, graceful shutdown/reconciliation, and lot age visualization.

## Roadmap

### Phase 2 — Production Hardening (Implemented)

- [x] **Ledger persistence on fill**: Auto-save FIFO ledger to disk after every fill
- [x] **httpx.AsyncClient reuse**: Shared `httpx.AsyncClient` across WS token requests
- [x] **Balances channel subscription**: WS2 `balances` channel for real-time BTC/USD balance updates
- [x] **Telegram notifications**: Fill alerts, risk state changes, daily P&L summaries, tax unlock countdowns
- [x] **Graceful shutdown**: SIGTERM/SIGINT handler that cancels all orders, disarms DMS, saves ledger, and exits cleanly
- [x] **Startup reconciliation flow**: On boot, load ledger from disk, connect WS2, reconcile via executions snapshot, cancel orphans, then begin trading
- [ ] **Process isolation**: Split into Feed Process (WS1 + ZMQ PUB) and Strategy Process (WS2 + strategy loop) for crash isolation

### Phase 3 — Observability & Resilience (Partially Implemented)

- [x] **Order book checksum validation**: L2 book manager with CRC32 checksum validation per Kraken WS v2 spec
- [x] **Circuit breaker hysteresis**: Cooldown period with 50% recovery threshold before re-entering trading after velocity freeze
- [x] **Reconnect state recovery**: LifecycleManager reconciles order slots against exchange snapshots after reconnect, cancels orphan orders
- [ ] **Structured metrics export**: Prometheus/OpenTelemetry metrics for tick latency, fill rate, drawdown, rate limiter utilization, regime distribution
- [ ] **Dashboard**: Grafana or web UI showing grid state, lot ages, portfolio allocation, tax countdown timers
- [ ] **Async callbacks in WS dispatch**: Move execution/ack callbacks to an asyncio queue to avoid blocking the WS receive loop

### Phase 4 — Strategy Enhancements (Partially Implemented)

- [x] **Bollinger Band volatility spacing**: Automatic `spacing_bps` adjustment based on rolling Bollinger Band width with configurable scale, floor, and cap
- [x] **Dynamic grid sizing**: Per-regime `order_size_scale` adjusts order notional (1.0x range-bound, 0.75x trending, 0.5x chaos), wired through RegimeRouter → StrategyLoop → GridEngine
- [ ] **Signal Engine**: Second alpha source alongside the grid (e.g., momentum, mean-reversion signals on longer timeframes)
- [ ] **Volume-weighted mid-price**: Use VWAP from recent trades instead of simple mid for grid centering
- [ ] **Adaptive regime thresholds**: Self-tuning EWMA/momentum thresholds based on rolling realized vol distributions
- [ ] **Multi-pair support**: Extend `Pair`, `FeeModel`, and slot allocation to trade multiple BTC pairs (e.g., XBT/EUR)

### Phase 5 — Tax Optimization (Implemented)

- [x] **Tax-loss harvesting**: `underwater_lots()` + `recommend_loss_harvest()` with Freigrenze targeting, near-threshold protection, and configurable rate limits
- [x] **Lot age visualization**: CLI view with per-lot table, ASCII age histogram, projected tax-free unlock schedule, and portfolio summary
- [ ] **Annual report automation**: Auto-generate Anlage SO at year-end, email to configured address or push to tax advisor portal
- [ ] **Multi-year carry-forward**: Track loss carry-forward across tax years for accurate Freigrenze calculations

### Backlog (Low Priority)

- [ ] Move `DesiredLevel` dataclass from `order_manager.py` to `types.py` to reduce cross-layer coupling
- [ ] Add `taker_bps` to `FeeTier.rt_cost_bps` as an option for non-post_only scenarios
- [ ] REST fallback for order placement when WS2 is temporarily disconnected
- [ ] FIX API support as alternative to WebSocket for lower-latency execution
- [ ] Backtesting harness with historical tick data replay through the strategy loop

## License

Private. All rights reserved.
