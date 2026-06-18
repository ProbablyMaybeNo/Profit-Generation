# Master Audit — Internal Systems Dossier

> Companion to `docs/MASTER_OPTIMIZATION_PLAN.md`. Produced 2026-06-17 by a 6-thread internal audit
> (workflow wf_be0050f6-2b0): purpose/history, execution core, strategies, DB performance reality,
> config/runtime, prior diagnoses. All numbers verified against `data/trading.db` / source unless labelled.

## Top Takeaways

- No live money ever traded. Paper account $100,000 -> $102,367.21 (+2.37%) over ~30 days, peak $103,769.53, max drawdown only -1.39%. The equity curve is the ONLY trustworthy P&L; live trading is structurally impossible (no alpaca_live credentials section, live_strategies=[]).
- Root cause of failure was the execution core, not bad strategies: strategies acted as independent owners of one shared broker position, causing competing flattens -> oversell into accidental shorts (~-$62k growing to the -$101k test figure), wash-trade rejects, and an unmeasurable book. Sprint 3 M1-M10 rebuilt this (broker-as-truth, single-owner, idempotent stop/flatten) and it is prod-verified; short_market_value is now 0.
- 95.2% of outcomes (2,906/3,052) are phantom_no_fill with no broker fill; only ~30 fresh honestly-filled closed outcomes exist in all history. Every headline EOD edge number is backtest/signal-scoped, not live-validated. The live sample is far too thin for any expectancy/Kelly/eligibility decision.
- TWO production bugs are LIVE RIGHT NOW (verified, not historical): (1) the 1d phantom factory still runs because daily_report.py:378 lacks require_fill=True (the intraday pass at :399 has it) -> 13 new phantoms manufactured 2026-06-17; one-line fix. (2) the protective hard stop has NEVER rested on the book (0/409 trades have a stop) due to a fill-settlement race, yet 119 buys are falsely stamped entry_stops='atr_initial'. The only real protection is the soft trailing engine, which acts only on the next scheduled run.
- The edge is unambiguously DAILY mean-reversion (+13.9% across 1d real rows: botnet101-3-bar-low, consec-below-ema, 4bar-reversal, donchian). The bleed is entirely INTRADAY (-415.4% across 1m/5m/15m, led by intraday-1m-momentum -249% and vwap-reclaim -98%). Intraday generated 82%+ of all signals for net-negative return.
- intraday_edge_gate correctly vetoes 32,146 intraday entries because expected move (~0.03-0.10%) is below 0.13% friction+buffer -- intraday 1m strategies have no edge net of cost. Keep them paused or retire; they are pure churn.
- The proven EOD edge is currently PAUSED. The 2026-06-05 Donchian-only reset paused 19 of 30 strategies (including ALL botnet101 winners and the P2-promoted rsi2/rsi14/bollinger). Only trend-donchian-breakout-20 (1d) and intraday-candle-continuation-15m (15m, unpaused to Stage 4 live-on-paper) actually trade. The biggest opportunity is safely un-pausing the MR winners.
- The gating milestone for everything downstream is Sprint 3 M12 (evidence-gated, one-strategy-at-a-time reintroduction framework with IWM/KRE/NVDA/QQQ conflict-regression fixtures). It is OPEN. M11 (intraday time-stop/max-loss overlay) is also OPEN and must precede scaling the candle strategy past Stage 4.
- ~$95k (93%) of capital is idle; only ~$6,930 deployed. The binding constraint is the reset (19 paused strategies), NOT the risk caps (max_open 12, max_position_usd $10k, Kelly max 10% all leave huge headroom).
- MFE/MAE shows avg captured +0.016% vs avg given-back -0.073% (~4.5x) -- the system holds losers into force-flatten. long_exit_signal is the only strongly-positive real exit (+31.1%, 58.8% win); the loss concentrates in stale_intraday_flatten_missed (-412.6% over 99 rows), the leaked-overnight-position pathway.
- Two stop optimizations are built but switched off and now unblocked by MFE/MAE data: stops.regime_aware=false (table populated) and mean_reversion ATR still 2.0x (1.5x proposed to truncate the loss tail). One config line each, validate before/after.
- Order accounting is not 1:1 (205 sells vs 146 buys filled; 4 sells stuck 'accepted'/NULL from the final run because order_sync only runs at pass start). Never derive trade-level P&L from paper_trades or the outcomes table -- exclude phantom_no_fill and stale_intraday_flatten_missed in every report by default.

---

# Profit Generation — Internal Systems Dossier

**Prepared by:** Lead Systems Analyst (merge of 6 audit threads)
**As of:** 2026-06-17 | **Branch:** INTRADAY | **Git HEAD:** `ff198c1`
**DB of record:** `data/trading.db` | **All numbers below are verified against the live DB / source unless explicitly labelled backtest or estimate.**

---

## 0. Executive Summary

Profit Generation is an autonomous algorithmic **paper**-trading system on Alpaca (US equities/ETFs, crypto-capable). It scans a strategy roster on daily (1d) and intraday (1m/5m/15m) bars, fires signals, and an `auto_trader` submits paper orders behind a thick risk stack. It runs as discrete **Windows Scheduled Tasks** under `\TradingSystem\`, not a persistent loop.

The single most important fact: **the system never traded live money.** The paper account went **$100,000.00 → $102,367.21 (+2.37%) over ~30 days**, with a peak of $103,769.53 and a max drawdown of only **−1.39%**.

The second most important fact: **it underperformed not because the strategies were proven losers, but because the execution core was broken.** Strategies behaved as independent position owners against a broker that holds one position per symbol, producing accidental shorts, rejected stops, phantom outcomes, and an effectively unmeasurable book. On **2026-06-05** Ross did a hard "Donchian-only" reset (commit `a95c2b8`), paused 19 of 30 strategies, and is rebuilding the execution core (Sprint 3) plus a disciplined, staged intraday candle trend-follower.

The third most important fact: **the edge picture is clear.** Every net-positive cohort is **daily (1d) mean-reversion**; the entire measured bleed is **intraday (1m/5m/15m)**. The reset stripped the book to the daily edge, which is why the equity curve is green.

The caveat that governs everything: **the live-validated sample is near-empty.** Of 3,052 outcome rows, **2,906 (95.2%) are `phantom_no_fill`** (no broker fill), and only **~30 honestly-filled "fresh" closed outcomes** exist across all history. Edge numbers cited below from the EOD set are largely **backtest / signal-scoped**, not live fills.

---

## 1. What the System Is and Is Supposed To Do

### 1.1 Identity
- **Autonomous Alpaca PAPER-trading system**, equities/ETFs + crypto-capable. Python runtime split: `py -3.13` for the unit env, conda `trading` (Python 3.11) for `alpaca-py`/`yfinance`. `README.md:1-5`.
- Runs as **Windows Scheduled Tasks under `\TradingSystem\`** (Heartbeat, Intraday, DailyReport, Reconcile, Backup, MacroFetch, LiveStream, plus disabled/unregistered tasks). Fixes activate on the **next scheduled task run** — there is no daemon except LiveStream.
- Surrounding surface: Flask dashboard, Telegram control, Notion daily reports, Hermes/GPT cron brief.

### 1.2 Mandate
Scan a strategy roster on 1d + intraday bars → fire signals → auto-submit paper orders through a risk stack → **prove edge on paper** → graduate select strategies to **live**. The live flip was always defined as a **deliberate manual human decision, never an agent milestone** (`PHASE3 plan:183`). **No live flip ever happened**, and is currently structurally impossible (§4.2).

### 1.3 The risk stack (17-stage entry-gate chain)
`monitoring/auto_trader.py` `process_signals` (~lines 3455–3783), in order:
kill-switch → daily-drawdown breaker → earnings veto → negative-sentiment veto (OFF) → intraday edge/cost veto → cool-down → concentration cap → regime-mismatch rotation → **paused-strategy** (`:3621`, `sh_mod.is_paused`) → ma-cross weak-continuation → max-open-per-strategy → PDT guard (intraday) → intraday symbol round-trip cap → live-creds. Then inside `_process_entry`: edge-eligibility (incl. `realized_stats_gate`) → dedupe → **single-symbol-owner authority** → sizing/price/buying-power → unprotected-entry stop preflight.

---

## 2. How It Was Built — Phase Timeline

All phases built by the **milestone-builder agent**. Sequence (completion dates from `docs/PHASE*_PLAN` footers):

| Phase | Date | What shipped |
|---|---|---|
| **Phase 2** (feature-complete, ~50 milestones) | 2026-05-15 | Strategy ingestion/validation, dashboard analytics, Kelly/ATR sizing, risk gates, FRED macro, earnings/sentiment veto. |
| **Phase 3** | 2026-05-16/17 | Paper→live scaffolding (kill switch, Telegram listener, reconcile, live segregation), crypto adapter, options/futures research (both gated). |
| **Phase 4** | 2026-05-17 | Live-promotion tooling, Claude-API codegen, public perf page, and the **trend-following half** (trailing stops, pyramiding, donchian/ma-cross/new-high-volume) wired into `auto_trader`. |
| **Phase 5** | 2026-05-18/19 | **Intraday** trading on 1m/5m/15m + PDT guard + EOD close-out. |
| **Phase 5.5** | 2026-05-19 | **~553-symbol trend-only scanner** (now 552 deduped). |
| **Phase 6** | 2026-05-19/20 | ATR stops generalized, fractional Kelly, breakout-retest, SAR overlay (shadow). Options-pyramiding research **NO-GO**. |
| **Phase 7** | 2026-05-22/24 | Live IEX websocket data layer, `intraday_skips` logging, 1m-native strategies (ORB/momentum/VWAP-reclaim), **LLM filter overlay in shadow mode**. |

**Structural critique:** capability shipped faster than any single strategy accumulated the ≥50 closed outcomes needed to judge edge. The system grew **broad and shallow** — 30 strategies, a 552-symbol scanner, intraday + LLM filter + SAR + options research — none of it validated under the real execution path before scaling. The 30-fresh-outcome reality is the proof.

---

## 3. WHY It Underperformed — Root Causes (with evidence)

### 3.1 ROOT FAILURE: multi-strategy ownership of one broker position
*(Sprint 3 doc `docs/OPTIMIZATION_SPRINT_3.md:11-14`)*
Strategies behaved as if they independently owned positions. Alpaca holds **one position per symbol**, so multiple strategies submitted competing stops/exits/flattens → `40310000` wash-trade rejects, insufficient-qty failures, and **overselling past flat into unintended SHORTS on a long-only system**. There was no single authority for: who owns a symbol, who may submit, what qty is available, whether an exit already exists, or what counts as real performance.

**Damage:** `scripts/flatten_unintended_shorts.py:4-8` cites "~-$62k of such shorts across 10 mega-caps"; the M1 behavioral test (`tests/test_broker_truth_m1.py`, ref `docs/OPTIMIZATION_SPRINT_3.md:67`) cites "the −$101k oversell". A buy-to-cover tool was required to recover. **DB now: `short_market_value = 0.0` (covered/flat).**

### 3.2 Sub-penny initial stops rejected 100% of the time
Across **all 409 `paper_trades`: 0 rows have `stop_price` set, 0 rows have `order_type LIKE '%stop%'`** (verified). Not one protective initial stop ever rested on the Alpaca book. Every ATR stop was rejected with sub-penny increment errors (4dp vs 2dp tick for stocks ≥$1). `docs/INTRADAY_REALITY_CHECK.md:80-124` documents 63/63 stop submits rejected (code `42210000`). The M0 fix shipped (commit `9064ff9`), but see §3.5 — the stop *still* never rests, now for a different reason.

### 3.3 Phantom outcomes contaminated ~95% of the record
`SELECT exit_reason, COUNT(*) FROM outcomes` → **`phantom_no_fill = 2,906 / 3,052 (95.2%)`** (verified). These are signal-scoped rows the orphan sweep booked at fabricated marks with **no backing broker fill**. They kept the lifecycle verifier permanently RED and poisoned expectancy/eligibility gates. The 2026-06-11 batch of 2,846 was the documented cleanup — but **the factory was never turned off for the 1d path** (§3.5). Only **~30 fresh (non-cleanup) closed outcomes** exist in all history.

### 3.4 Intraday lifecycle was only ~7% clean
`docs/INTRADAY_TREND_BUILD_PLAN.md:126`: 12 clean / 7% vs 159 leaked / 90%. Positions leaked overnight/over-days (`stale_intraday_flatten_missed` + `reconciled_no_position`) until a later sweep booked them, often at −4% to −9%. DB still carries **99 `stale_intraday_flatten_missed`** rows and **12 `reconciled_no_position`** rows.

### 3.5 The phantom factory and the naked stop are STILL LIVE (current production bugs)
These two are the most important findings — they are **not historical**, they are happening on every run today.

**(a) 1d phantom factory still active.** `monitoring/daily_report.py:378` calls `outcome_tracker.reconcile_signals` for the **1d EOD pass WITHOUT `require_fill=True`** — verified in source. The intraday pass at `:399` correctly passes `require_fill=True`. So `outcome_tracker.open_for_entry` (`:66-87`) opens an outcome for **every 1d `long_entry` signal that merely has a close price**, no fill required; the same-run orphan sweep then quarantines them as `phantom_no_fill`. Result: **13 new 1d phantoms manufactured then quarantined on 2026-06-17** (all 13 of that date's 1d outcomes); recent sessions show ~100% phantom rate (06-09: 85/85, 06-10: 46/46). 2,634 of all phantoms are `bar_interval='1d'`.

**(b) Protective hard stop never rests — fill-settlement race.** Stops are *enabled* (`atr_multiplier=2.5`, `fixed_percent_fallback=0.05`) and **119 filled buys are stamped `entry_stops='atr_initial'`**, yet **0 stop orders exist**. Mechanism: `_process_entry` submits a market BUY, immediately reads `entry_fill = order.filled_avg_price` (`auto_trader.py:881`) which is `None` on a just-"accepted" order; `_maybe_attach_stop` → `position_manager.safe_submit_stop`, whose `available_to_sell` sees broker qty=0 (fill not settled) → `SKIP_NO_AVAILABLE_QTY` → `info['status']='no_stop'` → **no stop submitted/recorded**. Meanwhile `:914` stamps `entry_stops='atr_initial'` whenever `stop_info.stop_method` is truthy (set at `:2964` *before* submit) — so **the DB falsely advertises stop protection that does not exist on 119 buys.** The only real protection is the soft `trailing_stops` table (20 DB-side trip levels, last `updated_at=2026-06-17T21:04:04`), which only acts on the **next scheduled run** — a between-run gap-down has no floor.

**(c) M7 cannot catch it.** `verify_fill_protected` (`position_manager.py:700-749`): `in_run_ok` requires `stop_info.status=='submitted'` (never happens); falls through to broker `has_protective_stop`, which returns `None` on read failure and is silently treated as not-protected. With `stops_expected=True` it should fire a loud ERROR+Telegram on every entry, but the 0-stop reality proves either the alarm isn't reaching anyone or the unknown read is swallowed.

**(d) `order_sync` strands the last run's sells.** `order_sync.sync_order_fills` runs **only at the START** of a `process_signals` pass (`auto_trader.py:3176-3210`). The current pass's own SELLs are never re-queried; if no further pass runs (EOD final run), they strand at `status='accepted'` with NULL `fill_price` forever. **4 SELLs are currently stuck** from the 2026-06-17 21:04 run. This is the documented precursor to orphan OPEN outcomes (`reconciled_no_position`/`stale_intraday_flatten_missed`).

### 3.6 The lower-tier drags
- **Intraday/ORB has no edge net of cost.** `intraday_edge_gate` vetoed **32,146 entries** (56% of all entry-blocking skips) because expected move (~0.03–0.10%) is below friction+buffer 0.13% — the gate is correct; the strategies are structurally unprofitable at 1m. (`intraday_skips` reason_detail: "expected move 0.036% < friction+buffer 0.130%".)
- **Idle capital / under-trading.** Only **~$6,930 (6.8%) of $102,367 equity deployed**; ~$95.4k idle. The binding constraint is the Donchian-only reset, not the risk caps (max_open 12, max_position_usd $10k, Kelly max 10% all leave huge headroom).
- **`price_too_high` cap ($250)** blocked **6,338 entries** on the liquid large-caps (NVDA/SPY/QQQ at $462–$755) the daily edge wants.
- **No central risk validation.** `auto_trader` never routed submissions through `config/risk.validate_order`; committed settings have `enabled=true, dry_run=false`. Flagged `BUG REPORT.md BR-002/BR-003`; partially superseded by Sprint 3 but the central-risk gap remains a known foot-gun.

---

## 4. Current State — What Is Live RIGHT NOW

### 4.1 The reset
Commit `a95c2b8` (2026-06-05): "Donchian-only reset + Sprint 3 execution-core rebuild backlog". `scripts/reset_to_donchian_only.py` paused all recently-active strategies except `trend-donchian-breakout-20`. **DB verified: 19 paused rows, all `source='sprint3_reset_donchian_only'`, `paused_at=2026-06-06T01:03:31Z`, `expires_at=NULL` (indefinite).**

### 4.2 Mode & switches
- **PAPER, structurally locked.** `paper_trading=true`; `credentials.json alpaca.paper=true`; `accounts.json` single `paper-main` @100% capital; `live_strategies=[]`; **no `alpaca_live` credentials section exists** → live routing is impossible even if a strategy were listed (would hit `SKIP_LIVE_CREDS_MISSING`).
- **Auto-trade ON.** `enabled=true, dry_run=false` → real Alpaca *paper* orders. `intraday_enabled=true` (1m/5m/15m), `trend_scanner_enabled=true`.
- **Kill switch OFF** — no `config/kill_switch.json`; `kill_switch.py:64-65` defaults to not-halted.
- `skip_intraday_signals=true` AND `intraday_enabled=true` both set, but `skip_intraday_signals` is a **dead config key** (defined `auto_trader.py:47`, never read) — the "config contradiction" flagged in two analysis docs is a **non-issue**; only `intraday_enabled` gates intraday.

### 4.3 Sizing & risk (config/settings.json)
`sizing_method=kelly_quarter` (¼ Kelly, `max_position_fraction=0.10`, `pf_size_up` boosts to 0.15 at PF>2.0). Tiered fallback $5k/$7.5k/$10k. `max_position_usd=$10k`, `max_open_positions=12`, `max_open_per_strategy=5`, `max_orders_per_day=100`, `max_new_entries_per_day=25`, `allow_shorts=false`, crypto cap $500. Stops: ATR initial **2.5×** (MR class 2.0×, donchian-retest 1.0×), ATR trailing **3.0×**, **hard −8% max-loss cap** on the trend book (M10). `stops.regime_aware=false` (built, table populated, switched off).

### 4.4 Scheduled tasks (`Get-ScheduledTask \TradingSystem\`)
**Ready (7):** Backup, DailyReport, Heartbeat, Intraday, LiveStream, MacroFetch, Reconcile.
**Disabled (2):** DailyAnalysis, DailyBrief (the Hermes LLM reports).
**Not registered:** TelegramListener, Crypto, Weekly, PublicDeploy.

### 4.5 What is actually entering positions
Despite 11 nominally-eligible strategies, **only TWO have placed orders in the last 7 days:**
- **`trend-donchian-breakout-20`** (EOD 1d, wide 552-symbol scan, PROMOTED) — the intended keep-set strategy.
- **`intraday-candle-continuation-15m`** (15m, OBSERVE/grace-period, 3 names TSLA/AMD/COIN) — **NOT in the paused list; it was unpaused.** The staged intraday candle build has crossed into **live-on-paper (Stage 4+)**.

Today (2026-06-17): **6 orders** total. **5 positions open now:** candle-continuation AMD+COIN (15m); donchian AMAT+ASML+ABBV (1d). The other 9 "non-paused" strategies are dormant (failing eligibility/`realized_stats_gate`, or never signalling).

### 4.6 Execution-core rebuild status — Sprint 3 (`docs/OPTIMIZATION_SPRINT_3.md`)
**Phase 1 M1–M9 DONE + prod-verified:** broker-as-truth reservation ledger (`position_manager._RUN_SELL_RESERVED`), single symbol-owner (`symbol_owner`/`owns_symbol`, oldest open buy wins), idempotent cancel-replace stops/sells (`safe_submit_stop`/`safe_submit_sell` reconcile resting SELLs then cap to net-available, never cross zero), exit-gating to owned holdings, paused-strategy flatten (`_flatten_paused_holdings`), EOD flat assertion (`assert_intraday_flat`), post-fill stop verify (M7), perf-vs-cleanup split, correct exposure accounting. **The single-owner + reservation design correctly fixes the −$101k unintended-short root cause.**
**M10 DONE (2026-06-08):** trend loser cap, −8% hard floor (`git 0e4f6ce`).
**M11 OPEN:** intraday time-stop / per-position max-loss overlay.
**M12 OPEN:** strategy reintroduction framework (the gate to safely re-add the 19 paused strategies).

### 4.7 Intraday candle trend-follower status (`docs/INTRADAY_TREND_BUILD_PLAN.md`, 2026-06-08)
Staged 0–8. **DONE:** Stage 0 lifecycle verifier; Stage 1 pattern library (5 bullish/3 bearish); `candle_continuation` strategy (3-of-5 confirmation chain + time-of-day filter, avoid 11AM–12PM lull); Stage 2 backtest go-gate (15m: **2,417 trades, 37.2% WR, PF 1.18, +0.048% exp**); Track B 9-month historical bars; Stage 3 signal-only; phantom cleanup. **Stage 4 (entry + initial stop, tiny, 3 symbols) reached live — unpaused, 4 buys in last 7d.** **Stages 5–8 (trailing ratchet validation, bearish exit, universe scale, edge decision) PENDING** clean live days.

---

## 5. Realized P&L and Equity Picture (the real numbers)

**The equity curve is the single source of truth for P&L. The outcomes table is NOT a usable ledger.**

### 5.1 Equity (authoritative)
- First: **$100,000.00** @ 2026-05-18T22:41:43Z. Latest: **$102,367.21** @ 2026-06-17T21:04:04Z.
- **+$2,367.21 / +2.37%** over ~30 days. Peak **$103,769.53** (2026-06-05). **Max drawdown −$1,439.81 / −1.39%.** All 859 snapshots `source='auto_trader'`.
- Latest snapshot detail: cash **$95,436.94**, long_market_value **$6,930.27**, short_market_value **$0.0**, buying_power **$401,152.52**. (verified)

### 5.2 Why the outcomes table contradicts the equity curve
- **2,906/3,052 (95.2%) `phantom_no_fill`** (NULL return). Only **141 carry a non-NULL `return_pct`**: SUM **−401.45%**, AVG −2.85%, median −1.73%, win rate **29.8% (42/141)**. (verified)
- Of those 141, **99 (70%) are `stale_intraday_flatten_missed`** force-marks summing **−412.6%** alone (avg −4.17%, 22.2% win).
- **By interval: daily (1d) sum +13.9%; intraday (1m/5m/15m) sum −415.4%.** The −401% total is **intraday measurement noise on tiny capped positions**, directly contradicted by the rising equity curve.
- **Any optimization driven off the outcomes table will optimize against noise.** Reports/backtests must exclude `phantom_no_fill` and `stale_intraday_flatten_missed` by default.

### 5.3 paper_trades cannot yield clean trade P&L
409 rows: 351 filled (146 buy / 205 sell), 54 canceled, 4 accepted-stuck. All `order_type='market'`. Buy notional $231,879.94 vs sell notional $330,475.43 (net +$98,595.49) — **205 sells vs 146 buys** is the per-strategy-buy vs per-symbol-flatten mismatch; not 1:1 matchable. **Use the equity curve.**

---

## 6. Demonstrated Edges vs Bleeds

### 6.1 The edge is DAILY mean-reversion (all net-positive cohorts are 1d)
Per-strategy realized `return_pct` (real rows only; from the contaminated set, treat as directional not absolute):

| Strategy | n | Sum ret | Win | Status |
|---|---|---|---|---|
| botnet101-3-bar-low | 5 | **+24.80%** | 100% | PAUSED |
| botnet101-consec-below-ema | 8 | **+15.00%** | 75% | PAUSED |
| botnet101-4bar-momentum-reversal | 3 | **+3.08%** | — | PAUSED |
| trend-donchian-breakout-20 | 5 | **+2.19%** | 40% | **LIVE** |

**Backtest edge (offline, signal-scoped — the headline numbers):** EOD botnet101 family across **1,853 daily backtest outcomes: 65.5% WR, +1.33%/trade, PF 2.22.** Top: 3-bar-low (PF 2.92), 4bar-momentum-reversal (PF 3.31), consec-below-ema (PF 2.76, largest n). `docs/SYSTEM_DATA_ANALYSIS.md`. **These are real backtest numbers but the live-validated sample is ~30 fresh closes total.**

**P2-validated MR extensions (backtest, coded, committed `4c5a53e`, then re-paused by the reset):** rsi14-oversold (PASS, n=116, +1.39%, PF 1.81, sharpe_ish 0.218 — best risk-adjusted), bollinger-bandit, rsi2-oversold. Best symbol cells: rsi2 on XLK = PF 3.77, on SMH = PF 2.84. Symbol adds XLK/SMH/SPY/DIA/XLY/XLC.

### 6.2 The bleed is INTRADAY (entirely)
| Strategy | n | Sum ret | Win |
|---|---|---|---|
| intraday-1m-momentum | 61 | **−249.32%** | 24.6% |
| intraday-1m-vwap-reclaim | 18 | **−98.02%** | 16.7% |
| intraday-1m-orb | 14 | −22.13% | — |
| trend-ma-cross-20-50 | 4 | −17.46% | — |
| intraday-orb-pivots-5m | 6 | −16.71% | 0% |
| intraday-orbo-5m | 6 | −14.92% | — |
| rsi2-oversold | 1 | −12.76% | — |
| intraday-mr-3bar-low-15m | 6 | −11.73% | — |

Intraday generated **82%+ of all 49,712 signals** (intraday-1m-momentum 22,711 + vwap-reclaim 18,210) for net-negative return. `botnet101-consec-bearish` is a backtest loser (PF 0.95, −0.06%, n=168).

### 6.3 Exit-reason quality & the give-back signature
- **`long_exit_signal` is the only strongly-positive real exit: +31.1% over 17, 58.8% win.** `trailing_stop` +1.0% (n=3), `eod_close` +0.5% (n=10). The loss is concentrated in `stale_intraday_flatten_missed` (−412.6%, n=99).
- **MFE/MAE (110 real rows): avg MFE +0.016% vs avg MAE −0.073%** — positions give back **~4.5× more than they ever showed in profit.** This is the signature of holding losers into a force-flatten, and the clearest mechanical lever (tighten/trail to keep captured upside).
- `trailing_stops`: 20 rows, all `atr_trail`/long, **actively ticking** (7 updated 2026-06-17). This is the **only** protective mechanism that has ever actually run.

---

## 7. Open Problems & Proposed-but-Unbuilt Fixes

### 7.1 CRITICAL — live, regenerating, or unprotected
| # | Problem | Fix (proposed) | Evidence |
|---|---|---|---|
| P1 | **Naked long on every entry** — hard stop never rests (fill-settlement race); soft trailing only acts next run; gap-down = no floor. | Decouple stop from same-instant settlement: fall back to entry qty when `available_to_sell==0` after a just-submitted buy; OR poll entry to `filled` before arming; OR use an **Alpaca bracket/OTO** so the stop rests atomically on fill. Verify `order_type='stop'`/`stop_price` rows finally appear. | `auto_trader.py:881,902-924,3004-3028`; `position_manager.py:621-634`; DB 0/409 stops vs 119 stamped |
| P2 | **1d phantom factory still manufacturing rows daily** (13 created+quarantined 2026-06-17). | **One-line fix:** add `require_fill=True` to the 1d `reconcile_signals` call at `daily_report.py:378`, mirroring the intraday pass at `:399`. | verified: `:378` lacks it, `:399` has it; 13/13 1d outcomes today phantom |
| P3 | **`entry_stops='atr_initial'` falsely stamped** on 119 buys with no resting stop — corrupts any audit trusting it. | Gate `:914` on `stop_info.get('status')=='submitted'` instead of `stop_method` truthiness. Add daily invariant: stamped-buys ≈ resting/filled stops (current 119-vs-0 would have flagged P1 instantly). | `auto_trader.py:914` vs `:2960-2967` |
| P4 | **`order_sync` strands the last run's sells** (4 stuck `accepted`/NULL today) → orphan OPEN outcomes. | Run `sync_order_fills` at **END** of each pass too, or add a dedicated EOD reconcile task. | `auto_trader.py:3176-3210`; 4 stuck rows |

### 7.2 HIGH — the path back to multi-strategy & the dormant edge
- **M12 (strategy reintroduction framework)** — the gating OPEN milestone. Evidence-gated (≥20 fresh closes), one-strategy-at-a-time, conflict-regression fixtures on IWM/KRE/NVDA/QQQ. **Highest-leverage open item.** Without it, re-enabling risks repeating the multi-owner oversell loop.
- **The validated EOD MR edge is PAUSED and not trading** — the entire opportunity cost of the reset. Once M12 exists, **unpause botnet101-3-bar-low / consec-below-ema / 4bar-momentum-reversal first** (they carry the historical edge; the core that broke them is now rebuilt + prod-verified), then promote the P2 extensions (rsi14 first).
- **M11 (intraday time-stop / per-position max-loss overlay)** — build before scaling the candle trend-follower past Stage 4; the prior intraday set bled partly from no hard intraday loss cap.
- **Measure edge cleanly NOW.** With phantoms quarantined and ownership/stops being fixed, the precondition for any sizing/eligibility decision is **accumulating fresh closed outcomes** on Donchian + candle. The current ~30-fresh base is far too thin.
- **Idle capital (~$95k).** Binding constraint is the reset, not the caps. Re-admitting 2–3 proven MR strategies multiplies capital-at-work.

### 7.3 MEDIUM
- **Stop optimizations gated on MFE/MAE, now unblocked but unapplied:** set `stops.regime_aware=true` (built, table populated) and tighten `mean_reversion.atr_multiplier` 2.0→1.5 to truncate the −10%+ loss tail and lift the 0.95 payoff ratio. One config line each; validate on accumulated MFE/MAE.
- **`trend-donchian-breakout-20` wins=2 / n=672 anomaly** — likely an outcomes wins-flag artifact, or a genuinely near-zero-edge lone live strategy. Reconcile before leaning the whole live book on it.
- **NULL `return_pct` on whole cohorts** (botnet101-buy-5day-low n=253, consec-bearish n=168, turn-of-month n=37) — un-judgeable even if unpaused (`_is_eligible` filters `return_pct IS NOT NULL`, `auto_trader.py:181`). Fix the population.
- **$250 price cap** blocks NVDA/SPY/QQQ — switch to notional/ATR-based sizing cap, not an absolute share-price veto.
- **Outcome model is incoherent for the 1d path.** Decide and enforce **position-scoped** (one row per filled entry) — permanently kills the phantom class and makes per-strategy eligibility/Kelly trustworthy.
- **In-loop reconcile latency:** `reconcile_stop_fills` + `sweep_orphan_outcomes` + `order_sync` all run per-row at the top of the pass (`auto_trader.py:3191-3229`), best-effort; a slow broker / large backlog adds unbounded latency before any signal evaluates.
- **`verify_fill_protected`** should be promoted from best-effort warning to a tracked daily-report counter so a chronic 0-stop condition can't hide.
- **Owner authority edge case:** a sell that FAILED at broker but was recorded `accepted` would wrongly free a still-held symbol to a second strategy (`position_manager.py:504-521`); 4 stuck-accepted sells are currently in this state.

### 7.4 LOW (hygiene / scope)
- Paused strategies **still run full signal generation** (22,711 momentum signals/30d; 261,842 `intraday_skips` rows). Gate at the scanner layer, not just entry.
- **Pyramiding 100% dormant** (`pyramid_tier` NULL on all 409 trades; no column in outcomes). Built, never activated.
- **Dead strategy modules:** `strategies/generated/gap_fill_reversion.py` (candidate-only, not wired), `smc/strategy.py`, ORB EOD variants, ross-cameron-five-pillar (all verdict=FAIL/compute_fn=None). Archive to shrink roster 30→~22.
- **Gap-fill MR** backtested P2 and HELD (sharpe_ish 0.061 < 0.1; works on QQQ PF 3.45 but fails high-beta GDX/XME/XLE). Needs universe-restriction rework if revisited.
- **Short side** never validated (`breakout-donchian-retest-short-20` not paused, n=0 closed).
- Re-enable DailyAnalysis/DailyBrief if Hermes reports are wanted (wired, scheduled, disabled).
- **Strategic call:** the reset already implicitly chose "narrow and deep." Formalizing it (concentrate on the 1–2 proven edges, retire the breadth that caused the original failures) would shrink the surface area that produced the collapse.

---

## 8. SHIPPED vs PENDING (sprint ledger, cross-checked vs git + DB)

**SHIPPED & prod-verified:** Sprint 1 M0–M4 (sub-penny stop fix `9064ff9`, MFE/MAE instrumentation, quarantine negative-edge, qty<1 veto, deploy-idle size-to-edge). Sprint 2 M1–M8 (per-symbol order manager, unintended-short cover, pause negative-expectancy intraday, expectancy gate, exit-dedup, cost/slippage gate, ma-cross regime gate, report honesty). Sprint 3 M1–M10 (execution-core rebuild + −8% cap). Audit fixes F1–F8, A1–A5, B1–B3. Phantom quarantine (2026-06-11, `f20bb54`/`09140f6`) which **unfroze Donchian eligibility** (n=35/−7.3% INELIGIBLE → n=1/+2.1% grace).

**PENDING / unbuilt:** Sprint 3 **M11** (intraday time-stop overlay) and **M12** (reintroduction framework) — both unchecked. The 1d-path `require_fill` fix (TICKET remaining item 2). Position-scoped outcome model (TICKET item 1). `regime_aware=true` + MR ATR 1.5×. Pyramiding activation. The four §7.1 production bugs (P1–P4) — note P1/P3/P4 are **newly surfaced by this audit** and are not on the existing sprint board.
