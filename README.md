# iCryptoTrader

A Bitcoin trading bot for **Kraken** that automatically buys low and sells high using a grid strategy — while keeping your German taxes as low as legally possible.

> **In plain English:** The bot places a ladder of buy orders below the current BTC price and sell orders above it. When the price dips, it buys. When it bounces back, it sells. Each round-trip earns a small profit. It does this 24/7, and it knows German tax law so it avoids selling BTC that would trigger unnecessary taxes.

---

## How the Grid Works

```
Sell $500 @ $86,500   ← sell order 3
Sell $500 @ $86,000   ← sell order 2
Sell $500 @ $85,500   ← sell order 1
       ── $85,000 ──  ← current BTC price
Buy  $500 @ $84,500   ← buy order 1
Buy  $500 @ $84,000   ← buy order 2
Buy  $500 @ $83,500   ← buy order 3
```

When the price drops to $84,500, the bot buys. When it rises back to $85,500, the bot sells. The difference (minus fees) is your profit. The grid automatically re-places orders after each fill, so it keeps working around the clock.

**The twist:** In Germany, if you hold BTC for more than 365 days, the profit from selling it is completely tax-free. This bot tracks every single purchase (called a "lot") and tries to only sell lots that have already passed the 365-day mark. If all your lots are too young, the bot simply waits — unless there's an emergency (like a 20% crash).

---

## Quick Start

### What You Need

- **Python 3.11 or newer** — [Download here](https://www.python.org/downloads/)
- **A Kraken account** — [Sign up here](https://www.kraken.com/)
- **Kraken API keys** — Create at [kraken.com/u/security/api](https://www.kraken.com/u/security/api)
  - Enable permissions: "Create & Modify Orders" + "Query Open Orders & Trades"

### Step 1: Install

```bash
git clone <repo-url> && cd iCryptoTrader
pip install -e ".[dev]"
```

### Step 2: Configure

```bash
# Interactive setup wizard — walks you through everything
python -m icryptotrader setup
```

Or manually edit `config/default.toml` (see [Configuration](#configuration) below).

### Step 3: Start Paper Trading

Start small to make sure everything works before risking real money:

```bash
# Edit config first: set order_size_usd = "50" and grid.levels = 3
python -m icryptotrader run
```

### Step 4: Monitor

- **Telegram alerts** — Get notified on your phone for every buy/sell (see [Telegram setup](#telegram-setup))
- **Web dashboard** — Open `http://localhost:8080` in your browser (see [Dashboard setup](#dashboard-setup))
- **Backtest** — Test the strategy on historical data before going live:
  ```bash
  python -m icryptotrader backtest --data data/btc_prices.csv
  ```

### Step 5: Go Live

Once you're comfortable, increase `order_size_usd` and `grid.levels` in the config.

> **Warning:** This is a trading bot that uses real money. Start small, monitor closely, and never invest more than you can afford to lose. Past performance (including backtests) does not guarantee future results.

---

## Key Features

### Trading
- **Grid Trading Engine** — Automatically places buy and sell orders in a symmetric grid around the current price
- **Bollinger Band Spacing** — Grid spacing automatically widens when the market is volatile and tightens when it's calm (so you don't get caught in big swings)
- **Avellaneda-Stoikov Model** — Optional optimal market-making model where spread and inventory skew both scale with volatility (the key A-S insight: when vol is high AND you hold inventory, the skew is much larger)
- **Order Book Imbalance (OBI)** — Microstructure signal from L2 book depth drives spacing asymmetry (bullish book = tighter buys, wider sells)
- **AI Signal Engine** — Optionally asks an AI (Gemini, Claude, or GPT) for market direction and adjusts the grid accordingly
- **Dynamic Grid Sizing** — Order sizes automatically scale down during scary markets and scale up during calm ones
- **Multi-Pair Support** — Trade multiple pairs (e.g., BTC/USD + BTC/EUR) with weighted capital allocation
- **Event-Driven Architecture** — WS callbacks (book updates, trades, fills) wake the tick loop instantly instead of fixed polling

### Risk Protection
- **Circuit Breaker** — If the price moves more than 3% in 60 seconds, the bot freezes to avoid trading in a crash
- **Drawdown Protection** — Pauses trading when your portfolio drops 15%, and starts emergency selling at 20%
- **Hedge Manager** — Automatically reduces buy orders during drawdowns to limit your exposure
- **Dead Man's Switch** — If the bot disconnects from Kraken, all orders are automatically cancelled (so you're never exposed without the bot watching)
- **Inventory Limits** — Prevents the bot from going "all-in" on BTC; keeps your allocation balanced

### German Tax Optimization
- **FIFO Tax Ledger** — Tracks every BTC purchase with exact cost basis in both USD and EUR
- **365-Day Rule** — Only sells BTC lots older than 365 days (tax-free under German law)
- **Freigrenze Protection** — Keeps your annual taxable gains below EUR 1,000 (the tax-free threshold)
- **Tax-Loss Harvesting** — If you're above the Freigrenze, proactively sells losing positions to offset gains
- **Annual Tax Report** — Auto-generates the Anlage SO form (CSV/JSON) for your tax return
- **ECB Exchange Rates** — Uses official ECB EUR/USD rates for all tax calculations (accepted by the Finanzamt)

### Operations
- **Telegram Notifications** — Real-time alerts for fills, risk state changes, tax unlocks, and daily P&L
- **Web Dashboard** — Browser-based status page with portfolio overview, grid state, and lot ages
- **Graceful Shutdown** — Press Ctrl+C and the bot safely cancels all orders, saves your ledger, and exits
- **Crash-Safe Persistence** — Your ledger is saved to disk after every trade using atomic writes (no data loss even if the bot crashes mid-write)
- **Prometheus Metrics** — Export trading metrics to your monitoring stack

---

## Configuration

All settings live in `config/default.toml`. The setup wizard creates this for you, but here's what each section does:

```toml
pair = "XBT/USD"                   # Which pair to trade (XBT = BTC on Kraken)
log_level = "INFO"                 # How much logging ("DEBUG" for troubleshooting)
persistence_backend = "json"       # How to save the ledger ("json" or "sqlite")

[kraken]
api_key = ""                       # Your Kraken API key
api_secret = ""                    # Your Kraken API secret (keep this safe!)

[grid]
levels = 5                         # How many buy + sell orders to place (5 = 5 buys + 5 sells)
order_size_usd = "500"             # How much USD per order ($500 = ~0.006 BTC at $85k)
min_spacing_bps = "20"             # Minimum gap between orders in basis points (20 bps = 0.20%)
post_only = true                   # Only place maker orders (lower fees)

[risk]
max_portfolio_drawdown_pct = 0.15  # Pause trading if portfolio drops 15% from peak
emergency_drawdown_pct = 0.20      # Emergency sell if portfolio drops 20%
price_velocity_freeze_pct = 0.03   # Freeze if price moves 3% in 60 seconds
price_velocity_cooldown_sec = 30   # Wait 30 seconds after freeze before resuming

[tax]
holding_period_days = 365          # Days to hold BTC before it's tax-free (German law: 365)
near_threshold_days = 330          # Don't sell lots this close to becoming tax-free
annual_exemption_eur = "1000"      # German Freigrenze (EUR 1,000/year tax-free threshold)
emergency_dd_override_pct = 0.20   # Override tax protection in a 20% drawdown emergency
harvest_enabled = false            # Tax-loss harvesting (turn on to optimize taxes)
harvest_min_loss_eur = "50"        # Only harvest losses bigger than EUR 50
harvest_max_per_day = 3            # Max harvest trades per day
harvest_target_net_eur = "800"     # Try to keep taxable gains below EUR 800

[regime.range_bound]               # Settings when the market is calm and sideways
btc_target_pct = 0.50              # Target 50% of portfolio in BTC
btc_max_pct = 0.60                 # Never go above 60% BTC
btc_min_pct = 0.40                 # Never go below 40% BTC
grid_levels = 5                    # Use all 5 grid levels
order_size_scale = 1.0             # Full order size

[regime.trending_up]               # Settings when BTC is going up
order_size_scale = 0.75            # Reduce order size to 75%

[regime.chaos]                     # Settings when the market is crashing
btc_target_pct = 0.00             # Get out of BTC entirely
btc_max_pct = 0.05
grid_levels = 0                    # Don't place any orders
signal_enabled = false
order_size_scale = 0.5             # Half order size

[bollinger]
enabled = true                     # Use Bollinger Bands for adaptive spacing
window = 20                        # Look at the last 20 price ticks
multiplier = 2.0                   # Standard Bollinger multiplier
spacing_scale = 0.5                # How much Bollinger affects spacing
min_spacing_bps = "15"             # Never go below 15 bps (0.15%)
max_spacing_bps = "200"            # Never go above 200 bps (2.0%)

[avellaneda_stoikov]
enabled = false                    # When true, replaces Bollinger + DeltaSkew with A-S model
gamma = 0.3                        # Risk aversion [0.01, 2.0]. Higher = wider spread
max_spread_bps = "500"             # Hard cap on half-spread per side
max_skew_bps = "50"                # Hard cap on inventory + OBI skew combined
obi_sensitivity_bps = "10"         # OBI of +/-1.0 maps to this many bps adjustment

[telegram]
enabled = false                    # Set to true to enable Telegram alerts
bot_token = ""                     # Get from @BotFather on Telegram
chat_id = ""                       # Your Telegram chat ID

[ai_signal]
enabled = false                    # Set to true to enable AI signals
provider = "gemini"                # "gemini", "anthropic", or "openai"
api_key = ""                       # API key for your chosen AI provider
model = "gemini-2.0-flash"         # Which AI model to use
cooldown_sec = 300                 # Wait 5 minutes between AI queries
weight = 0.3                       # How much the AI influences trading (0-1)
timeout_sec = 10                   # Give up if AI doesn't respond in 10s

[metrics]
enabled = false                    # Prometheus metrics endpoint
port = 9090

[hedge]
enabled = false                    # Auto-reduce exposure during drawdowns
trigger_drawdown_pct = 0.10        # Activate at 10% drawdown
strategy = "reduce_exposure"       # Reduce buy orders
max_reduction_pct = 0.50           # Cancel up to 50% of buy levels

[web]
enabled = false                    # Browser dashboard
port = 8080
host = "127.0.0.1"
username = ""                      # Leave empty for no login required
password = ""

# Multi-pair allocation (optional — uncomment to trade multiple pairs)
# [[pairs]]
# symbol = "XBT/USD"
# weight = 0.7                    # 70% of capital to BTC/USD
# [[pairs]]
# symbol = "XBT/EUR"
# weight = 0.3                    # 30% of capital to BTC/EUR
```

### Telegram Setup

1. Open Telegram, search for **@BotFather**, and type `/newbot`
2. Follow the prompts to create a bot — you'll get a **bot token**
3. Send any message to your new bot, then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` to find your **chat_id**
4. Set both values in `config/default.toml` and set `enabled = true`

### Dashboard Setup

1. Set `web.enabled = true` in `config/default.toml`
2. Optionally set `username` and `password` for login protection
3. Open `http://localhost:8080` in your browser after starting the bot

---

## Glossary

| Term | Meaning |
|------|---------|
| **bps (basis points)** | 1 bps = 0.01%. So 20 bps = 0.20%. Used to describe tiny price differences. |
| **Grid** | A ladder of buy and sell orders placed at regular intervals above and below the current price. |
| **Spacing** | The gap between grid levels. Wider spacing = fewer trades but larger profits per trade. |
| **FIFO** | "First In, First Out" — when you sell BTC, the oldest purchased lot is sold first (German tax law requires this). |
| **Lot** | A single BTC purchase with its own cost basis, timestamp, and tax status. |
| **Haltefrist** | German holding period: 365 days. BTC held longer than this is tax-free when sold. |
| **Freigrenze** | German tax-free threshold: EUR 1,000/year. If your total crypto gains stay under this, you pay zero tax. |
| **Drawdown** | How much your portfolio has dropped from its highest point. 15% drawdown = you've lost 15% from the peak. |
| **Circuit Breaker** | Emergency stop that freezes trading when the price is moving too fast (like in a crash). |
| **DMS (Dead Man's Switch)** | A safety feature: if the bot stops sending heartbeats to Kraken, all orders are cancelled automatically. |
| **Regime** | The bot classifies the market into 4 states: Range-bound (sideways), Trending Up, Trending Down, or Chaos. Each regime uses different trading parameters. |
| **Delta Skew** | When you hold too much BTC, the bot makes sell orders tighter (easier to hit) and buy orders wider (harder to hit) to rebalance. |
| **Bollinger Bands** | A statistical measure of how volatile the price is. The bot uses this to automatically adjust grid spacing. |
| **VWAP** | Volume-Weighted Average Price — a more stable "average price" that accounts for trade volume. Used as the grid center. |
| **Maker / Taker** | Maker = your order sits in the book waiting (lower fees). Taker = your order fills immediately (higher fees). The bot uses maker-only orders. |
| **Anlage SO** | The section of the German tax return where you report crypto gains/losses. |

---

## Architecture (for Developers)

If you're a developer looking to understand or modify the code, this section is for you.

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
       +--------+-------+-------+---------+
       |        |       |       |         |
  +----v---+ +--v--+ +--v---+ +v------+ +v---------+
  | Regime | | Grid| |Delta | |Invent.| |AI Signal |
  | Router | | Eng.| |Skew  | |Arbiter| |Engine    |
  +--------+ +-----+ +------+ +-------+ +----------+
       |        |       |       |         |
       +--------+-------+-------+---------+
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

Each strategy tick (event-driven, wakes on WS book/trade/fill events with 1s max fallback) runs the following pipeline:

1. **Market data** — Update inventory price, regime router, VWAP
2. **Circuit breaker** — Check price velocity; freeze if >3% move in 60s
3. **Risk update** — Compute portfolio drawdown, update pause state
4. **Regime classification** — EWMA vol + momentum -> Regime enum
5. **AI signal** — Query LLM for directional bias (rate-limited, async)
6. **Grid computation** — N buy/sell levels with fee-aware spacing
7. **Tax gating** — Tax Agent recommends sell-level count based on sellable ratio
8. **Hedge evaluation** — HedgeManager caps buy levels and boosts sell levels during drawdowns
9. **Delta skew / A-S** — Skew buy/sell spacing based on allocation deviation + OBI + AI bias (or A-S model if enabled)
10. **Order decisions** — Per-slot decide_action(): Add / Amend / Cancel / Noop
11. **Rate limiting** — Gate add/amend commands against Kraken's rate counter
12. **Dispatch** — Commands sent to WS2; fills update FIFO ledger

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
- **Freigrenze**: Annual gains under EUR 1,000 are fully exempt (but if you go even 1 cent over, the entire amount is taxable)
- **Near-threshold protection**: Lots 330-365 days old are protected from sale (too close to becoming tax-free)
- **Emergency override**: Portfolio drawdown >20% overrides all tax locks (survival first)
- **ECB reference rate**: All EUR conversions use the official ECB daily rate
- **Tax-loss harvesting**: Proactive selling of underwater lots to offset YTD gains, targeting net below Freigrenze
- **Annual report automation**: Auto-generate Anlage SO CSV/JSON at year-end

### Project Structure

```
src/icryptotrader/
  __init__.py              # Package root, version
  __main__.py              # CLI entry point (run / backtest / setup subcommands)
  types.py                 # Shared enums, dataclasses (HarvestRecommendation, FeeTier, ...)
  config.py                # TOML config loader with validation and typed dataclasses
  logging_setup.py         # Structured JSON / dev logging
  lifecycle.py             # Graceful shutdown, startup reconciliation, reconnect recovery
  metrics.py               # Prometheus-compatible metrics registry and HTTP server
  watchdog.py              # Background process health monitor (tick rate, WS, memory)
  pair_manager.py          # Multi-pair diversification with capital allocation + correlation
  setup_wizard.py          # Interactive first-run configuration wizard

  strategy/
    strategy_loop.py       # Main tick orchestrator with ledger auto-save, event-driven
    grid_engine.py         # Grid level computation
    regime_router.py       # EWMA vol / momentum regime classifier + VWAP tracking
    bollinger.py           # Bollinger Band + ATR volatility-adaptive grid spacing
    avellaneda_stoikov.py  # Avellaneda-Stoikov optimal market making model
    ai_signal.py           # Multi-provider AI signal engine (Gemini, Anthropic, OpenAI)

  order/
    order_manager.py       # Amend-first slot state machine
    rate_limiter.py        # Kraken per-pair rate counter tracker

  risk/
    risk_manager.py        # Drawdown tracking, pause states, circuit breaker (with hysteresis)
    delta_skew.py          # Allocation deviation -> quote asymmetry
    hedge_manager.py       # Portfolio delta reduction during adverse conditions

  inventory/
    inventory_arbiter.py   # BTC/USD allocation enforcement per regime

  tax/
    fifo_ledger.py         # FIFO lot tracking, cost basis, atomic persistence, underwater_lots()
    tax_agent.py           # Sell veto, Freigrenze, near-threshold, tax-loss harvesting
    tax_report.py          # Anlage SO report generator (CSV, JSON, text) + auto-generate
    ecb_rates.py           # ECB EUR/USD reference rate fetcher
    lot_viewer.py          # Lot age visualization (table, histogram, unlock schedule)

  fee/
    fee_model.py           # Kraken fee tier schedule and profitability gate

  notify/
    telegram.py            # Interactive Telegram bot (fills, risk, tax, P&L, inline keyboards)

  ws/
    ws_codec.py            # Kraken WS v2 message encode/decode (orjson)
    ws_public.py           # WS1: public market data feed
    ws_private.py          # WS2: authenticated trading + executions + balances
    book_manager.py        # L2 order book with CRC32 checksum validation

  web/
    dashboard.py           # Async HTTP dashboard with status API and embedded HTML UI

  backtest/
    engine.py              # Historical price replay through the strategy loop

config/
  default.toml             # Default configuration

tests/                     # 759 tests across 35 test files
```

---

## AI Signal Engine

The bot can optionally ask an AI for market direction. This is **completely optional** — the bot works perfectly without it.

| Provider | Model | Use Case |
|----------|-------|----------|
| **Google Gemini** | `gemini-2.0-flash` | Default — fast, low-cost, good for frequent signals |
| **Anthropic Claude** | `claude-sonnet-4-6` | Higher reasoning quality for complex market analysis |
| **OpenAI** | `gpt-4o` | Alternative provider for redundancy |

The AI signal provides:
- **Direction**: STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL
- **Confidence**: 0.0 to 1.0 — used to weight the signal
- **Bias (bps)**: Applied as additional grid spacing skew
- **Regime hint**: Optional regime suggestion from the AI

The engine is **fail-open** — if the AI provider is unreachable, the bot continues with grid-only trading. Calls are rate-limited by `cooldown_sec` to avoid quota exhaustion.

## Fee Model

Grid spacing is auto-calibrated to be profitable at your current Kraken fee tier. You don't need to configure this — it's automatic based on your 30-day trading volume:

| 30-Day Volume (USD) | Maker Fee | Taker Fee | Min Grid Spacing |
|---------------------|-----------|-----------|-----------------|
| $0 - $10K           | 0.25%     | 0.40%     | 0.65%           |
| $10K - $50K         | 0.20%     | 0.35%     | 0.55%           |
| $50K - $100K        | 0.14%     | 0.24%     | 0.43%           |
| $100K - $250K       | 0.12%     | 0.20%     | 0.39%           |
| $1M - $5M           | 0.04%     | 0.14%     | 0.23%           |
| $10M+               | 0.00%     | 0.10%     | 0.15%           |

*The bot ensures every trade is profitable by making the grid spacing at least 2x your maker fee + a safety margin.*

---

## Testing

```bash
# Full suite (759 tests)
pytest -v

# With coverage report
pytest --cov=icryptotrader --cov-report=term-missing

# Type checking (strict mode)
mypy src/

# Linting
ruff check src/ tests/

# Specific modules
pytest tests/test_fifo_ledger.py -v     # FIFO ledger + underwater lots (39 tests)
pytest tests/test_telegram.py -v        # Interactive Telegram bot (57 tests)
pytest tests/test_risk_manager.py -v    # Drawdown, pause states, circuit breaker (38 tests)
pytest tests/test_order_manager.py -v   # Slot states, amend-first logic (33 tests)
pytest tests/test_book_manager.py -v    # L2 book + CRC32 checksums (29 tests)
pytest tests/test_tax_agent.py -v       # Tax veto + harvest recommendations (25 tests)
pytest tests/test_strategy_loop.py -v   # Tick cycle, order dispatch (25 tests)
pytest tests/test_ai_signal.py -v       # AI signal engine — all providers (24 tests)
pytest tests/test_fee_model.py -v       # Fee tiers, profitability gates (24 tests)
pytest tests/test_bollinger.py -v       # Bollinger Band + ATR spacing (23 tests)
pytest tests/test_ws_codec.py -v        # WS v2 message codec (23 tests)
pytest tests/test_config.py -v          # Config validation (26 tests)
pytest tests/test_regime_router.py -v   # Regime classification + VWAP (20 tests)
pytest tests/test_grid_engine.py -v     # Grid levels + spacing (19 tests)
pytest tests/test_backtest.py -v        # Backtest simulation (18 tests)
pytest tests/test_lifecycle.py -v       # Graceful shutdown + reconciliation (16 tests)
pytest tests/test_pair_manager.py -v    # Multi-pair diversification (16 tests)
pytest tests/test_tax_report.py -v      # Anlage SO export (16 tests)
pytest tests/test_lot_viewer.py -v      # Lot age visualization (15 tests)
pytest tests/test_integration_e2e.py -v # End-to-end integration (13 tests)
pytest tests/test_main.py -v           # CLI entry point (12 tests)
pytest tests/test_metrics.py -v        # Prometheus metrics (12 tests)
pytest tests/test_backtest_engine.py -v # Backtest engine (11 tests)
pytest tests/test_hedge_manager.py -v  # Hedge manager (10 tests)
pytest tests/test_strategy_loop_wiring.py -v  # Bollinger, AI, SQLite wiring (16 tests)
pytest tests/test_delta_skew.py -v     # Delta skew + OBI integration (14 tests)
pytest tests/test_avellaneda_stoikov.py -v  # A-S optimal market making (19 tests)
pytest tests/test_watchdog.py -v       # Process watchdog (6 tests)
pytest tests/test_web_dashboard.py -v  # Web dashboard (7 tests)
```

**759 tests**, all business logic paths thoroughly tested, including 13 end-to-end integration tests exercising the full tick cycle with real components (no mocks). Uncovered lines are primarily async WebSocket connection code and the interactive setup wizard.

---

## Roadmap

### Phase 2 — Production Hardening (Done)

- [x] Ledger persistence on fill (async executor save)
- [x] Atomic ledger writes (crash-safe)
- [x] httpx.AsyncClient reuse
- [x] Balances channel subscription
- [x] Telegram notifications + interactive BotActionProvider
- [x] Graceful shutdown (SIGTERM/SIGINT + watchdog)
- [x] Startup reconciliation flow
- [x] Config validation
- [x] OrderManager fill/execution/ack callback wiring
- [x] ECB rate service (background task, 4h refresh)
- [x] WebDashboard wired with risk_manager + metrics
- [x] Hedge manager properly wired (buy_level_cap, sell_level_boost, sell_spacing_tighten)
- [x] FIFO ledger aggregate caching (total_btc, tax_free_btc)
- [ ] Process isolation (Feed + Strategy split)

### Phase 3 — Observability & Resilience (Done)

- [x] Order book checksum validation (CRC32)
- [x] Circuit breaker hysteresis
- [x] Reconnect state recovery
- [x] Structured metrics export (Prometheus)
- [x] Web dashboard

### Phase 4 — Strategy Enhancements (Done)

- [x] Bollinger Band volatility spacing
- [x] Dynamic grid sizing (per-regime)
- [x] AI Signal Engine (Gemini, Claude, GPT)
- [x] Volume-weighted mid-price (VWAP)
- [x] Multi-pair support (PairManager)
- [x] Order Book Imbalance (OBI) → grid spacing asymmetry
- [x] Avellaneda-Stoikov optimal market making model
- [x] Event-driven tick loop (WS callbacks instead of polling)
- [ ] Adaptive regime thresholds

### Phase 5 — Tax Optimization (Done)

- [x] Tax-loss harvesting
- [x] Lot age visualization
- [x] Annual report automation (Anlage SO)
- [ ] Multi-year carry-forward

### Backlog

- [ ] Process isolation (Feed + Strategy split for crash safety)
- [ ] REST fallback for order placement
- [ ] FIX API support for lower latency

---

## License

Private. All rights reserved.
