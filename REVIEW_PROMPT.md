# Deep Code Review Prompt for iCryptoTrader

Copy everything below the line into Gemini (use a model with large context: Gemini 2.5 Pro).

---

## ROLE

You are an elite quantitative trading systems engineer with 15 years of experience building production crypto market-making bots, deep expertise in German tax law (§23 EStG), and a track record of finding subtle mathematical, financial, and architectural bugs that cause real money losses. You have reviewed the source code of Hummingbot, Freqtrade, CCXT-based bots, and proprietary HFT systems.

## TASK

Perform a brutally honest, comprehensive review of the iCryptoTrader codebase — a tax-optimized spot grid trading bot for Kraken BTC/USD, built in Python 3.11. The bot trades live with real money. Every bug you miss could cost the operator money. Every mathematical error could result in incorrect tax filings. Every missing feature could mean lost alpha.

## CODEBASE OVERVIEW

- **645 tests**, all passing. mypy strict clean. 80% line coverage.
- **Architecture**: Strategy Loop (100ms ticks) → Grid Engine → Order Manager → Kraken WS v2
- **Tax**: FIFO ledger with §23 EStG compliance (365-day Haltefrist, EUR 1,000 Freigrenze)
- **Risk**: 5-level drawdown classification, circuit breaker, pause state machine
- **Strategy**: Fee-aware grid + Bollinger adaptive spacing + AI signal bias + delta skew

## CORE FORMULAS & LOGIC TO SCRUTINIZE

### 1. Grid Level Pricing
```
buy_price[i] = mid_price × (1 - (i+1) × spacing_bps / 10_000)
sell_price[i] = mid_price × (1 + (i+1) × spacing_bps / 10_000)
order_qty = order_size_usd / level_price
```
**Questions**: Is multiplicative spacing correct vs additive? Does the grid skew the center? What happens when spacing × levels > 50%?

### 2. Bollinger + ATR Blended Spacing
```
SMA = mean(prices[-window:])
StdDev = sqrt(mean((price - SMA)² for each price))
BandWidth_bps = (upper - lower) / SMA × 10_000
ATR = mean(true_ranges[-atr_window:])
ATR_bps = ATR / SMA × 10_000

blended = (1 - atr_weight) × bb_spacing + atr_weight × atr_spacing
final = clamp(blended × spacing_scale, min_bps, max_bps)
```
**Questions**: Is population vs sample variance correct? Is ATR calculation using close-to-close or high-low-close? Should Bollinger width use band_width × scale or something nonlinear?

### 3. EWMA Volatility (Regime Router)
```
return = (price - last_price) / last_price
ewma_var = (1 - α) × ewma_var + α × return²
α = 2 / (span + 1)
ewma_vol = sqrt(ewma_var)
```
**Questions**: This is tick-level variance, not annualized. The momentum calculation uses `(newest - oldest) / oldest` over a deque. Is the EWMA initialization correct (starts from single return²)?

### 4. Delta Skew
```
deviation = btc_alloc_pct - target_pct
raw_skew_bps = deviation × 100 × sensitivity(2.0)
skew = clamp(raw_skew, -30, +30)

If over-allocated: widen buys (+skew), tighten sells (-skew)
If under-allocated: tighten buys (-skew), widen sells (+skew)

buy_spacing = max(1, base_spacing + buy_offset)
sell_spacing = max(1, base_spacing + sell_offset)
```
**Questions**: Is sensitivity=2.0 correct for a spot grid? Should skew be proportional to deviation or use a convex function? Is ±30 bps cap too aggressive or too conservative?

### 5. FIFO Cost Basis (Tax)
```
For each lot consumed during FIFO sell:
  sell_portion = min(remaining_lot_qty, qty_to_sell)
  cost_proportion = sell_portion / lot.original_qty
  cost_basis_eur = cost_proportion × lot.purchase_total_eur
  proceeds_eur = (sell_portion × sale_price - proportional_fee) / eur_usd_rate
  gain_loss = proceeds_eur - cost_basis_eur
```
**Questions**: Is proportional fee allocation correct for partial fills? Is the EUR conversion applied correctly (should it use the rate at time of sale, not purchase)? Does the Freigrenze logic correctly handle the "all-or-nothing" threshold (EUR 999.99 = tax-free, EUR 1,000.01 = fully taxable)?

### 6. Fee Model & Profitability Gate
```
min_spacing_bps = 2 × maker_fee_bps + adverse_selection(10bps) + min_edge(5bps)
net_edge = spacing - round_trip_cost - adverse_selection
```
**Questions**: Is 10 bps adverse selection realistic for BTC spot? Should adverse selection scale with volatility? Is the fee model using maker-maker or maker-taker for the round-trip?

### 7. Auto-Compounding
```
scale = portfolio_value / compound_base_usd
order_size = base_order_size × scale × regime_size_scale
```
**Questions**: Does this create a positive feedback loop during drawdowns (smaller orders → less recovery)? Should compounding have a floor?

### 8. Risk Manager Drawdown
```
drawdown_pct = (HWM - current_portfolio) / HWM
HWM updates on new highs only
Trailing stop: tighten max_dd from 15% toward 7.5% as portfolio grows
```
**Questions**: Is HWM reset on deposit/withdrawal? Does the trailing stop create false pauses during normal pullbacks after a strong rally?

### 9. Circuit Breaker
```
velocity = |current_price - price_60s_ago| / price_60s_ago
If velocity >= 3%: freeze for 30s
Hysteresis: only unfreeze if velocity < 1.5%
```
**Questions**: Is 60s window appropriate for crypto? Should there be multiple timeframes (5s, 60s, 300s)? Does the circuit breaker fire on both up and down moves (could miss buying opportunities during a flash rally)?

### 10. Tax-Loss Harvesting
```
For each underwater lot where days_held < 330:
  If YTD_gains > 0 AND loss > min_loss(EUR 50):
    Recommend sell to offset gains
    Stop when net_taxable < target(EUR 800)
```
**Questions**: Does harvesting account for the wash-sale equivalent in German law (Gestaltungsmissbrauch)? Is the 330-day near-threshold correct (should it be configurable)? Does harvesting interact correctly with the Freigrenze all-or-nothing rule?

### 11. Inventory Arbiter
```
max_buy_btc = (max_alloc_pct - current_alloc_pct) × portfolio_value / price
Capped at 10% portfolio rebalance per tick
```
**Questions**: Is 10% per-tick rebalance cap appropriate at 100ms tick rate (could move 100% in 1 second)? Should the cap scale with the tick interval?

### 12. Order Manager Amend-First Logic
```
If LIVE and price/qty changed:
  If side changed → Cancel (can't amend side on Kraken)
  Else → Amend (preserves queue priority)
Price epsilon: $0.01, Qty epsilon: 0.00000001 BTC
```
**Questions**: Does amending preserve Kraken's time-priority in the order book? What happens if the amend is rejected — does the bot retry or get stuck? Are the epsilon values appropriate?

## WHAT TO REVIEW

### A. Mathematical & Financial Correctness
1. **Grid pricing**: Is multiplicative spacing mathematically optimal? Compare against additive and logarithmic spacing.
2. **Bollinger calculation**: Population vs sample variance. Window management. ATR integration correctness.
3. **EWMA volatility**: Initialization bias. Tick-level vs time-scaled variance. Comparison with realized volatility.
4. **Fee model**: Is 10 bps adverse selection a realistic constant? Should it be dynamic?
5. **Cost basis**: EUR conversion timing. Fee allocation for partial fills. Rounding errors in Decimal arithmetic.
6. **Drawdown calculation**: HWM behavior with deposits. Trailing stop calibration.
7. **Compounding**: Positive feedback loops. Minimum order size enforcement.
8. **Skew**: Sensitivity parameter calibration. Convexity of the skew function.

### B. Trading Best Practices (compare against Hummingbot, Freqtrade, professional market makers)
1. **Inventory risk**: Is delta skew sufficient or should the bot use Avellaneda-Stoikov optimal market-making?
2. **Spread management**: How does the bot handle spread widening during low liquidity? What about during exchange maintenance?
3. **Order book defense**: Does the bot react to large orders (icebergs) approaching its levels?
4. **Fill probability**: Are grid levels placed at round-number magnets ($85,000, $84,500)? Should they avoid these?
5. **Latency**: 100ms tick interval — is this too slow for grid trading? What if multiple fills happen within one tick?
6. **Slippage**: Does the bot account for slippage on market orders (e.g., during emergency sell)?
7. **Exchange-specific**: Is the bot handling Kraken's unique behaviors (order ID format, rate limiting nuances, post-only rejections)?
8. **Position sizing**: Kelly criterion or fixed-fraction? Does the sizing adapt to edge quality?
9. **P&L tracking**: Are unrealized P&L and realized P&L tracked separately? Is the P&L per-strategy or portfolio-level?

### C. Architecture & Reliability
1. **Single point of failure**: What happens if the ZMQ connection between Feed Process and Strategy Process drops?
2. **Clock drift**: Does the bot use monotonic clocks everywhere? What about ECB rate timestamps?
3. **Concurrency**: Any race conditions between WS callbacks and the tick loop?
4. **Memory leaks**: Unbounded deques or growing data structures?
5. **Error recovery**: What happens after a ledger mismatch (FIFO sell fails)?
6. **Idempotency**: Can the bot recover from replaying the same fill twice?
7. **Config hot-reload**: Can parameters be changed without restart?

### D. German Tax Compliance (§23 EStG, BMF Circular 10.05.2022)
1. **FIFO enforcement**: Is FIFO truly enforced when multiple fills happen in the same tick?
2. **Haltefrist precision**: Does the 365-day calculation use calendar days or trading days? Timezone handling?
3. **Freigrenze**: Is the EUR 1,000 threshold implemented as Freigrenze (all-or-nothing) or Freibetrag (deduction)? These are legally different.
4. **EUR conversion**: Is the ECB daily reference rate legally sufficient, or does the BMF require the rate at the exact time of transaction?
5. **Wash sale**: Does German law have wash-sale rules that affect the tax-loss harvesting?
6. **Staking/lending**: If the bot's BTC is staked, the Haltefrist extends to 10 years — is this tracked?
7. **Reporting**: Does the Anlage SO format match current Finanzamt requirements?

### E. Missing Features (compared to best-in-class bots)
1. What features do Hummingbot and Freqtrade have that this bot lacks?
2. What would a professional market-making desk add?
3. What ML/AI features beyond simple LLM signals would add alpha?
4. What monitoring and alerting is missing for production operations?
5. What backtesting capabilities are missing?

### F. Agentic AI Usage Proposals
1. How could an AI agent autonomously tune grid parameters (spacing, levels, sizing) based on market conditions?
2. Could an agent manage the tax-loss harvesting decisions more intelligently?
3. How could agents handle exchange-specific operational tasks (withdrawal management, API key rotation, rate limit optimization)?
4. What would an agentic system look like that manages multiple bots across multiple exchanges?
5. How could reinforcement learning replace the hardcoded regime thresholds?
6. Could an agent perform continuous backtesting and A/B testing of parameter changes?

## OUTPUT FORMAT

Structure your review as:

### 1. CRITICAL BUGS (would cause money loss or incorrect taxes)
- Bug description, file, line reference, impact, fix

### 2. MATHEMATICAL CONCERNS (may affect profitability)
- Formula, issue, suggested correction, quantified impact

### 3. TRADING BEST PRACTICES VIOLATIONS
- Practice, current implementation, what best bots do, priority

### 4. TAX COMPLIANCE RISKS
- Rule, current implementation, legal risk, fix

### 5. MISSING FEATURES (ranked by expected impact on profitability)
- Feature, why it matters, implementation complexity, expected impact

### 6. AGENTIC AI PROPOSALS (ranked by feasibility and impact)
- Proposal, architecture, expected benefit, implementation effort

### 7. OVERALL ASSESSMENT
- Honest rating out of 10 for: Code quality, Mathematical correctness, Trading sophistication, Tax compliance, Production readiness
- Top 3 things to fix immediately
- Top 3 features to add next

Be specific. Reference formulas, constants, and logic. Don't say "looks good" — find the problems. If something is genuinely well-done, say so briefly and move on. Spend your analysis budget on finding issues, not praising correct code.
