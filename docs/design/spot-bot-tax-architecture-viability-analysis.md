# Spot Bot Tax Architecture — Comprehensive Viability Analysis

---

## CRITICAL LEGAL UPDATE: €20k Futures Loss Cap ABOLISHED

**The entire tax premise for this project has changed.**

The Annual Tax Act 2024 (Jahressteuergesetz 2024), passed by the Bundesrat on November 22, 2024 and published in BGBl on December 5, 2024, **retroactively abolished** the €20,000 futures loss deduction cap. Specifically, §20 Abs. 6 Satz 5 and 6 EStG were deleted entirely.

**What this means:**
- Losses from futures/derivatives (Termingeschäfte) can now be offset **in full** against ALL capital gains (Einkünfte aus Kapitalvermögen) — no €20k cap, no separate loss bucket
- Applies **retroactively** to all open tax assessments since the rule's introduction (2021)
- The BFH had already ruled the cap likely unconstitutional (VIII B 113/23, June 2024)
- Banks must implement the change by January 1, 2026; for 2024/2025 assessments, manual correction via tax return is possible

**Impact on this project:** The original motivation — "futures trading is fiscally suicidal for private persons" — **no longer applies**. Your existing HFT Futures bot on Kraken Futures is now fiscally viable again. Futures losses offset fully against futures gains under the flat 25% Abgeltungssteuer + Soli.

**However, the spot bot still has compelling advantages:**
1. §23 EStG: BTC held >1 year is **completely tax-free** (0% vs 26.375% Abgeltungssteuer on futures)
2. No counterparty/liquidation risk (spot vs perpetual futures)
3. No funding rate drag (perpetual futures pay funding every 8h)
4. Simpler operational model (no margin management, no liquidation engine)
5. BTC accumulation for long-term wealth building

**The question shifts from "must I move to spot?" to "is spot BETTER than futures given all tradeoffs?"**

This analysis proceeds assuming you want the full evaluation regardless.

---

## Section 1: Strategy Viability at Each Fee Tier

### Fee Schedule Reference (Maker fees, Kraken Pro, crypto pairs)

| Tier | 30d Volume | Maker Fee | Taker Fee | RT Cost (maker+maker) |
|------|-----------|-----------|-----------|----------------------|
| Base | $0+ | 0.25% | 0.40% | 50 bps |
| T1 | $10k+ | 0.20% | 0.35% | 40 bps |
| T2 | $50k+ | 0.14% | 0.24% | 28 bps |
| T3 | $100k+ | 0.12% | 0.20% | 24 bps |
| T4 | $250k+ | 0.08% | 0.18% | 16 bps |
| T5 | $500k+ | 0.06% | 0.16% | 12 bps |
| T6 | $1M+ | 0.04% | 0.14% | 8 bps |
| T7 | $5M+ | 0.02% | 0.12% | 4 bps |
| T8 | $10M+ | 0.00% | 0.10% | 0 bps (maker) |

### 1.1 Grid Strategy (Mean-Reversion)

Assumptions: $5k capital, 50/50 USD/BTC split, BTC at ~$85k, daily vol ~2-3%.

**Base Tier ($0+, 0.25% maker, RT cost = 50 bps)**

| Metric | Value | Notes |
|--------|-------|-------|
| Min grid spacing | 80 bps | 80 - 50 = 30 bps gross edge; ~10 bps adverse selection → 20 bps net |
| Net edge per RT | ~20 bps | $1.00 on a $500 level |
| Grid levels | 5 buy + 5 sell | $500 per level |
| Expected RTs/day | 3-8 | BTC 2-3% daily range / 0.8% spacing, mean-reversion dependent |
| Daily profit | $3-8 | 5 avg RTs × $1.00 |
| Monthly profit | $90-240 | ~$150 mid-case |
| Daily volume | $3k-8k | 5 RTs × $500 × 2 sides |
| Monthly volume | $90k-240k | ~$150k mid-case |
| Win rate needed | >62% | To overcome 50 bps RT cost at 80 bps spacing |
| Sharpe (annualized) | 0.5-1.0 | Low due to inventory risk and fee drag |
| Max drawdown | -15% to -25% | Grid fully filled in 10%+ BTC drop |

**$100k Tier (0.12% maker, RT cost = 24 bps)**

| Metric | Value | Notes |
|--------|-------|-------|
| Min grid spacing | 40 bps | 40 - 24 = 16 bps gross; viable but thin |
| Optimal spacing | 60 bps | 60 - 24 = 36 bps gross; ~10 bps adverse = 26 bps net |
| Net edge per RT | ~26 bps | $1.30 on $500 level |
| Expected RTs/day | 5-15 | Tighter grid → more crossings |
| Daily profit | $6.50-19.50 | ~$13 mid-case |
| Monthly profit | $195-585 | ~$390 mid-case |
| Monthly volume | $150k-450k | Approaching $500k tier |
| Sharpe | 0.8-1.5 | Improved by lower fee drag |

**$500k Tier (0.06% maker, RT cost = 12 bps)**

| Metric | Value | Notes |
|--------|-------|-------|
| Optimal spacing | 35 bps | 35 - 12 = 23 bps net edge |
| Net edge per RT | ~23 bps | $1.15 on $500 level |
| Expected RTs/day | 10-25 | Much tighter grid |
| Daily profit | $11.50-28.75 | ~$20 mid-case |
| Monthly profit | $345-862 | ~$600 mid-case |
| Monthly volume | $300k-750k | Stable at this tier |
| Sharpe | 1.0-2.0 | Starting to look like a real strategy |

**$1M+ Tier (0.04% maker, RT cost = 8 bps)**

| Metric | Value | Notes |
|--------|-------|-------|
| Optimal spacing | 25 bps | 25 - 8 = 17 bps net |
| Net edge per RT | ~17 bps | $0.85 on $500 level |
| Expected RTs/day | 15-35 | Very tight grid, high frequency |
| Daily profit | $12.75-29.75 | ~$21 mid-case |
| Monthly profit | $382-892 | ~$630 mid-case (edge per trade lower but more trades) |
| Monthly volume | $450k-1.05M | May need capital increase to sustain |
| Sharpe | 1.5-2.5 | Good risk-adjusted performance |

### 1.2 Momentum/Breakout Strategy

Momentum works differently — it captures larger moves but trades less frequently, needs taker fills sometimes.

**Base Tier ($0+)**

| Metric | Value | Notes |
|--------|-------|-------|
| Min signal threshold | 150 bps | Must overcome 50 bps RT + 50 bps expected slippage |
| Expected edge per trade | 50-100 bps | After fees, when signal is correct |
| Trades/day | 1-3 | Selective, only on strong signals |
| Win rate needed | >45% | With 2:1 reward/risk ratio |
| Monthly volume | $15k-45k | Low frequency, larger size |
| Sharpe | 0.3-0.8 | High variance, few trades |
| Viable? | **Marginal** | Fee drag kills edge on most signals |

**$500k+ Tier (0.06% maker)**

| Metric | Value | Notes |
|--------|-------|-------|
| Min signal threshold | 50 bps | 12 bps RT + slippage |
| Edge per trade | 30-80 bps | Viable with good signals |
| Trades/day | 2-5 | More signals become actionable |
| Sharpe | 0.8-1.5 | Reasonable |
| Viable? | **Yes** | Becomes a useful complement to grid |

### 1.3 Classical Mean Reversion (non-grid)

**Verdict: NOT VIABLE at any tier below $5M+.** Classical mean-reversion trades ~5-20 bps price reversions. At 50 bps RT cost (base tier), every trade is a guaranteed loss. Even at $1M tier (8 bps RT), the edge is too thin after adverse selection. The grid IS the mean-reversion strategy — it just structures it as resting limit orders.

### 1.4 Cross-Pair Arbitrage (BTC/USD vs BTC/USDC or BTC/EUR)

**Verdict: NOT VIABLE for fee tier progression.** Even if arb opportunities exist between BTC/USD and BTC/USDC (typically 1-5 bps), the base tier fees (50 bps RT per pair = 100 bps total) make it impossible. At $10M+ tier (0% maker), it could work — but you can't get there without $10M volume on the primary pair first. Also, BTC/USDC volume may not count toward the same fee tier (needs verification). **Skip this entirely.**

### 1.5 Strategy Viability Summary

| Strategy | Base Tier | $100k | $500k | $1M+ | When Viable? |
|----------|----------|-------|-------|------|-------------|
| Grid (mean-reversion) | Marginal-OK | Good | Strong | Strong | From day 1, improves with tier |
| Momentum/Breakout | Not viable | Marginal | Good | Strong | Phase C only, after $500k tier |
| Classical MR | Dead | Dead | Marginal | Marginal | Never worth building |
| Cross-pair Arb | Dead | Dead | Dead | Maybe | Never at these capital levels |

**Conclusion:** Grid is the only viable strategy from day 1. Momentum becomes a useful complement at $500k+ tier. The others are not worth building.

---

## Section 2: Architecture Reuse Map

### Direct Reuse (copy/adapt imports, no logic changes)

| Component | Files (HFT-Bot) | Reuse Notes |
|-----------|-----------------|-------------|
| ZMQ IPC topology | `utils/` ZMQ setup | PUB/SUB pattern identical: feed → strategy |
| Cython compilation infra | `setup_cython.py` | Build system reusable; hot path modules need new content |
| Telemetry pipeline | `telemetry/` | JSONL format, p50/p95/p99 tracking — direct reuse |
| Telegram integration | `telegram/` | Commands (/status, /kill, /latency) + alerts — direct reuse |
| systemd integration | `deploy/` | Type=notify, WatchdogSec — direct reuse |
| File lock | `utils/` | Single-instance prevention — direct reuse |
| AI Supervisor infrastructure | `supervisor/` | LLM agent scaffolding, tool dispatch — direct reuse |
| Backtest framework (structure) | `backtest/` | Runner/harness reusable; fill model needs adaptation |

### Adapt (modify existing code)

| Component | Changes Needed |
|-----------|---------------|
| **EWMA Volatility** | Same math, but also feeds Regime Router. Add regime classification output. |
| **Flow Toxicity Scoring** | Demote from entry signal to **filter only**. Toxicity > threshold → pause grid, don't enter. Same 5-component model. |
| **Order Book Imbalance** | Same calculation, used as filter + Regime Router feature. |
| **Inventory Skew** | Rename to **Delta Skew**. Replace funding rate component with "deviation from target BTC allocation %". Target allocation is regime-dependent (50%/70%/30%/0%). Core math (skew → quote asymmetry) stays. |
| **Multi-Level Quoting** | Adapt for grid structure (fixed levels vs dynamic levels). Same concept of skewed bid/ask placement. |
| **Drawdown Monitor** | Split into USD drawdown + BTC exposure tracking. Add "accumulation healthy vs problem" classifier. |
| **Price Velocity Circuit Breaker** | Same 15s rolling window, adapted for spot price feed format. |
| **RCA Agent** | Adapt for WS-based execution errors instead of REST errors. |
| **Quant Agent** | Add fee-tier awareness (auto-tighten grid when tier drops). Add "sellable inventory" metric. Add 5 safety layers adapted for spot parameters. |
| **Macro Agent** | Drop funding rate monitoring. Add ETF flow tracking, on-chain exchange inflows/outflows. |
| **Backtest Fill Model** | Adapt for spot execution model (no funding, maker-only, amend_order semantics). |
| **Cython Codec** | New codec for Kraken Spot WS v2 JSON format (different from Futures binary). |
| **Cython Price Math** | Adapt tick sizes for spot pairs (XBT/USD tick = $0.1, different from futures). |

### Replace (fundamentally different mechanism)

| Component | Replacement |
|-----------|-------------|
| **REST Order Client** | **WS2 Order Manager**: All orders via WebSocket v2 `amend_order`/`add_order`/`cancel_order`. No REST in hot path. |
| **Nonce/HMAC Auth** | **WS Token Auth**: Single `GetWebSocketsToken` REST call at startup. Token used for WS2 auth. No per-request signing. |
| **Funding Rate Skew** | **Delta Skew**: Deviation from target allocation → quote asymmetry. |
| **Futures Symbol Handling** | **Spot Pair Handling**: `PF_XBTUSD` → `XBT/USD`. Different tick sizes, lot sizes, API field names. |
| **Cancel+Place Batch Cycle** | **Amend-First Order Loop**: Use `amend_order` (atomic, queue-preserving) instead of cancel-all + batch-place. |

### Irrelevant (drop entirely)

| Component | Why |
|-----------|-----|
| Funding rate fetching | No funding on spot |
| Futures position management | Spot has balance, not "position" |
| Margin/leverage logic | No leverage on spot |
| Liquidation engine | Cannot be liquidated on spot |
| Futures-specific error handling | Different error codes on spot |

### New Required (build from scratch)

| Component | Description | Complexity |
|-----------|-------------|------------|
| **Tax-Aware FIFO Ledger** | Track purchase lots with timestamps, FIFO sell logic, §23 EStG reporting. Core edge. | HIGH |
| **Tax Agent (veto power)** | Evaluates every sell decision, blocks near-threshold lots, manages pause states. | HIGH |
| **Grid State Machine** | Fixed-level grid with state per level (empty/resting/filled/amending). | MEDIUM |
| **Regime Router** | Vol + directional + OBI + toxicity → regime classification → capital allocation gates. | MEDIUM |
| **Fee Model Service** | Pair-aware, tier-aware fees. `expected_net_edge_bps()` check before every order. | LOW |
| **Two-Connection WS Manager** | WS1 (public feeds) + WS2 (private trading+executions). Reconnection, DMS, backpressure. | HIGH |
| **Global Inventory Arbiter** | Grid + Signal share single BTC/USD balance. NET conflicting desires. | MEDIUM |
| **EUR/USD Rate Service** | Daily ECB reference rate fetch for tax basis calculations. | LOW |
| **Signal Engine** | Momentum/breakout detector with trailing stops and OTO orders. Phase C only. | MEDIUM |
| **Tax Report Generator** | Annual Anlage SO report with all required §23 EStG fields. | LOW |

---

## Section 3: WebSocket-Native Architecture Design

### 3.1 Two-Connection Topology — Confirmed Optimal

- **WS1 (Public)**: `wss://ws.kraken.com/v2` — L2/L3 book, trade tape, ticker, OHLC, instrument
- **WS2 (Private)**: `wss://ws-auth.kraken.com/v2` — ALL trading commands + executions + balances channels

Why this is optimal: Public data flood cannot starve order ack reads; independent reconnection; matches Kraken's endpoint split. A single-connection approach is not viable (public data unavailable on ws-auth). Three connections (splitting trade commands from executions) is over-engineered — Cloudflare rate limits make extra connections costly.

### 3.2 CRITICAL CORRECTION: Use `amend_order`, NOT `edit_order`

The user's prompt specifies `edit_order` throughout. **This must be corrected to `amend_order`.**

| Feature | `edit_order` (legacy) | `amend_order` (atomic) |
|---------|----------------------|----------------------|
| Engine model | Cancel-new (multi-phase) | In-place modification (single-phase) |
| Queue priority | **Always lost** | **Preserved** (except on price change) |
| Order ID | New ID assigned | Same ID preserved |
| Fill history | Lost (new order) | Preserved on same order |
| Rate limits | Standard | Higher (more efficient) |
| Conditional close | Supported | NOT supported (must cancel+new) |
| Sequencing guarantee | N/A | **Not guaranteed on WS** (guaranteed on FIX) |

**Decision: `amend_order` is the default. `edit_order` (cancel+new) only when:**
- Changing order side (buy↔sell) — never amend side
- Order has conditional close terms — amend doesn't support these
- Changing time-in-force or order type — non-amendable attributes

### 3.3 Amend-First Order State Machine

```
States per order slot:
  EMPTY → PENDING_NEW → LIVE → AMEND_PENDING → LIVE (updated)
                                              → FILLED → EMPTY
                         LIVE → CANCEL_PENDING → EMPTY

Transitions:
  EMPTY + strategy wants level → add_order → PENDING_NEW
  PENDING_NEW + ack (exec_type=new) → LIVE
  LIVE + price/qty change needed → amend_order → AMEND_PENDING
  AMEND_PENDING + ack (exec_type=restated) → LIVE (updated params)
  LIVE + cancel needed → cancel_order → CANCEL_PENDING
  LIVE + filled (exec_type=trade, fully) → EMPTY (+ FIFO ledger update)
  LIVE + partial fill → LIVE (reduced qty)

Critical rule: NEVER stack commands on a PENDING slot.
  If slot is PENDING_NEW or AMEND_PENDING, wait for ack or timeout (500ms).
  If timeout → force cancel_order (stale).
```

### 3.4 Process Architecture

**Two-process model (preserving existing pattern):**

- **Feed Process**: WS1 → Cython codec → ZMQ PUB → IPC. Stateless, restartable.
- **Strategy Process**: ZMQ SUB ← market data. Contains: signal pipeline, grid engine, signal engine, regime router, tax agent, inventory arbiter, order manager, WS2 connection.

WS2 is in the Strategy Process because: (a) minimizes signal-to-wire latency (no IPC hop for orders), (b) order slot state machine needs atomic access from both strategy ticks and execution callbacks, (c) fills must immediately update FIFO ledger and inventory.

### 3.5 Reconnection & State Recovery

Orders survive WS2 disconnect (they live in the matching engine). Recovery flow:
1. Mark all PENDING slots as UNCERTAIN, pause strategy
2. Reconnect with exponential backoff (instant × 3, then 5s, 10s, 20s, max 30s)
3. If reconnected < 60s: re-arm `cancel_after` heartbeat, subscribe executions (snap_orders=true)
4. Reconcile: snapshot → local state comparison (by order_id and cl_ord_id)
5. Handle orphans: cancel any snapshot orders not in local state
6. If reconnect > 60s: `cancel_after` timer fires server-side, all orders cancelled → clean slate

### 3.6 Latency Improvement

| Path | REST (current) | WS2 (new) | Improvement |
|------|---------------|-----------|-------------|
| Single order RT | 50-200ms | 5-20ms | 4-10x |
| 10-level requote | 160-400ms | 25-50ms | 6-8x |
| Auth overhead | HMAC per request | One-time token | Negligible per-request |

Most significant gain: atomic `amend_order` replaces cancel-all + batch-place (11 operations → 10 amends).

### 3.7 Backpressure Handling

Rate limit model: per-pair counter, Pro tier max = 180, decay = 3.75/sec. The bot:
- Tracks estimated rate counter locally, syncs from `rate_count` in execution events
- Uses 80% headroom (max 144 effective)
- Priority queue: cancels > risk-critical amends > normal amends > new orders
- If throttled: skip this tick's requotes, retry next tick

**WS sequencing caveat**: Kraken does NOT guarantee sequencing of unacknowledged amends/cancels on WebSocket (unlike FIX). The bot must never stack multiple pending operations on the same order slot.

---

## Section 4: Fee Bootstrapping Analysis

### 4.1 Confirmed: Stablecoin/FX Volume Excluded

Kraken explicitly separates stablecoin/FX pairs from crypto pairs for fee tier calculation. Volume on USDC/USD, EUR/USD, etc. does NOT count toward BTC/USD fee progression. All volume must come from actual crypto pair trading.

### 4.2 Fee Tier Progression Timeline ($5k Capital, BTC/USD Only)

**Assumptions:**
- Grid bot running 24/7 on BTC/USD
- $500 per grid level, 5 buy + 5 sell levels
- BTC average daily range: 2-3%
- Mid-case round-trips per day at each spacing

| Month | Fee Tier | Maker Fee | Grid Spacing | Avg RTs/day | Daily Volume | Monthly Volume | Cumulative | Monthly Profit |
|-------|----------|-----------|-------------|-------------|-------------|----------------|------------|---------------|
| 1 | Base (0.25%) | 25 bps | 80 bps | 5 | $5k | $150k | $150k | ~$150 |
| 2 | T3 ($100k) | 12 bps | 60 bps | 8 | $8k | $240k | $390k | ~$312 |
| 3 | T4 ($250k) | 8 bps | 45 bps | 12 | $12k | $360k | $750k | ~$432 |
| 4 | T5 ($500k) | 6 bps | 35 bps | 15 | $15k | $450k | $1.2M | ~$517 |
| 5 | T6 ($1M) | 4 bps | 28 bps | 18 | $18k | $540k | $1.74M | ~$486 |
| 6+ | T6 ($1M) | 4 bps | 28 bps | 18 | $18k | $540k | $2.28M | ~$486 |

**Note:** The Quant Agent auto-tightens spacing as fees drop. This creates the positive feedback loop: lower fees → tighter grid → more round-trips → more volume → lower fees → repeat.

**Key milestones:**
- $10k tier: Reached in ~2 days (trivial)
- $50k tier: Reached in ~10 days
- $100k tier: Reached in ~month 1
- $250k tier: Reached in ~month 2
- $500k tier: Reached in ~month 3
- $1M tier: Reached in ~month 4-5
- $5M tier: **Unreachable** with $5k capital (would need ~$170k/day volume)

**Realistic steady state: T5-T6 tier ($500k-$1M).** Getting to T7 ($5M+, 0.02% maker) requires ~$167k daily volume, which demands either ~$50k+ capital or leverage (not available on spot).

### 4.3 Positive Feedback Loop Model

```
Month 1: 0.25% maker → 80 bps grid → $150k vol → fees: $375
Month 2: 0.12% maker → 60 bps grid → $240k vol → fees: $288 (23% fee reduction)
Month 3: 0.08% maker → 45 bps grid → $360k vol → fees: $288 (same absolute, but more vol)
Month 4: 0.06% maker → 35 bps grid → $450k vol → fees: $270
Month 5: 0.04% maker → 28 bps grid → $540k vol → fees: $216

Total fees paid months 1-5: ~$1,437
Total profits months 1-5: ~$1,897
Net after fees: ~$460 profit (first 5 months)
```

This shows Month 1 is the most painful — almost all profit goes to fees. From Month 3 onward, the economics improve significantly.

### 4.4 KFEE Credits

$4,000 fee-free volume per 1,000 KFEE at maker rate. At base tier, 1,000 KFEE saves $10 (0.25% × $4,000). KFEE trades at roughly $0.02-0.05 on Kraken. So 1,000 KFEE costs ~$20-50 for $10 savings. **Not worth it at current pricing.** Only viable if KFEE price drops below $0.01.

### 4.5 Other Fee Acceleration Methods

- **Kraken Pro volume tiers are per-account, 30-day rolling.** No way to "game" this with multiple accounts (KYC-linked).
- **No maker rebates on BTC/USD.** Kraken does offer maker rebates on some lower-liquidity pairs, but BTC/USD is not one of them.
- **Institutional/OTC tier negotiation:** At $1M+ monthly volume, could contact Kraken for custom fee schedule. Worth exploring at Month 4-5.
- **FIX API access:** Kraken offers FIX API for institutional clients. FIX has guaranteed amend sequencing (unlike WS). Worth requesting at $1M+ tier.

---

## Section 5: Risk Management for Long-Only Spot

### 5.1 BTC Allocation Targets by Regime

| Regime | BTC Target | BTC Max | BTC Min | Grid Levels | Signal Engine |
|--------|-----------|---------|---------|-------------|--------------|
| Range-bound | 50% | 60% | 40% | 5 (full) | Enabled |
| Trending-up | 70% | 80% | 55% | 3 (asymmetric: more buys) | Enabled |
| Trending-down | 30% | 40% | 15% | 3 (asymmetric: more sells) | Enabled |
| Chaos/Black Swan | 0% | 5% (residual tax-locked) | 0% | 0 (cancelled) | Disabled |

Regime transitions use asymmetric grid adjustment, NOT market orders. Max single-tick rebalance: 10% of portfolio value. Tax veto may prevent sells, causing allocation to drift above target — this is accepted.

### 5.2 Flash Crash Scenario ($5k Capital)

Starting: $2,500 USD + $2,500 BTC (0.029 BTC at $85k). Grid: 5 buy levels at $500 each.

**10% crash ($85k → $76.5k):** All 5 buy levels fill. Total BTC: 0.029 + 0.033 = 0.062 BTC. Portfolio value: $4,743 + $37.50 = $4,780. Drawdown: -4.4%.

**20% crash ($85k → $68k):** Same fills (grid already exhausted at -4.7%). BTC at $68k: 0.062 × $68k = $4,216 + $37.50 = $4,253. Drawdown: -14.9%. Approaches chaos regime trigger.

**50% crash ($85k → $42.5k):** 0.062 × $42.5k = $2,635 + $37.50 = $2,672. Drawdown: -46.6%. Emergency override (>20%) would have fired, but Tax Agent may veto the sell → DUAL_LOCK → full pause.

**Max theoretical loss:** $4,962 (99.25% of capital) if BTC → $0. Mitigated by chaos regime trigger at -15% portfolio drawdown.

### 5.3 Idle USD Management

**USD must remain as fiat on Kraken.** Converting to stablecoins is a taxable event under German law. Kraken pays no interest on fiat. This is a known cost. The bot tracks `idle_usd_opportunity_cost` as a metric but does not act on it. Manual off-exchange sweep for interest is out of scope.

### 5.4 Drawdown Classification

| Portfolio DD from HWM | BTC Unrealized | Classification | Action |
|-----------------------|---------------|---------------|--------|
| ≤ 0% | Any | Healthy accumulation | Continue |
| 0-5% | Positive | Healthy accumulation | Continue |
| 0-5% | Negative | Warning | Telegram alert |
| 5-10% | Any | Warning | Reduce grid to 3 levels |
| 10-15% | Any | Problem | Regime → trending_down (30% BTC target) |
| ≥ 15% | Any | Critical | Regime → chaos (cancel all, full pause) |
| ≥ 20% | Any | Emergency | Tax override → force sell (if tax-locked) |

### 5.5 Tax-Lock Pause Behavior

When tax-lock prevents risk sells:
- **TAX_LOCK_ACTIVE**: Buys allowed, sells blocked, grid is buy-only
- **DUAL_LOCK** (tax + risk): All trading stopped — full idle
- **EMERGENCY_SELL** (>20% DD): Tax loses — sell regardless, record taxable event
- **Re-entry**: When tax-free lots mature OR drawdown recovers below 10% (5% hysteresis)

Pause duration: depends on age of oldest locked lot. Could be days (if lots are near 365d) or months (if recently purchased). The `days_until_next_free` metric is critical for user decision-making.

---

## Section 6: Tax Optimization Design

### 6.1 FIFO Ledger Data Model

Each purchase creates a `TaxLot` with fields:
- `lot_id` (UUID), `exchange_order_id`, `exchange_trade_id`
- `purchase_timestamp` (UTC), `quantity_btc`, `remaining_qty_btc`
- `purchase_price_usd`, `purchase_total_usd`, `purchase_fee_usd`
- `purchase_price_eur`, `purchase_total_eur` (converted at ECB daily reference rate)
- `exchange_rate_eur_usd`, `exchange_rate_source` ("ECB")
- `status` (OPEN / PARTIALLY_SOLD / CLOSED / TAX_FREE)
- `tax_free_date` (purchase_timestamp + 365 days)
- `source_engine` ("grid" / "signal"), `grid_level` (if applicable)

Each sale creates one or more `Disposal` records (FIFO order):
- `disposal_id`, `lot_id` (parent), `disposal_timestamp`
- `quantity_btc`, `sale_price_usd`, `sale_total_usd`, `sale_fee_usd`
- `sale_price_eur`, `sale_total_eur`, `exchange_rate_eur_usd`
- `gain_loss_eur` (computed: sale_total_eur - proportional purchase_total_eur)
- `is_taxable` (false if lot held > 365 days)
- `days_held_at_disposal`

**EUR/USD rate source**: ECB daily reference rate via `https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A`. Fetched daily, cached. Weekend/holiday trades use previous business day's rate.

### 6.2 VETO Mechanism

The Tax Agent evaluates every sell request with this priority:

1. **Emergency override** (portfolio DD > 20%): ALLOW regardless of tax status
2. **Tax-free lots available** (held > 365 days): ALLOW (sell these first under FIFO)
3. **Partial tax-free**: ALLOW_PARTIAL (sell only tax-free quantity)
4. **Within annual Freigrenze** (total taxable gains + this sale < €1,000): ALLOW
5. **Near-threshold lots** (330-365 days): VETO (protect for 35 more days)
6. **All lots locked**: VETO (enter TAX_LOCK_ACTIVE state)

**"Near-threshold" is configurable, default 330 days.** Lots 330-365 days old get heightened protection: never included in voluntary sells, Telegram alerts daily on approach.

### 6.3 Priority Hierarchy: Tax > Risk > Alpha

```
Sell Decision Flow:
  Alpha Engine → "I want to sell X BTC"
    → Risk Check: "Does this violate min allocation?"
      → If yes: BLOCK (risk veto)
      → If no: pass to Tax Agent
        → Tax Agent evaluate_sell():
          → ALLOW: execute sell
          → ALLOW_PARTIAL: execute reduced sell
          → VETO: enter TAX_LOCK (buy-only mode)
          → OVERRIDE_EMERGENCY: execute sell, log taxable event

Buy Decision Flow:
  Alpha Engine → "I want to buy X BTC"
    → Risk Check: "Does this violate max allocation?"
      → If yes: BLOCK or reduce to allowed quantity
      → If no: execute buy, create new TaxLot
    → Tax Agent: NOT consulted on buys (buys always allowed from tax perspective)
```

### 6.4 Pause State Machine

```
ACTIVE_TRADING ─── tax lock ──→ TAX_LOCK_ACTIVE (buy-only)
ACTIVE_TRADING ─── risk pause ─→ RISK_PAUSE (no trading)
TAX_LOCK_ACTIVE ── risk pause ─→ DUAL_LOCK (full stop)
TAX_LOCK_ACTIVE ── lots mature ─→ ACTIVE_TRADING
RISK_PAUSE ─────── recovery ───→ ACTIVE_TRADING
DUAL_LOCK ──────── lots mature ─→ RISK_PAUSE (sells OK, buys still paused)
DUAL_LOCK ──────── DD > 20% ───→ EMERGENCY_SELL (tax overridden)
DUAL_LOCK ──────── recovery ───→ TAX_LOCK_ACTIVE (buys resume)
```

### 6.5 Edge Cases

- **ALL BTC tax-locked**: Bot enters TAX_LOCK_ACTIVE. Grid runs buy-only (becomes a DCA-like accumulator). Signal engine sell signals suppressed. Waits for oldest lot to cross 365 days.
- **Max DD conflicts with tax-lock**: Tax wins until 20% DD. Between 15-20% DD: DUAL_LOCK (full idle). Above 20%: EMERGENCY_SELL overrides tax. User is Telegram-notified with exact tax cost of the forced sell.

### 6.6 German §23 EStG Reporting

Required fields per disposal for Anlage SO: `Art des Wirtschaftsguts` ("Bitcoin"), `Datum der Anschaffung`, `Datum der Veräußerung`, `Veräußerungspreis` (EUR), `Anschaffungskosten` (EUR), `Werbungskosten` (fees, EUR), `Gewinn/Verlust` (EUR), `Haltefrist überschritten` (bool). Supporting documentation must be retained 10 years (§147 AO): exchange name, trading pair, quantities, USD prices, EUR/USD exchange rates with source, all order/trade IDs, FIFO method confirmation per BMF 10.05.2022.

Annual summary generator: aggregates all disposals where `is_taxable = true` and `year = tax_year`. Computes total taxable gain, total taxable loss, net gain, and checks against €1,000 Freigrenze.

### 6.7 Quant Agent Integration

The Quant Agent receives a `sellable_inventory` metric every tick:
- `sellable_ratio` = tax_free_btc / total_btc
- When ratio ≥ 0.8: full sell-side grid
- When ratio 0.5-0.8: reduce sell levels to 60%
- When ratio 0.2-0.5: keep only 1 sell level
- When ratio < 0.2: buy-only grid (no sell levels)

### 6.8 Proactive Tax Planning

The Tax Planner scans the ledger daily with a 60-day lookahead:
- **30 days before maturity**: Telegram alert ("0.005 BTC becomes tax-free in 30 days")
- **14 days before**: Pre-position sell level in grid (activated only after maturity)
- **At maturity**: Lot marked TAX_FREE, sell levels activated, Quant Agent adjusts sell-side density upward
- **Smart timing**: If regime is trending-up at maturity, Quant Agent may recommend holding longer (tax-free, can sell anytime). If trending-down, sell immediately at maturity.

---

## Section 7: Open Validation Points

### Must Verify Before Phase A

1. **Stablecoin/FX volume exclusion**: Contact Kraken support or test empirically with a small USDC/USD trade to confirm it does NOT increment 30-day crypto volume counter.

2. **`amend_order` queue preservation**: Place a resting limit order, note L3 queue position, amend price, verify new queue position. Amend quantity down, verify priority preserved.

3. **`amend_order` on WS sequencing**: Verify behavior when two rapid amends are sent without waiting for acks. Does the second overwrite the first? Does it fail? Critical for rate-limited burst scenarios.

4. **Deadline reject rate**: Test with 500ms, 1000ms, 2000ms deadline values during normal and volatile conditions. Measure reject rate. Too tight = missed fills, too loose = adverse fills. Start with 1500ms, tune empirically.

5. **L2/L3 data quality**: Subscribe to L3 feed on BTC/USD during volatile session. Measure: message rate, latency, checksum validity. Compare to L2 for strategy usefulness.

6. **BTC/USDC vs BTC/USD liquidity**: Compare L2 book depth at 10 bps, 50 bps, 100 bps from mid. BTC/USD should dominate. If BTC/USDC is within 2x, it could serve as backup pair.

7. **Minimum order sizes**: Verify 0.0001 BTC minimum on XBT/USD. At $85k, that's $8.50. Grid levels of $500 = 0.006 BTC, well above minimum.

### Must Verify During Phase B

8. **Tax Agent FIFO accuracy**: Run 30 days, export FIFO ledger, manually verify against spreadsheet. Cross-check EUR/USD rates against ECB published rates. Non-negotiable.

9. **Grid profitability at base tier**: After 7 days, compute net P&L after ALL fees. If negative with no clear path to profitability at next tier, reconsider.

10. **Maker ratio**: Target >95%. If taker fills occur (race conditions, book sweeps), investigate and fix. Every taker fill at 0.40% vs 0.25% maker destroys 15 bps of edge.

11. **Adverse selection rate**: Measure how often grid fills are immediately followed by continued price movement in the same direction (fill → further drop for buys, fill → further rise for sells). If >60%, the grid is being adversely selected and spacing must widen.

---

## Section 8: Go/No-Go Recommendation

### The Honest Math

**Scenario A: Keep trading futures on Kraken Futures**
- Maker fee: 0.02% (existing tier) — 25x cheaper than spot base tier
- The €20k loss cap is abolished — no tax problem
- Flat 26.375% Abgeltungssteuer on net gains
- Your existing bot works NOW, 620+ tests, production-grade
- No engineering effort required

**Scenario B: Build spot bot**
- Maker fee: 0.25% at start, takes ~4-5 months to reach 0.04%
- §23 EStG tax advantage: 0% after 1 year (vs 26.375% on futures)
- First 5 months: ~$460 net profit on $5k capital (after fees)
- Steady state (month 6+): ~$500/month on $5k → ~$6,000/year
- Tax savings: If you accumulate 0.1 BTC/year at $85k = $8,500. Tax-free after 1 year saves ~$2,242 (26.375% of $8,500) vs futures taxation
- Engineering effort: 300-500 hours

**Break-even analysis:**
- Engineering opportunity cost: 400 hours × $50/hr (conservative) = $20,000
- Annual spot bot profit: ~$6,000 trading + ~$2,242 tax savings = ~$8,242
- Break-even on engineering effort: ~2.4 years
- At $25k capital: profit scales 5x → ~$41,210/year → break-even in 6 months

**Vs. simple DCA:**
- DCA $500/month into BTC, hold >1 year: same §23 tax benefit (0% after 1 year)
- No engineering effort. No operational risk. No fee drag.
- DCA Sharpe: ~0.3-0.5 (just BTC beta)
- Bot Sharpe: ~1.0-2.0 (grid alpha + tax optimization)
- Bot advantage over DCA: ~5-15% additional BTC accumulation efficiency per year through grid profits
- At $5k capital: 10% extra efficiency = $500/year. Does NOT justify 400 hours of work.
- At $50k capital: 10% extra efficiency = $5,000/year. Starting to make sense.

### Verdict: CONDITIONAL GO

**If your sole motivation was the €20k futures loss cap: STOP. The cap is abolished. Keep your futures bot.**

**If you want both futures AND spot exposure, or are building for long-term wealth accumulation with tax optimization:**

- **GO at $25k+ capital.** The tax savings alone (~$10k+/year) justify the effort. Grid profits are a bonus.
- **MARGINAL GO at $5k capital.** The absolute dollar edge over DCA is too small to justify the engineering effort on pure financial grounds. Only proceed if: (a) you value the engineering challenge, (b) you plan to scale capital within 1 year, or (c) you want both the futures bot running AND spot accumulation simultaneously.
- **NO-GO if you're choosing between futures and spot.** Futures at 0.02% maker crushes spot at 0.25% maker. The tax advantage of spot doesn't overcome the 12.5x fee disadvantage until you hold positions for 1+ year AND your gains are large enough to matter.

### Recommended Path

**Option 1 (Best of Both Worlds):** Keep the futures bot running for short-term alpha generation (high-frequency, low fees, now tax-efficient). Build a SIMPLIFIED spot grid bot (Phase A + B only, skip Phase C Signal Engine) purely for long-term BTC accumulation with §23 tax optimization. The spot bot is a "smart DCA" — not an alpha generator. Target: accumulate BTC at grid-implied discount, hold >1 year, sell tax-free.

**Option 2 (Pure Spot):** If you prefer to consolidate onto one system, proceed with full Phase A → B → C. Accept that months 1-3 will be painful at high fees. The strategy becomes viable at $500k+ monthly volume tier (month 3-4).

**Option 3 (Just DCA):** If capital is $5k and you're not scaling soon, set up a simple recurring buy on Kraken (€500/month), hold >1 year, sell tax-free. Save 400 hours of engineering time. Seriously consider this.

### Single Most Likely Failure Mode

**Persistent adverse selection at the base fee tier.** The grid buys BTC as it drops, but in a sustained downtrend, every fill is adversely selected. The 20 bps net edge per round-trip is too thin to absorb significant adverse selection. If BTC trends down 30% over 3 months, the grid accumulates BTC at progressively lower prices with no sells, and the 20 bps RT profit is dwarfed by the unrealized inventory loss.

**Detection:** After 14 days of trading, if adverse selection rate > 55% (fills followed by continued price movement) AND net P&L after fees is negative AND inventory drift exceeds configured bands: halt and re-evaluate grid spacing.

**Mitigation:** Flow toxicity filter (pause grid when toxicity > 0.8), tighter grid spacing (reduces individual fill size), faster regime transition to trending-down (reduce BTC target to 30%).

### Phase Timeline Estimate (if GO)

| Phase | Duration | Deliverables |
|-------|----------|-------------|
| A: Foundation | 6-8 weeks | WS manager, order manager, feeds, safety, fee model, reconciliation |
| B: Grid Trading | 4-6 weeks dev + 4 weeks live validation | Grid engine, delta skew, tax ledger MVP, KPI logging |
| C: Hybrid (optional) | 6-8 weeks dev + 4 weeks validation | Signal engine, regime router, full AI supervisor, full tax agent |
| **Total** | **4-6 months** | |

### Critical Files for Implementation

New files to create in `/home/user/iCryptoTrader/src/`:
- `ws/ws_public.py` — WS1 public feed connection manager
- `ws/ws_private.py` — WS2 private trading connection + executions channel
- `ws/ws_codec.py` — Kraken Spot WS v2 message codec (Cython candidate)
- `order/order_manager.py` — Amend-first order slot state machine
- `order/rate_limiter.py` — Per-pair rate counter with server sync
- `strategy/grid_engine.py` — Grid state machine with level tracking
- `strategy/signal_engine.py` — Momentum/breakout detector (Phase C)
- `strategy/regime_router.py` — Regime classification + capital allocation gates
- `risk/risk_manager.py` — Drawdown classifier, allocation enforcement, pause states
- `risk/delta_skew.py` — Target allocation deviation → quote asymmetry
- `tax/fifo_ledger.py` — FIFO lot tracking with EUR conversion
- `tax/tax_agent.py` — Veto mechanism, priority hierarchy, proactive planning
- `tax/tax_report.py` — §23 EStG annual report generator
- `tax/ecb_rates.py` — ECB daily EUR/USD rate fetcher
- `fee/fee_model.py` — Pair-aware, tier-aware fee service with `expected_net_edge_bps()`
- `inventory/inventory_arbiter.py` — Global inventory manager (Grid + Signal → NET orders)

Reused from HFT-Bot (copy + adapt):
- ZMQ IPC setup, telemetry pipeline, Telegram integration, systemd config, file lock
- EWMA volatility, flow toxicity scoring (as filters), Cython build infrastructure
- AI Supervisor agent scaffolding (adapt RCA/Quant/Macro agents)
- Backtest framework (adapt fill model for spot)

---

## Sources

- [MTR Legal — Futures Transactions Without Offset Restrictions](https://www.mtrlegal.com/en/futures-transactions-without-offset-restrictions/)
- [WINHELLER — Loss Offsets for Crypto Derivatives in Germany](https://www.winheller.com/en/banking-finance-and-insurance-law/bitcoin-trading/bitcoin-and-tax/loss-offsets.html)
- [FXFlat — Important Tax Changes Due to the 2024 Annual Tax Act](https://www.fxflat.com/en/service-help/news/article/important-tax-changes-due-to-the-2024-annual-tax-act)
- [Kraken — Spot Atomic Amends Guide](https://docs.kraken.com/api/docs/guides/spot-amends/)
- [Kraken — Atomic Amends Blog](https://docs.kraken.com/api/blog/atomic-amends/)
- [Kraken — Amend Order WS v2](https://docs.kraken.com/api/docs/websocket-v2/amend_order/)
- [Kraken — Edit Order WS v2](https://docs.kraken.com/api/docs/websocket-v2/edit_order/)
- [Kraken — Spot Trading Rate Limits](https://docs.kraken.com/api/docs/guides/spot-ratelimits/)
- [Kraken — Cancel After (DMS)](https://docs.kraken.com/api/docs/websocket-v2/cancel_after/)
- [Kraken — Fee Schedule](https://www.kraken.com/features/fee-schedule)
- [Koinly — Germany Crypto Tax Guide](https://koinly.io/guides/crypto-tax-germany/)
- [Blockpit — Germany Crypto Tax Guide](https://www.blockpit.io/tax-guides/crypto-tax-germany)
