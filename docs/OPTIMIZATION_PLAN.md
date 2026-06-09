# Profit Generation — Optimization Plan

> **Generated:** 2026-06-02  
> **Primary input:** `docs/SYSTEM_DATA_ANALYSIS.md` (quantitative analysis from live DB + backtest CSVs)  
> **Secondary inputs:** `config/settings.json`, Phase 6 / Phase 7 plans, options / futures / crypto research docs  
> **Constraint:** planning only — no code, DB, or config modified in this document

---

## Executive Summary

Five highest-leverage moves, in priority order:

1. **Instrument MFE/MAE/exit_reason NOW — it gates every downstream optimization.**  
   `mfe_pct`, `mae_pct`, and `exit_reason` are 100% NULL in `outcomes` (1,853 rows). Losers run a median 11 bars vs 5 for winners, with 165 trades worse than −10% and a worst-case of −31.4%. We cannot confirm whether stops are misfiring, whether trailing is cutting winners short, or whether any stop-parameter change helps or hurts — until the columns are populated. Every other stop/exit/pyramid improvement is unmeasurable guesswork without this.

2. **Kill the negative-edge intraday strategies; resolve the config contradiction.**  
   `intraday-1m-orb` is −$55.91 at 22% WR live; ORB backtest is uniformly negative (orbo-bidir −12.8%, orbo-long −12.9%). `intraday-1m-vwap-reclaim` is −$7.39 at 39% WR live. These strategies consume open-position slots, capital, and monitoring cycles with no demonstrated edge in backtest or live. Additionally, `skip_intraday_signals=true` and `intraday_enabled=true` coexist in `config/settings.json` — a config contradiction whose resolution changes intraday routing entirely; resolve before tuning anything intraday.

3. **Raise the price cap or replace it with a notional gate — stop vetoing the best liquid symbols.**  
   A flat `$250/share` cap issued 6,338 `price_too_high` skips in one week (5,139 on `intraday-1m-momentum` alone). The vetoed names — SPY, QQQ, NVDA, AVGO, SMH, IWM — are the tightest-spread, highest-liquidity vehicles in existence. The system already has `max_position_usd`; replacing the per-share cap with a notional gate sizes these names correctly rather than excluding them.

4. **Deploy idle capital by decoupling proven-winner sizing from the broken intraday eligibility gate.**  
   The system is 70% idle: $69.5k cash, median filled position $914 against a $10k cap. Root cause is compounded: `kelly_quarter × fraction_of_kelly=0.25 × max_position_fraction=0.10` stacks three multipliers that crush size, and intraday strategies are permanently in grace-period half-size because `min_outcomes=30` is never met (zero intraday closed outcomes). The core EOD botnet101 strategies have n=390/361/564 closed outcomes — they should be receiving scaled capital, not the same grace-period treatment as strategies with n=0.

5. **Kill `botnet101-consec-bearish` and fix or quarantine `botnet101-buy-5day-low`.**  
   `botnet101-consec-bearish` loses money: PF 0.95, −0.06% expectancy, n=168. It is consuming capital allocation that should be redistributed to `botnet101-3-bar-low` (PF 2.92, +2.18%/trade) and `botnet101-4bar-momentum-reversal` (PF 3.31, +2.10%/trade). `botnet101-buy-5day-low` has payoff 0.57 — losers are 1.7× bigger than winners — marginal despite a 69% WR. Both dilute the edge concentration without contributing expectancy.

---

## Prioritized Roadmap Table

| Priority | Initiative | Expected Impact | Effort | Risk | Evidence |
|---|---|---|---|---|---|
| **P0** | Instrument `mfe_pct`, `mae_pct`, `exit_reason`, intraday closed outcomes in `outcomes` table | Unlocks all stop/trailing/pyramid optimization; currently those are unmeasurable | S | Low — observation only, no live behavior change | 100% NULL on 1,853 rows; 0 intraday closed outcomes in table |
| **P0** | Resolve `skip_intraday_signals` vs `intraday_enabled` config contradiction | Eliminates ambiguous routing; required before any intraday change | S | Low — config-only | Both flags set simultaneously in `config/settings.json` |
| **P0** | Deactivate `intraday-1m-orb` and `intraday-1m-vwap-reclaim` | Stops active bleeding; frees position slots | S | Low — removing negative-edge strategies | ORB live: −$55.91 at 22% WR (n=9); vwap-reclaim: −$7.39 at 39% WR (n=18); ORB backtest: −12.9% vs SPY +22.6% |
| **P0** | Deactivate `botnet101-consec-bearish` | Removes money-losing strategy from capital pool | S | Low — clear negative edge at n=168 | PF 0.95, −0.06% expectancy, n=168 |
| **P1** | Replace `$250/share` price cap with notional gate using existing `max_position_usd` | Restores signal flow on SPY/QQQ/NVDA/AVGO/SMH/IWM; net more high-quality entries | S | Low (caveat: only matters once intraday strategies have positive edge) | 6,338 `price_too_high` skips in one week; 5,139 on `intraday-1m-momentum` alone |
| **P1** | Raise Kelly fraction on proven EOD winners (3-bar-low, 4bar-reversal, consec-below-EMA) to deploy idle capital | Capital utilization from 30.6% toward 60-70%; proportionally more profit from the strategies that are working | M | Medium — larger positions increase drawdown exposure | $69.5k idle; median fill $914 vs $10k cap; n=390/361/564 for the three strategies |
| **P1** | Flag `botnet101-buy-5day-low` for quarantine or parameter rework | Removes losers-1.7× payoff drag; capital redistributed to higher-PF strategies | S | Low — marginal edge, not a core contributor | PF 1.28, payoff 0.57, +0.33% expectancy at n=243 |
| **P1** | Flag trend strategies for quarantine until edge is demonstrated | Avoids compounding all-loss runs; zero wins at n=26 (donchian) and n=4 (ma-cross) | S | Low — thin sample but uniformly bad | donchian: 0% WR, −8.0%/trade, n=26; ma-cross: 0% WR, −4.87%, n=4 |
| **P2** | Instrument and backtest `rsi2-oversold` + `inside-day-breakout` + `bollinger-bandit` on the proven ETF universe (GDX/XBI/XME/QQQ/IWM) before any live capital | Validates whether coded-but-untested strategies extend the EOD edge; no live risk | M | Low — paper / backtest only | These 4 strategies are coded with `compute_*` functions but have zero closed outcomes; RSI2 is a known mean-reversion variant directly adjacent to the proven edge |
| **P2** | Build out gap-fill mean-reversion strategy for the EOD universe | Most natural extension of the proven edge; overnight gap reversal on sector ETFs directly adjacent to botnet101 patterns | M | Medium — new strategy needs validation runway | ETF universe is 100% net-positive at n≥20; gap-fill is a categorical gap (not present at all) |
| **P2** | Enable pyramiding on `botnet101-3-bar-low` and `botnet101-4bar-momentum-reversal` once MFE/MAE is instrumented | Both are `pyramidable` but `pyramid_tier` is NULL on all 196 paper_trades; dormant feature with proven edge underneath | M | Medium — requires instrumentation first to avoid blind pyramiding | `pyramid_tier` 100% NULL; 11 `pyramid_not_pyramidable` skips logged |
| **P2** | Tighten initial stop from ATR 2.0× toward 1.5× ATR for mean-reversion strategies (test on paper first) | Expected to truncate the −10%+ loss tail (165 trades); potential payoff ratio improvement | M | Medium — cannot confirm without MFE/MAE; must instrument first | Losers run median 11 bars vs 5 for winners; payoff 0.95; 165 trades worse than −10%; worst −31.4% |
| **P2** | Activate `donchian-retest-short` on the EOD universe (currently n=0 live/closed) | Adds short-side hedge to a currently long-only book; `allow_shorts=false` gate must be evaluated | M | Medium — shorts have asymmetric loss exposure; `allow_shorts=false` in `config/settings.json` must be consciously overridden | Short side is a categorical gap; strategy exists but dormant |
| **P3** | Expand EOD ETF universe with 3-5 additional sector ETFs having confirmed mean-reversion properties (e.g., XLF, XLV, XLRE, XRT) | More entry opportunities for the proven edge; modest capital utilization increase | M | Low — more symbols of the same proven profile | All 9 current n≥20 symbols are net-positive; universe is likely too narrow for capital being deployed |
| **P3** | Options pyramiding — defer until trend strategies have ≥50 closed outcomes AND ≥5% of those exceed +50% return | No current evidence that trend strategies produce the asymmetric tail that justifies options infrastructure cost | L | Low (deferred) | 6.5.1 verdict: NO-GO; trend strategies have 0 closed outcomes as of 2026-05-21; 9 engineering days of infrastructure cost |
| **P3** | Futures / crypto leverage — defer indefinitely per existing research | No evidence advantage over current equity setup; data cost $120-150/mo before justified | L | Low (deferred) | FUTURES_RESEARCH.md and CRYPTO_LEVERAGE_RESEARCH.md both NO-GO |

---

## Per-Question Recommendations

### 1. Should we build out data / instrumentation?

**Verdict: DO — immediately, before any other optimization.**

The `outcomes` table has 1,853 closed rows and every single `mfe_pct`, `mae_pct`, and `pyramid_tier` column is NULL. `exit_reason` is uniformly `long_exit_signal` on all 1,853 rows — either stops never fired (unlikely given config defining ATR 2.0–2.5× stops and 3.0× trailing) or the exit-reason field is simply not being written by the live stop/trailing engine. Live `paper_trades` confirms stops ARE attached at entry (`entry_stops='atr_initial'` on 61 fills), which means the triggering events are invisible.

The consequence is concrete: with payoff ratio 0.95 (avg win +3.69% barely exceeds avg loss −3.88%) and losers holding a median 11 bars vs 5 for winners, there is a strong inference that stops are firing late or not at all on losers. But "inference" is not "measurement." A tighter stop (e.g., 1.5× vs 2.0× ATR for mean-reversion) is the intuitive fix — but without MFE data, we cannot rule out that a tighter stop also clips winners before they recover. This is unresolvable without instrumentation.

**What exactly to capture:**
- `mfe_pct` — maximum favorable excursion from entry to the highest intraday price before exit, expressed as a percentage of entry. Measures how far a winner extended before being closed or reversed. Needed to: (a) evaluate trailing stop tightness (are we giving back too much from peak?), (b) confirm pyramid trigger quality (does price reach a meaningful new high before we add?).
- `mae_pct` — maximum adverse excursion from entry to the lowest intraday price, expressed as a percentage. Measures how deep a trade went against us. Needed to: (a) confirm whether the ATR 2.0× stop is binding at all, (b) evaluate whether tighter stops would have preserved capital without triggering prematurely on noise.
- `exit_reason` — one of: `signal_exit`, `atr_stop`, `trailing_stop`, `sar_overlay`, `eod_close`, `manual`. Currently 100% `long_exit_signal` — completely uninformative. Populating this identifies which exits are stop-driven vs signal-driven, enabling attribution of loss magnitude to stop failures.
- Intraday closed outcomes — there are ZERO intraday rows in `outcomes`. The intraday strategies (1m-orb, 1m-momentum, vwap-reclaim) have no resolved P&L in the outcomes table, only in raw `paper_trades` FIFO round-trips. This means the entire intraday strategy evaluation pipeline — Kelly eligibility gate, Sharpe computation, the `min_outcomes=30` graduation gate — receives zero intraday data and can never graduate intraday strategies out of grace period. Fix: write intraday closed trades to `outcomes` with the same schema, tagging `bar_interval` correctly.

**Why this gates every other improvement:** stop tightening, trailing parameter changes, pyramid activation, and strategy deactivation decisions all benefit from MFE/MAE evidence. Running any of those changes blind (on 15 days of live data with no exit attribution) risks making the wrong directional bet with no way to confirm improvement.

---

### 2. Should we run more strategies?

**Verdict: NO on "more strategies" — DO on targeted cuts and category-adjacent extensions.**

The corpus has 18 scraped records and 14 are already implemented. Adding more strategies is not the bottleneck. The issue is edge concentration vs edge dilution.

**Cut immediately (losing money):**
- `botnet101-consec-bearish` — deactivate. PF 0.95, −0.06% expectancy, n=168. Statistically large enough sample to confirm this is a negative-edge strategy. Capital freed here flows toward the profitable strategies.
- `intraday-1m-orb` — deactivate or quarantine. Live: −$55.91 at 22% WR (n=9). Backtest: orbo-bidir −12.8%, orbo-long −12.9% vs SPY +22.6% buy-and-hold over the same period. Negative in both contexts.
- `intraday-1m-vwap-reclaim` — quarantine. Live: −$7.39 at 39% WR (n=18). Sample is small but direction is consistent with the broader intraday-ORB failure mode.

**Quarantine for rework (marginal or thin):**
- `botnet101-buy-5day-low` — quarantine. Payoff 0.57 means losers average 1.7× the size of winners. Despite a 69% WR, expectancy is only +0.33%. The strategy needs a parameter rework (tighter exit on losers, or a different entry filter) before it earns capital.
- `trend-donchian-breakout-20` and `trend-ma-cross-20-50` — quarantine. 0% WR at n=26 and n=4 respectively. Sample is thin (don't kill permanently) but don't scale capital into all-loss strategies. Watch: if these eventually produce closed outcomes with positive returns, re-evaluate. Until then, no new capital.

**Validate before scaling (coded, untested):**
- `rsi2-oversold`, `inside-day-breakout`, `bollinger-bandit`, `rsi14` — these have compute functions but zero closed outcomes. Run backtests and paper-trade them on the proven ETF universe (GDX/XBI/XME/QQQ/IWM/XLE) before committing live capital. RSI2 in particular is well-documented in the academic mean-reversion literature and directly adjacent to the proven botnet101 patterns — it is the highest-probability extension of the working edge.

**Add by category (not by count):**
- Gap-fill mean-reversion — overnight gap reversal on sector ETFs is the most natural extension of the EOD botnet101 edge. The same symbols, the same hold duration, a complementary entry trigger. Not yet present in the system.
- Short side — `donchian-retest-short` exists but has n=0 live. `allow_shorts=false` in `config/settings.json` is the gate. Before enabling: confirm the ATR stops and position-sizing logic handle short direction correctly; the existing config defines the stop-mirror correctly (`entry_price + (k × ATR)` for shorts) but has never been tested with real fills.

**Do NOT add:**
- More ORB variants, gap-and-go momentum (`momentum_drift` was explicitly tested at −3.16% mean, 32% WR, abandoned), or any additional intraday breakout strategies until the existing intraday infrastructure has at least one profitable strategy with closed outcomes.

---

### 3. Should we increase the number of symbols monitored?

**Verdict: DO — but fix the price cap first, then modestly expand EOD universe; intraday expansion is secondary.**

**For EOD strategies:**  
The current 37-symbol EOD universe is well-targeted and 100% net-positive at n≥20. The system's signal density on the top 9 sector ETFs is appropriate. However, the universe is arguably too narrow for the capital being deployed — with $100k in the account and only 9 primary symbols generating signals, there are days with very few qualifying setups. Extending to 3-5 additional sector ETFs with confirmed mean-reversion properties (candidates: XLF, XLV, XLRE, XRT, SMH for the ETF version) adds setup frequency without changing strategy logic. Validate each on backtest against the botnet101 strategies before adding — not every sector ETF mean-reverts reliably.

**For intraday strategies:**  
The `price_too_high` cap ($250/share) vetoed 6,338 entries in one week, 5,139 on `intraday-1m-momentum` alone. The vetoed names (SPY at $560+, QQQ at $470+, NVDA at $900+, AVGO at $1,700+, SMH at $230+) are the tightest-spread, most-liquid intraday vehicles available. Replacing the per-share cap with the existing `max_position_usd` logic — sizing position qty by `floor(max_position_usd / share_price)` rather than rejecting the signal entirely — corrects this without any change to risk exposure.

**Critical caveat:** fixing the price cap only helps once the underlying intraday strategies have positive edge. Currently the strategies whose entries are being blocked (1m-orb, 1m-momentum, vwap-reclaim) are net-negative in live trading. Fix the strategies before restoring their signal flow on the best symbols — otherwise the price cap change just accelerates losses. Sequence: (1) kill losing intraday strategies, (2) fix price cap, (3) evaluate intraday-1m-momentum on its own terms once ORB noise is removed.

**Universe dilution risk:**  
The EOD symbol concentration in sector ETFs is a feature, not a bug. Single-name equities gap on earnings, news, and idiosyncratic events that the mean-reversion model doesn't account for. Resist the temptation to expand into individual stocks. The 37-symbol EOD universe proves the ETF thesis: every n≥20 symbol is net-positive. If a symbol candidate doesn't fit the ETF / broad-sector profile, do not add it.

---

### 4. Should we improve pattern detection for stop-loss / trailing / pyramid on intraday strategies?

**Verdict: DO NOT YET — instrument first, then make a data-driven decision on intraday viability.**

The intraday viability question has a direct answer from the data: **the current intraday strategies do not have positive edge and should not be invested in for stop/trailing optimization until that changes.**

Specifically:
- The ORB strategies are the worst case: negative in backtest (both orbo-bidir and orbo-long underperform SPY buy-and-hold by 30%+ over the same period) AND negative live. This is not a stop-calibration problem; it is a signal-quality problem.
- VWAP-reclaim is negative live. The 15-day sample is thin for a definitive verdict, but the direction is consistent with the broader intraday-ORB failure mode and with the academic evidence on intraday mean-reversion strategies in the current regime.
- `intraday-1m-momentum` is the only intraday strategy with a marginally positive live record: +$11.83 at 54% WR across 24 round-trips. This is the only candidate worth preserving, but 24 trips is too thin for a confidence verdict. Keep it in observe mode after the ORB strategies are removed, then re-evaluate at n=50.

**On stop/trailing for intraday specifically:**  
The analysis cannot compute intraday exit quality at all — zero intraday closed outcomes exist in `outcomes`. There are literally no MAE or MFE numbers to work from. Investing engineering time in tighter intraday stops or intraday trailing mechanics before (a) fixing MFE/MAE instrumentation and (b) having a positive-edge intraday strategy to test them on is a guaranteed waste.

**On intraday pyramiding:**  
`pyramid_tier` is NULL on all 196 paper_trades. Pyramiding is built but never activated. Do not activate intraday pyramiding on negative-edge strategies. The only correct sequencing is: kill bad strategies → instrument → confirm positive edge at n≥50 → then enable pyramiding with measured add-on triggers.

**EOD stop optimization (different answer):**  
For the EOD strategies, stop/trailing optimization IS worth doing — but only after instrumentation. The inferred picture (losers run 2× as long as winners, payoff ratio 0.95, 165 trades worse than −10%) strongly suggests the ATR 2.0× stop for mean-reversion is too loose or misfiring. The expected fix (tighten to 1.5× ATR, capture more of the losers earlier) could materially improve the payoff ratio from 0.95 toward 1.2+, which at the current 65.5% win rate would lift expectancy from +1.33% to +1.8%+. But confirm with MFE/MAE data before changing stop parameters.

---

### 5. Sizing / capital efficiency

**Verdict: DO — multiple levers need pulling simultaneously.**

The system is 70% idle and sizes median fills at $914 against a $10,000 cap. This is the most direct lever on realized P&L. Understanding the compounding causes:

**Cause 1 — Kelly compounding.** `kelly_quarter (sizing_method)` × `fraction_of_kelly=0.25` × `max_position_fraction=0.10` means even a high-edge strategy (say Kelly fraction = 0.3) produces: `0.3 × 0.25 × 0.10 = 0.0075` — 0.75% of portfolio, or $750. That's consistent with the observed $914 median. This compound isn't necessarily wrong (fractional Kelly is conservative by design), but the `max_position_fraction=0.10` combined with `fraction_of_kelly=0.25` simultaneously is double-capping. Consider removing one of these redundant caps or raising `max_position_fraction` to 0.15 for strategies above PF 2.0.

**Cause 2 — Grace-period permanent lock.** Intraday strategies are permanently at `grace_period_size_multiplier=0.5` (half size) because `min_outcomes=30` requires 30 closed outcomes, and there are zero intraday closed outcomes in `outcomes`. The eligibility gate never clears. This is a data-pipeline problem, not a sizing problem — fix the intraday outcome recording (P0 above) and the gate will clear for strategies that accumulate closed trades.

**Cause 3 — No edge weighting.** `botnet101-3-bar-low` (PF 2.92, +2.18%/trade) is sized identically to `botnet101-consec-bearish` (PF 0.95, −0.06%/trade). The `tiered` config in `auto_trade` defines `tier_3_min_sharpe=0.3` as the top tier, but no intraday strategy ever reaches it (again: no closed outcomes). For the proven EOD strategies, a simple edge-tier assignment based on PF would let the top-PF strategies (3-bar-low, 4bar-reversal, turn-of-month at PF 8.73) receive proportionally larger allocation.

**Recommended sizing actions:**
1. Raise `max_position_fraction` from `0.10` to `0.15` for strategies with PF > 2.0 (3-bar-low, 4bar-reversal, consec-below-EMA, turn-of-month). Keep the `fraction_of_kelly=0.25` conservative cap in place — don't change both simultaneously.
2. Fix intraday outcome recording so `min_outcomes=30` clears naturally as intraday trades close.
3. After removing losing strategies (consec-bearish, ORB), the freed position slots and capital should flow toward the top-PF EOD strategies automatically through the existing Kelly machinery — confirm this is happening by checking deployed capital 1 week post-deactivation.
4. Do NOT raise `fraction_of_kelly` beyond 0.25 until MFE/MAE instrumentation is live and stop effectiveness is confirmed. The conservative Kelly posture is correct given that we cannot yet measure whether losers are being contained.

**Kelly min_samples issue:** `kelly.min_samples=50` in config, but the Kelly calculator code requires 50 closed outcomes before computing a Kelly fraction. The three top EOD strategies have n=390/361/564 — well above this threshold. They SHOULD be receiving Kelly-computed sizes, not the tiered fallback. Confirm whether the `min_samples=50` gate is actually clearing for these strategies in live execution — if the Kelly path is falling through to the tiered fallback for some configuration reason, fixing that alone could materially increase position sizes for the proven strategies.

---

### 6. Other high-leverage improvements

**Regime gating:**  
`stops.regime_aware=false` in the current config. The regime-aware stop multiplier (6.1.3) is built but dormant — regime multipliers are defined (`choppy=1.25`, `trending_down=1.10`, `low_vol=0.85`) but the `regime_aware=false` flag prevents them from activating. This is a one-line config change that wires in regime-aware stop widening in choppy markets and tightening in low-vol markets. Given the payoff-ratio problem, this is a reasonable activation candidate once MFE/MAE is instrumented. **However:** activate only after confirming the underlying stops are working correctly — regime-aware multipliers on broken stops would multiply the problem.

**Execution / slippage:**  
The live track record ($100k → $100.163k, +0.163% over 15 days) is too thin to measure slippage meaningfully. Slippage for sector ETFs is typically 1-3 cents per share on market orders, which at the current median fill of $914 / ~10 shares represents 0.1-0.3% per round-trip. At scale (if median position reaches $5k-8k), slippage matters more. Track `fill_price` vs `signal_price` per trade in a dedicated `slippage_audit` table once daily volume increases.

**Overfitting risk:**  
15 days of live data is far too thin for any parameter optimization. Every number in the live section of the analysis is directional signal, not a measured truth. The backtest data (1,853 trades over 2 years) is on firmer ground but is in-sample — the validated strategy parameters should be treated as correct until the live track record provides meaningful counter-evidence (suggest minimum 90 days / 300+ live closed outcomes before re-fitting any strategy parameter). The two things that ARE reliable enough to act on: (a) the loss-making strategies (consec-bearish at n=168, ORB backtest at 3 independent negative backtests) and (b) the instrumentation gaps (these are facts about the data pipeline, not about market behavior).

**LLM filter (Phase 7.1):**  
The LLM signal filter is built and in shadow mode. The 30-day evaluation clock started (per Phase 7 plan, shadow data accumulating since ~2026-05-22). This is running correctly — don't intervene. The graduation decision (flip `llm_filter_live: true`) happens at ~2026-06-21 if the A/B shows positive PnL delta with min 200 outcomes and Sharpe delta ≥ 0.2. Current shadow data is too thin for an interim verdict.

**Options / Futures / Crypto leverage:**  
- **Options:** NO-GO per 6.5.1 verdict. Trend strategies have 0 closed outcomes; the asymmetric-payoff prerequisite is unmet. Revisit 2026-08-21.
- **Futures:** NO-GO per Phase 3.4.3 research. Requires 90 days clean equity operation + $120-150/mo data subscription justified by demonstrated edge. Neither condition is met.
- **Crypto leverage:** NO-GO per Phase 4.2.1 research. Alpaca doesn't offer it; second-broker complexity not justified; current strategy classes don't benefit materially after funding costs.

---

## Phased Execution Sequence

### Phase A — Fix the measurement layer (do first, before any tuning)

These changes do not alter trading behavior. They are pure instrumentation and configuration hygiene.

1. **Resolve the `skip_intraday_signals` / `intraday_enabled` config contradiction.** Determine the intended behavior and set one authoritative flag. This is required context before any intraday decision.
2. **Populate `mfe_pct`, `mae_pct`, `exit_reason` in `outcomes` going forward.** Write stop-trigger exits, trailing-stop-trigger exits, and signal exits to `outcomes` with distinct `exit_reason` values. Record MFE and MAE at position close from the `paper_trades` fill history.
3. **Write intraday closed trades to `outcomes`.** The `bar_interval` column exists and distinguishes daily from intraday; the only change is ensuring the intraday loop's FIFO close events write to `outcomes` as they close, not just to `paper_trades`.
4. **Verify the Kelly path is clearing for n≥50 strategies.** Confirm that `botnet101-3-bar-low` (n=390), `4bar-momentum-reversal` (n=361), and `consec-below-ema` (n=564) are receiving Kelly-computed position sizes in live execution, not falling through to the tiered fallback.

### Phase B — Quick wins (implement within 1 week after Phase A)

These are directional changes with strong evidence support that do not require new measurement to validate.

1. **Deactivate `botnet101-consec-bearish`** from `TRACKED_STRATEGIES` (or set `enabled=false`). n=168, PF 0.95 — the evidence is clear.
2. **Quarantine `botnet101-buy-5day-low`** to observe-only (no new entries). Flag for parameter rework before re-enabling.
3. **Deactivate `intraday-1m-orb`** — stop taking new entries. Close or hold existing positions per existing stop logic.
4. **Quarantine `intraday-1m-vwap-reclaim`** — no new entries; monitor existing.
5. **Replace `$250/share` price cap with notional gate.** Change the veto logic to `floor(max_position_usd / share_price) < 1` (i.e., reject only if you can't afford even 1 share) rather than `share_price > 250`. This is a config/code change with zero risk impact — the position notional cap already governs max risk.
6. **Quarantine `trend-donchian-breakout-20` and `trend-ma-cross-20-50`.** No new entries. 0% WR at n=26 and n=4 respectively. Watch-and-accumulate rather than permanent kill, given thin sample.

### Phase C — Edge scaling (implement 2-4 weeks after Phase B, once Phase A data has accumulated)

These require Phase A instrumentation to validate safely.

1. **Review stop data from Phase A instrumentation.** With MFE/MAE now recording, evaluate whether the ATR 2.0× stop for mean-reversion strategies is binding: what percentage of losses exceed 2.0× ATR from entry (i.e., the stop should have fired), and how many trades that eventually won experienced temporary dips deeper than 1.5× ATR (suggesting a tighter stop would have been premature). Use this to decide whether to tighten from 2.0× to 1.5× ATR.
2. **Raise `max_position_fraction` to `0.15`** for strategies with PF > 2.0 (3-bar-low, 4bar-reversal, consec-below-ema, turn-of-month). Monitor deployed capital and drawdown response over 2 weeks.
3. **Activate `regime_aware=true`** in `stops` config. The regime multipliers are already defined; this is a one-flag change. Watch the stop distances in the next 2 weeks against market conditions.
4. **Backtest `rsi2-oversold`, `inside-day-breakout`, and `bollinger-bandit`** on the GDX/XBI/XME/QQQ/IWM ETF universe. If backtest expectancy is positive and PF > 1.5, paper-trade in grace mode.

### Phase D — Expansion (4-8 weeks out, after C validates)

1. **Enable pyramiding on `botnet101-3-bar-low` and `botnet101-4bar-momentum-reversal`** if Phase A MFE data confirms: (a) winning trades regularly extend past the pyramid trigger level before reversing, and (b) the pyramid add-on doesn't coincide with the MFE peak (i.e., we're not adding at the top).
2. **Add 3-5 new EOD sector ETFs** (candidates: XLF, XLV, XLRE, XRT, or SOXX) if backtest on botnet101 strategies shows PF > 1.5 and expectancy > +0.8%/trade on each new symbol.
3. **Evaluate `donchian-retest-short`** — flip `allow_shorts=true` on a paper-only basis and validate the short-direction stop logic before any live activation.
4. **Build gap-fill mean-reversion strategy** on the proven ETF universe. Paper-trade for 60 days minimum before live.
5. **LLM filter graduation decision** (~2026-06-21): if shadow A/B shows positive PnL delta with 200+ outcomes and Sharpe delta ≥ 0.2, flip `llm_filter_live: true`.

---

## Guardrails — What NOT to Do

**Do not optimize stop parameters on 15 days of live data.** The live sample (n=65 round-trips, 15 days) is statistically meaningless for parameter calibration. The backtest corpus (1,853 trades, 2 years) is the right dataset for that — once MFE/MAE is instrumented so the backtest data is trustworthy.

**Do not raise `fraction_of_kelly` above 0.25 before stops are confirmed working.** The fractional Kelly system is conservative for a reason: it hedges against the model's own measurement error. With MFE/MAE unknown, there is an unquantified tail-risk on the loss side. Fix measurement first.

**Do not add more intraday breakout strategies.** The ORB / gap-and-go / momentum breakout category has been tested across 3 independent backtest runs and the live paper session. The results are consistently negative. Adding another variant assumes the prior implementations were flawed, not that the category is wrong — an assumption not supported by the evidence.

**Do not over-expand the symbol universe.** Every n≥20 EOD symbol is profitable because they are sector ETFs with mean-reversion properties. Adding individual stocks, leveraged ETFs, or speculative single names breaks this selection criterion. The ETF screen is the edge.

**Do not activate options, futures, or crypto leverage yet.** All three research documents reach NO-GO conclusions with clear prerequisites. The prerequisites are not met. The system is generating real profit with the existing equity infrastructure; adding instrument complexity before the current instrument is fully measured adds operational risk without evidence of proportional reward.

**Do not disable the LLM filter shadow mode.** It is accumulating data that will inform a graduation decision in ~3 weeks. Do not interrupt the A/B record. Do not flip `llm_filter_live: true` before the 30-day A/B completes with min 200 outcomes.

**Do not interpret the trend strategies' all-loss runs as a signal to redesign them yet.** n=26 for donchian and n=4 for ma-cross is too thin for a statistically confident kill decision. Quarantine (no new capital) is the right posture; redesign is premature.

---

## Strategy Reference Table (for implementation clarity)

| Strategy ID | Status | Evidence | Action |
|---|---|---|---|
| `botnet101-3-bar-low` | Core winner | n=390, PF 2.92, +2.18%/trade | Scale capital; prioritize in sizing |
| `botnet101-4bar-momentum-reversal` | Core winner | n=361, PF 3.31, +2.10%/trade | Scale capital; prioritize in sizing |
| `botnet101-consec-below-ema` | Core winner | n=564, PF 2.76, +1.49%/trade | Maintain; good foundation |
| `botnet101-turn-around-tuesday` | Good | n=60, PF 3.64, +1.59%/trade | Maintain |
| `botnet101-turn-of-month` | Excellent, thin | n=37, PF 8.73, +2.11%/trade | Maintain; expand if more signals available |
| `botnet101-buy-5day-low` | Marginal | n=243, PF 1.28, payoff 0.57 | Quarantine; rework entry/exit parameters |
| `botnet101-consec-bearish` | Losing money | n=168, PF 0.95, −0.06%/trade | **Deactivate** |
| `intraday-1m-orb` | Losing money | Live −$55.91 at 22% WR; backtest −12.9% | **Deactivate** |
| `intraday-1m-vwap-reclaim` | Negative | Live −$7.39 at 39% WR | Quarantine |
| `intraday-1m-momentum` | Marginally positive | Live +$11.83 at 54% WR (n=24) | Keep; observe at n=50 before scaling |
| `intraday-mr-3bar-low-15m` | 1 trade, positive | n=1, +$2.00 | Keep; accumulate data |
| `trend-donchian-breakout-20` | All-loss | n=26, 0% WR, −8.0%/trade | Quarantine; do not scale |
| `trend-ma-cross-20-50` | All-loss | n=4, 0% WR, −4.87%/trade | Quarantine; too thin to kill |
| `breakout-donchian-retest-20` | Untested live | n=0 live outcomes | Observe; let accumulate |
| `breakout-donchian-retest-short-20` | Untested live | n=0 live outcomes | Observe; `allow_shorts=false` gate |
| `rsi2-oversold`, `inside-day-breakout`, `bollinger-bandit` | Coded, not validated | Zero closed outcomes | Backtest on ETF universe before live capital |

---

## Config Changes Reference (for implementation use)

Changes needed, in priority order:

| Config Key | Current Value | Recommended Change | Phase |
|---|---|---|---|
| `auto_trade.skip_intraday_signals` | `true` | Resolve contradiction with `intraday_enabled: true` — pick one authoritative flag | A |
| Strategy: `botnet101-consec-bearish` enabled | On | **Off** | B |
| Strategy: `intraday-1m-orb` enabled | On | **Off** | B |
| Strategy: `intraday-1m-vwap-reclaim` enabled | On | Quarantine (no new entries) | B |
| Strategy: `botnet101-buy-5day-low` enabled | On | Quarantine | B |
| Price cap logic | `share_price > 250` reject | Replace: reject only if `floor(max_position_usd / share_price) < 1` | B |
| `kelly.max_position_fraction` | `0.10` | `0.15` for PF > 2.0 strategies (after Phase A validates Kelly path) | C |
| `stops.regime_aware` | `false` | `true` (after Phase A stop data confirms baseline stops are working) | C |
| `stops.by_class.mean_reversion.atr_multiplier` | `2.0` | Evaluate tightening to `1.5` based on Phase A MAE data | C |

---

_End of Optimization Plan — 2026-06-02_
