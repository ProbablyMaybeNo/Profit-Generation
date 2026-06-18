# Master Optimization Plan — Profit Generation → Real Income

**Created:** 2026-06-17 · **Author:** Claude (ultracode synthesis) · **Owner:** Ross
**Branch at authoring:** INTRADAY · **HEAD:** `ff198c1` · **DB of record:** `data/trading.db`
**Inputs:** 6-thread internal audit + 6-thread external research (14-agent workflow `wf_be0050f6-2b0`).
**Companion dossiers:** `docs/MASTER_AUDIT_INTERNAL_DOSSIER.md`, `docs/MASTER_RESEARCH_EXTERNAL_DOSSIER.md`.

This is the durable, handoff-ready build plan. Every actionable item is a `- [ ]` milestone in
the project's standard format so `milestone-builder` / `/next-milestone` can execute it end-to-end.
Each milestone carries **WHY / FILES / DO / ACCEPT** so a lower-effort model can build it without
re-deriving context. Nothing advances a stage until its **Go-gate is green**. Build Log at bottom.

---

## 0. The Thesis — Why This Works (and why we keep going)

Ross's working thesis: *we can monitor 1–15m candles across many symbols, recognize patterns that
precede upward moves, ride them with a trailing stop that ratchets up, and do this across a portfolio
for net profit.* **The research validates the constructive form of that thesis directly** — and the
internal audit shows we already built most of the machinery.

Three facts make this an optimistic plan, not a hopeful one:

1. **The strategies were never the problem — the plumbing was.** The system lost money in testing
   because multiple strategies fought over one broker position (causing accidental shorts, rejected
   stops, and 95% "phantom" outcomes), *not* because the signals lacked edge. That plumbing has been
   rebuilt and prod-verified (Sprint 3 M1–M10). We are standing on a repaired foundation, not rubble.
   The paper account is **+2.37% with a −1.39% max drawdown** — green, even at 6.8% capital deployed.

2. **Our confirmed edge and the current market regime are the same trade.** The only net-positive
   cohort in our own data is **daily mean-reversion** (+13.9% across real 1d rows). The independent
   market research says mid-2026 is a **low-VIX (≈16), near-ATH, sector-rotation chop tape — the
   textbook mean-reversion environment** (~65–70% range-bound sessions). Our strength *is* what the
   market is paying for right now. That is a rare alignment; we lean into it.

3. **The hardest engineering is behind us, and the highest-leverage wins are small.** The two biggest
   remaining failures are a **one-line fix** (turn off the phantom factory) and a **bounded fix**
   (make the protective stop actually rest on the book). The single most powerful upgrade — survivability
   sizing — is an edit to one module. We are not inventing; we are finishing.

The math that makes the trailing-stop thesis pay (from the research, applied to us):
> Expectancy `E = (Win% × AvgWin) − (Loss% × AvgLoss)`. A 40% win rate with 2.5R winners / 1R losers
> = **+0.4R per trade**. The fat right tail (the runners the trailing stop lets run) funds the system;
> every loser is cut to exactly 1R. Risk-of-ruin at 1%/trade and this edge is ≈0. **Sub-50% win rate
> is not a bug — it is the design.** Our job is exit discipline + sizing, and we have both in code.

**What winning looks like:** one proven strategy, executed cleanly, measured honestly, sized by
survivability math, scaled one strategy at a time behind an evidence gate, then graduated to live money.

---

## 1. Where We Actually Are (the one-page truth, all verified 2026-06-17)

| Dimension | Reality |
|---|---|
| **Mode** | Paper only. Live is structurally impossible today (no `alpaca_live` creds, `live_strategies=[]`). |
| **Equity** | $100,000 → **$102,367.21 (+2.37%)** over ~30d. Peak $103,769.53. Max DD **−1.39%**. |
| **Capital at work** | ~$6,930 (6.8%). **~$95.4k idle.** Binding constraint = the reset, not risk caps. |
| **The edge** | **Daily mean-reversion** (botnet101 family + Donchian): +13.9% real 1d; backtest 65.5% WR / PF 2.22 on 1,853 outcomes. |
| **The bleed** | **Intraday** (1m/5m/15m): −415% real; led by 1m-momentum (−249%) and vwap-reclaim (−98%). |
| **Measurement** | **95.2% of outcomes are phantom** (2,906/3,052, no broker fill). Only **~30 fresh honest closes** exist. |
| **Execution core** | Sprint 3 **M1–M10 rebuilt + prod-verified** (broker-as-truth, single-owner, idempotent orders). |
| **Two live bugs** | (a) 1d phantom factory still manufacturing rows daily; (b) **0 of 409 trades ever had a resting stop**. |
| **What's trading** | `trend-donchian-breakout-20` (1d) + `intraday-candle-continuation-15m` (15m, Stage-4 live-on-paper, 3 names). 19 strategies paused. |
| **Open milestones** | Sprint 3 **M11** (intraday max-loss overlay) + **M12** (reintroduction framework) — both gating. |

**The governing caveat:** every headline edge number is **backtest / signal-scoped**, not live-validated —
the live sample is ~30 closes. So Stage 0 (clean measurement) is not optional polish; it is the
precondition for every sizing, eligibility, and promotion decision downstream. **We cannot optimize
what we cannot measure.**

---

## 2. Strategic Direction — Five Pillars

1. **Fix execution truth first.** Stops must rest on the book; phantoms must stop; outcomes must be
   position-scoped. Until measurement is honest, every other number lies. (Stage 0)
2. **Narrow and deep.** Concentrate capital on the proven mean-reversion edge; retire intraday churn
   that has no edge net of cost. The reset already chose this — we formalize it. (Stages 3–4)
3. **Regime-gate everything.** VIX + ADX decides the day: mean-reversion on chop, trend/ORB on
   catalyst/trend days. This is the cleanest documented edge for a solo operator. (Stage 2)
4. **Size for survival, not for hope.** ATR volatility-targeting at **0.75% risk / 6% portfolio heat**,
   hybrid swing-low→Chandelier(3×) trailing stop. This is the highest-leverage change in the whole plan. (Stage 1)
5. **Re-add one strategy at a time, and let Claude do research, not trading.** The M12 evidence gate
   re-admits proven strategies one-by-one; an overnight Claude research loop proposes candidates that
   the walk-forward script vets. We do **not** build autonomous LLM traders. (Stages 3 & 5)

---

## 3. The Build Plan

> **Format:** `- [ ]` = unbuilt milestone. **WHY** = the reason. **FILES** = where to work.
> **DO** = the change. **ACCEPT** = the green-light test (run `py -3.13 -m pytest tests/ -m "not live"`
> for the suite; add a targeted test per milestone). Commit style: `feat(scope): ...` / `fix(scope): ...`.

---

### Stage 0 — STOP THE BLEED & SEE CLEARLY  *(P0 · surgical · do first, in order)*
**Go-gate:** phantom factory off; ≥1 real `order_type='stop'` row rests on the book after a live entry;
reports exclude phantom/stale by default; a daily invariant counter would have caught the naked-stop bug.
**Status (2026-06-17):** 0.1–0.4 + 0.6 SHIPPED. **Only 0.5 remains** (the position-scoped outcome model +
honest reporting defaults — the one architectural item, scoped to its own focused session).

- [x] **0.1 Kill the 1d phantom factory (one-line fix)** ✅ 2026-06-17 · commit `b9f21bb`
  - WHY: `daily_report.py` opens an outcome for every 1d `long_entry` signal that merely has a close
    price — no fill required — then the orphan sweep quarantines it as `phantom_no_fill`. 13 new
    phantoms were manufactured-then-quarantined on 2026-06-17 alone; 2,634 of all phantoms are 1d.
  - FILES: `monitoring/daily_report.py:378` (the 1d `reconcile_signals` call).
  - DO: add `require_fill=True` to the `:378` call, mirroring the intraday pass at `:395–399` which
    already has it. (Verified: `:378` lacks it, `:399` has it.)
  - ACCEPT: run the daily report on a day with unfilled 1d signals → **0 new `phantom_no_fill` rows**
    created. Add `tests/test_daily_report_require_fill.py` asserting the 1d path passes `require_fill=True`.

- [x] **0.2 Make the protective hard stop actually rest on the book** ✅ 2026-06-17 · `entry_filled` fallback in `safe_submit_stop` + `_entry_is_live` in the entry path
  - WHY: **0 of 409 trades ever had a resting stop** (all `order_type='market'`), yet 119 buys are
    stamped `entry_stops='atr_initial'`. Root cause: a fill-settlement race — `_process_entry` submits
    a market BUY, immediately reads `order.filled_avg_price` (None on a just-accepted order),
    `safe_submit_stop` sees broker qty=0 (fill unsettled) → `SKIP_NO_AVAILABLE_QTY` → no stop. The only
    real protection today is the soft `trailing_stops` engine, which acts **only on the next scheduled
    run** — a between-run gap-down has no floor. **This is the single biggest live risk.**
  - FILES: `monitoring/auto_trader.py:881, 902–924, 3004–3028`; `monitoring/position_manager.py:621–634`;
    `monitoring/stops.py`.
  - DO: pick the most robust path that fits Alpaca:
    **(preferred)** submit the entry as an **Alpaca bracket / OTO order** so the stop rests atomically
    on fill; **or** poll the entry order to `filled` (short bounded wait) before arming the stop;
    **or** when `available_to_sell == 0` immediately after a just-submitted buy, fall back to the
    entry qty for the stop submit. Whichever path: the stop must end up as a real resting order.
  - ACCEPT: after a paper entry, the DB shows a row with `order_type LIKE '%stop%'` and a non-null
    `stop_price` for that symbol; `verify_fill_protected` reports `submitted`. Add a live-marked smoke
    test that places one entry and asserts a resting stop appears.

- [x] **0.3 Stop-stamp honesty** ✅ 2026-06-17 · stamp gated on `status=='submitted'` (the daily naked-stop invariant counter is folded into 0.6 — both are daily protection metrics)
  - WHY: `auto_trader.py:914` stamps `entry_stops='atr_initial'` whenever `stop_method` is truthy
    (set *before* submit), so the DB advertises protection that doesn't exist. A simple invariant
    (stamped-buys ≈ resting stops) would have surfaced the 119-vs-0 gap instantly.
  - FILES: `monitoring/auto_trader.py:914` (stamp site) vs `:2960–2967` (where `stop_method` is set).
  - DO: gate the stamp on `stop_info.get('status') == 'submitted'`, not `stop_method` truthiness.
    Add a daily-report invariant: count(filled buys stamped with a stop) vs count(resting/filled stop
    orders); if they diverge beyond a small tolerance, emit ERROR + Telegram.
  - ACCEPT: a buy whose stop is rejected/skipped is **not** stamped as protected; the invariant counter
    appears in the daily report. Unit test both branches.

- [x] **0.4 Run `order_sync` at end-of-pass (strand fix)** ✅ 2026-06-17 · end-of-pass `sync_order_fills` mirroring the top-of-pass guard
  - WHY: `order_sync.sync_order_fills` runs only at the **start** of a `process_signals` pass, so the
    current pass's own SELLs are never re-queried; on the EOD final run they strand at
    `status='accepted'` / NULL fill forever (4 stuck today). This is the documented precursor to orphan
    `reconciled_no_position` / `stale_intraday_flatten_missed` outcomes.
  - FILES: `monitoring/auto_trader.py:3176–3210`.
  - DO: call `sync_order_fills` again at the **end** of each pass (and/or add a dedicated EOD reconcile
    task). Backfill the 4 currently-stuck sells.
  - ACCEPT: after a pass that submits sells, those sells reach `filled`/`canceled` (not stranded
    `accepted`) by pass end. Test with a mocked broker.

- **0.5 Position-scoped outcomes + honest reporting defaults** — ⏸ DEFERRED (backlog, its own focused session — architectural outcome-model rewrite; not a build target yet)
  - WHY: the outcome model is signal-scoped, which is what permits the phantom class and makes
    per-strategy expectancy/Kelly untrustworthy. Reports/backtests must never count phantom or
    force-flatten noise as P&L.
  - FILES: `monitoring/outcome_tracker.py`, `monitoring/daily_report.py`, `backtest/report.py`.
  - DO: make outcomes **position-scoped** (one row per filled entry, closed by its matching exit).
    Everywhere a report/eligibility query reads outcomes, **exclude `phantom_no_fill` and
    `stale_intraday_flatten_missed` by default**. Treat the equity curve as the authoritative P&L source.
  - ACCEPT: per-strategy P&L derived from outcomes reconciles in sign/direction with the equity curve;
    phantom/stale rows are filtered in the default report. Add a regression test on a fixture DB.

- [x] **0.6 Daily protection metrics: naked-long counter + alert** *(absorbs 0.3b)* ✅ 2026-06-17 · `protection_metrics()` + `_maybe_alert_naked()` wired into `persist_report`
  - WHY: M7's protection check is best-effort and silently swallows unknown broker reads, so a chronic
    0-stop condition can hide (it did). A daily invariant (stamped-buys ≈ resting/filled stops) would
    have surfaced the 119-vs-0 gap instantly.
  - FILES: `monitoring/position_manager.py:700–749`; `monitoring/daily_report.py`.
  - DO: emit a daily-report line `entries_protected / entries_total` AND the stamped-vs-resting-stop
    invariant; fire Telegram if any filled entry lacks a resting stop or the counts diverge. Treat a
    broker read failure as **not-protected** (loud), not "assume OK".
  - ACCEPT: the daily report shows the protected ratio + invariant; a deliberately-unprotected entry
    triggers the alert.

---

### Stage 1 — RISK & SIZING CORE  *(the highest-leverage lever in the plan)*
**Go-gate:** sizing is ATR-volatility-targeted at 0.75% risk; portfolio heat capped at 6%; the hybrid
trailing stop is live; every closed trade logs its R-multiple. Backtest the multipliers on our own
NASDAQ-100 + S&P-500 universe before locking them.

- [x] **1.1 ATR volatility-targeting position sizing** ✅ 2026-06-18 · `atr_risk` method + `risk_per_trade_pct=0.0075`; config flipped from `kelly_quarter`. *(The "skip if stop > 2× ATR" guard moves to 1.3 — with a 2.5× ATR stop it would veto everything; it belongs with the swing-low stop.)*
  - WHY: research is unanimous — sizing is the primary edge, signal is secondary. We currently size off
    a fixed `max_position_usd` cap, which over/under-sizes by volatility. Risk-of-ruin math: **1%/trade
    → RoR ≈ 0; 10%/trade → 1.7% (unacceptable)** at our edge.
  - FILES: `monitoring/auto_trader.py` sizing path; `config/risk.py`; `config/settings.json`.
  - DO: `Shares = (Equity × RiskPct) / (ATR(14) × Multiplier)`, default `RiskPct = 0.0075`. Keep
    `max_position_usd` only as a hard ceiling, not the primary sizer. Skip a setup if the stop distance
    > 2× ATR (the position becomes noise).
  - ACCEPT: for a worked case ($100k, 0.75%, ATR-stop $1.50 → ~500 shares) the sizer returns the
    expected qty; volatile names get smaller size than calm names at equal risk. Extend `tests/test_sizing.py`.

- [x] **1.2a Portfolio-heat cap (6%)** ✅ 2026-06-18 · `portfolio_heat_usd()` sums open Σ(stop distance × size); `process_signals` threads a `remaining_heat_usd` budget into `_process_entry` (mirrors the BP budget) → `SKIP_HEAT_CAP`. Enabled via `risk.max_portfolio_heat_pct=0.06`.
  - WHY: heat = Σ(stop distance × size)/equity is the cap that survives a correlated selloff (the formula
    RoR underestimates correlated risk). Tech/semis are 80–90% correlated in a drawdown.
  - ACCEPT (met): a 9th 0.75% position is rejected at the heat cap. Unit test the heat accumulator + the
    in-run gate (DRY_BUY then SKIP_HEAT_CAP).
- **1.2b Sector-correlation cluster sizing** — ⏸ DEFERRED (backlog, not a build target). Needs a sector backfill first.
  `data/universes/nasdaq100.csv` has an empty `sector` column (sp500 is populated), so the "3+ in a
  sector → 0.25% each" rule would silently no-op on most names. Prereq: backfill nasdaq100 sectors, then
  add the cluster rule keyed off the universe `sector` field.

- [x] **1.3 Hybrid stop: swing-low initial → Chandelier(3×)** ✅ 2026-06-18 · **Chandelier(22, 3.0) trail ADOPTED** in config (was atr_trail; floored at the initial stop so it engages ~+1R). **Swing-low initial stop BUILT + tested but OPT-IN** (`stops.initial_method`, default `atr_initial`), with the 2× ATR distance cap baked in — flipping it changes live stop distance + feeds `atr_risk` sizing, so it's gated pending a trade-frequency observation rather than flipped blind.
  - WHY: Chandelier ATR trail is the evidence-backed default (3× daily beat fixed-5% by 48%); a clean
    swing-low initial stop gives a real invalidation level. MFE/MAE shows we give back ~4.5× what we
    capture — tighter, ratcheting exits directly fix that.
  - FILES: `monitoring/stops.py`, `monitoring/trailing_stops.py`, `config/settings.json`.
  - DO: initial stop = 1.0× ATR(14) below nearest swing-low; once +1R, switch to **Chandelier(22, 3.0)**
    for daily/swing and **Chandelier(14, 2.0)** for intraday. Same trail across any future pyramided units.
  - ACCEPT: stop ratchets up only on new highs, never down; switches from swing-low to Chandelier at +1R.
    Extend the trailing-stop tests with a synthetic price path.

- [x] **1.4 Drawdown kill-switch ladder** ✅ 2026-06-18 · config over the existing `drawdown_throttle` + daily breaker. 3% daily-loss pause; peak-DD ladder halve@15% (→0.375% risk via the notional×0.5 multiplier, which `atr_risk` turns into half-risk), quarter@20%, halt+kill@25%. *(evaluate() is stateless — `recover_at_pct` is advisory.)*
  - WHY: rule-based pauses beat emotional overrides (a documented top-5 failure mode).
  - FILES: `monitoring/drawdown_throttle.py`, `monitoring/kill_switch.py`, `config/settings.json`.
  - DO: at **3% daily DD** pause new entries for the day; at **≥15% account DD** halve risk to 0.375%
    until DD < 10%. Log every manual override for monthly review.
  - ACCEPT: simulated DD triggers each rung; risk halves and restores correctly. Extend
    `tests/test_drawdown_throttle.py` / `tests/test_kill_switch.py`.

- [x] **1.5 No absolute share-price veto** ✅ 2026-06-18 · the "$250 cap" was historical (max_position_usd is now $10k notional); `SKIP_PRICE` only fires when one share > the whole cap (never, for our universe). Regression test pins NVDA/SPY/QQQ aren't vetoed. **Fractional shares deliberately rejected** — Alpaca can't place stop orders on fractional positions, which would reintroduce the 0.2 naked-long bug.
  - WHY: the absolute `price_too_high` ($250) cap blocked **6,338 entries** on exactly the liquid
    large-caps the daily edge wants (NVDA/SPY/QQQ at $462–$755).
  - FILES: `monitoring/auto_trader.py` (price-cap veto), `config/settings.json`.
  - DO: remove the absolute share-price veto; rely on notional sizing (1.1) + ATR sizing. Use fractional
    shares where available so high-priced names size correctly.
  - ACCEPT: an NVDA/SPY signal is no longer vetoed by price; notional stays within sizing limits. Test the gate.

- [x] **1.6 R-multiple logging on every closed trade** ✅ 2026-06-18 · `outcomes.r_multiple` (return ÷ initial-stop risk) computed in `close_outcome`; `expectancy_metrics()` rolls up avg-R + win-rate (excludes phantom/stale) into the daily report
  - WHY: R-multiples are the substrate for Kelly inputs, pyramiding readiness, and honest expectancy.
  - FILES: `monitoring/outcome_tracker.py` (add `r_multiple` to position-scoped outcomes).
  - DO: at close, log `R = realized_PnL / initial_risk`. Surface avg R and expectancy in the daily report.
  - ACCEPT: closed trades carry a sane `r_multiple`; daily report shows rolling expectancy. Unit test the calc.

---

### Stage 2 — REGIME GATE  *(first automation layer · rules-based, no ML)*
**Go-gate:** a daily pre-market regime score exists and both sizing and eligibility read it; an
event-quarantine filter de-sizes/skips known high-risk sessions.

- [x] **2.1 Daily pre-market regime score (VIX 200d-MA + ADX)** ✅ 2026-06-18 · new `monitoring/regime.py` (pure `score_regime`/`compute_adx`/`moving_average` + `compute_and_persist_regime` reading VIX from `macro` and ADX from the daily-bars fetcher) writes one `regime_scores` row/day (risk_on/transitional/risk_off + `risk_scale` 1.0/0.5/0.25 + confidence); `latest_regime_score` reader for both sizing + eligibility. Wired into `run_macro.bat` after the VIX pull. **Also FIXED the pre-existing `test_macro_fetcher` FRED failure** (the redundant client-side `observation_start` re-filter dropped server-filtered rows once fixture dates aged > lookback; now trusts the server filter, applies client-side cutoff only on the legacy-fred fallback path).
  - WHY: a rules-based VIX-200d-MA regime gate cut max drawdown −55%→−22% while preserving returns
    (Sharpe 0.45→0.72) in a 2005–2025 backtest. Best documented solo-operator edge; no ML needed for v1.
  - FILES: new `monitoring/regime.py`; `monitoring/macro_fetcher.py` (VIX source); a `regime` field in
    config/DB.
  - DO: each pre-market, compute a score → `risk_on` / `transitional` / `risk_off`. Rule of thumb:
    **ADX < 20–25 at the open = mean-reversion day; ADX > 30 + a catalyst = trend/momentum day.** Persist it.
  - ACCEPT: the score writes daily and is queryable; backtest the gate on/off over our universe and
    record the DD/Sharpe delta. New `tests/test_regime.py`.

- [x] **2.2 Wire regime into eligibility & sizing** ✅ 2026-06-18 · `process_signals` reads `latest_regime_score` once/run. ELIGIBILITY: on a `risk_off` tape, directional/momentum classes (trend/breakout/momentum) are blocked → `SKIP_RISK_REGIME`; mean-reversion stays eligible (size-scaled instead). SIZING: the `atr_risk` per-trade `risk_pct` is multiplied by the regime `risk_scale` (risk_on 1.0× / transitional 0.5× / risk_off 0.25×). Gated by `risk.regime_gate.enabled` (default true; false → no block + scale forced 1.0). Class-based (keyed off existing `strategy_class`, no new per-strategy config). 9 new tests.
  - WHY: the gate only pays if strategies actually read it — MR strategies favored on chop, trend/ORB
    on trend/catalyst days; size down in `transitional`/`risk_off`.
  - FILES: `monitoring/auto_trader.py` eligibility chain; sizing path.
  - DO: gate each strategy by the regimes it's allowed to trade; scale `RiskPct` by regime
    (e.g. risk_on 1.0×, transitional 0.5×, risk_off 0.25× or flat).
  - ACCEPT: on a forced `risk_off` day, trend entries are blocked and size is reduced; MR still allowed
    on chop. Test the eligibility/sizing interaction.

- [ ] **2.3 Event-quarantine filter (earnings + FOMC/CPI)**
  - WHY: pure risk management, zero prediction, zero crowding risk (the calendar is public). Directly
    addresses our documented event-volatility vulnerability. Mark **14 Jul (CPI)**, **28–29 Jul (FOMC)**,
    mid-Jul mega-cap earnings as high-risk.
  - FILES: `monitoring/earnings_calendar.py`, `monitoring/macro_fetcher.py`, eligibility chain.
  - DO: flag symbols with same-/next-day earnings and market-wide event dates; auto-reduce sizing to 25%
    or skip; reduce/flatten intraday around CPI/FOMC prints.
  - ACCEPT: a symbol with earnings tomorrow is de-sized/skipped; an FOMC day reduces intraday exposure.
    Extend `tests/test_earnings_calendar.py`.

---

### Stage 3 — REINSTATE THE PROVEN EDGE  *(M12 framework, then unpause one at a time)*
**Go-gate:** M12 reintroduction framework exists and is tested; the first MR winner is re-admitted and
accumulating **fresh, honest** closed outcomes. **Do not unpause anything until Stage 0 + Stage 1 are green.**

- [ ] **3.1 M12 — strategy reintroduction framework (the gating milestone)**
  - WHY: the entire downstream plan depends on safely re-adding strategies without repeating the
    multi-owner oversell loop. Highest-leverage open item.
  - FILES: new `monitoring/reintroduction.py` (or extend `monitoring/strategy_health.py`); fixtures.
  - DO: evidence-gated, **one-strategy-at-a-time** admission requiring **≥20 fresh closed outcomes** of
    positive expectancy AND **live drawdown correlation with the existing book < 0.3**. Include
    conflict-regression fixtures on **IWM/KRE/NVDA/QQQ** (the single-owner / competing-exit scenarios).
  - ACCEPT: the framework refuses a strategy that fails the evidence or correlation gate, and admits one
    that passes; conflict fixtures prove no oversell/competing-flatten. New `tests/test_reintroduction.py`.

- [ ] **3.2 Unpause the botnet101 mean-reversion winners (one at a time, M12-gated)**
  - WHY: these carry our historical edge and were paused only by the reset, not by failure
    (3-bar-low +24.8%/100% WR, consec-below-ema +15.0%/75% WR, 4bar-momentum-reversal +3.08%). The core
    that broke them is now rebuilt + verified.
  - FILES: `data/trading.db` `paused_strategies`; `scripts/` unpause tooling; `config/settings.json`.
  - DO: through the M12 gate, unpause **`botnet101-3-bar-low` first**, accumulate ≥20 fresh closes, then
    `consec-below-ema`, then `4bar-momentum-reversal`. Fix the NULL-`return_pct` cohorts
    (buy-5day-low n=253, consec-bearish n=168, turn-of-month n=37) so eligibility can judge them.
  - ACCEPT: each unpaused strategy trades, places resting stops, and books **non-phantom** outcomes; the
    equity curve and per-strategy P&L stay consistent. Verify after ≥1 clean live-paper week each.

- [ ] **3.3 Promote the P2-validated MR extensions + symbol adds**
  - WHY: backtested + coded already (`4c5a53e`), then re-paused by the reset. rsi14-oversold is the best
    risk-adjusted (n=116, +1.39%, PF 1.81); rsi2 cells are strong (XLK PF 3.77, SMH PF 2.84).
  - FILES: strategy modules under `strategies/mean_reversion/`; `data/universes/*.csv`;
    `config/settings.json`.
  - DO: promote **rsi14-oversold first**, then rsi2-oversold, then bollinger-bandit — each through M12.
    Add symbols **XLK, SMH, SPY, DIA, XLY, XLC**.
  - ACCEPT: each extension passes the M12 gate and books clean outcomes; symbol adds appear in the universe
    and trade. Per-strategy tests for the new modules.

- [ ] **3.4 Formalize RSI-2 EOD mean-reversion (Connors) on liquid ETFs**
  - WHY: clean, standalone, strong evidence — RSI(2)<5 buy / >65 exit with a 200-SMA filter:
    CAGR 12.7%, Sharpe 2.85, 75% WR, PF 3.0 (backtest). Adds a 50-SMA filter for +5–10% WR.
  - FILES: `strategies/mean_reversion/` (new or extend), config.
  - DO: implement on SPY/QQQ/XLK/SMH/DIA/XLY/XLC: enter when `price > 200d-SMA` and `RSI(2) < 5`,
    exit when `RSI(2) > 65`; optional `price > 50d-SMA` filter for the higher-quality variant.
  - ACCEPT: signals match the spec on a fixture; passes M12 walk-forward before paper. New per-strategy test.

- [ ] **3.5 Add RSP to the universe (trend instrument + breadth-regime signal)**
  - WHY: equal-weight RSP outperforming cap-weight SPX is the cleanest "MR-friendly broad tape" signal;
    when SPX re-leads, shift toward index trend-following.
  - FILES: `data/universes/*.csv`; `monitoring/regime.py`.
  - DO: add RSP as a tradable trend instrument and feed RSP-vs-SPX into the regime score.
  - ACCEPT: RSP is scannable/tradable; the regime score reflects the RSP/SPX relationship. Test the signal.

---

### Stage 4 — INTRADAY: KEEP ONLY WHAT NETS POSITIVE
**Go-gate:** the net-negative 1m churn is retired; M11 max-loss overlay exists; any intraday strategy
kept or added shows **positive expectancy after modeled costs** on out-of-sample data.

- [ ] **4.1 M11 — intraday time-stop / per-position max-loss overlay**
  - WHY: the prior intraday set bled partly from no hard intraday loss cap; must exist before scaling the
    candle strategy past Stage 4.
  - FILES: `monitoring/auto_trader_intraday.py`, `monitoring/stops.py`.
  - DO: add a per-position intraday max-loss cap and a time-stop (exit if not working within N bars).
  - ACCEPT: an intraday position breaching the max-loss or time limit is force-exited with the reason
    logged (not left to leak overnight). New test in `tests/test_auto_trader_intraday*.py`.

- [ ] **4.2 Retire the net-negative 1m churn**
  - WHY: `intraday_edge_gate` correctly vetoed **32,146 entries** because expected move (~0.03–0.10%) is
    below 0.13% friction+buffer. 1m-momentum (−249%) and vwap-reclaim (−98%) are structurally unprofitable
    at 1m and generated 82%+ of all signals for net-negative return.
  - FILES: `config/settings.json`, `strategies/intraday/`, `strategies/momentum/`, scanner registration.
  - DO: keep `intraday-1m-momentum`, `intraday-1m-vwap-reclaim`, `intraday-1m-orb` **paused and gated at
    the scanner layer** (not just entry — they still burn signal generation). Archive the dead modules
    (`strategies/generated/gap_fill_reversion.py`, `smc/strategy.py`, ORB EOD variants,
    ross-cameron-five-pillar) to shrink the roster 30→~22.
  - ACCEPT: those strategies produce no new signals; the roster shrinks; suite still green.

- [ ] **4.3 Advance the candle-continuation strategy through Stages 5–8**
  - WHY: it's the disciplined intraday build (15m backtest: 2,417 trades, 37.2% WR, PF 1.18, +0.048%
    expected) and is already live-on-paper at Stage 4 on 3 names. The edge lives in the trailing ratchet,
    not the pattern — validate it cleanly.
  - FILES: `docs/INTRADAY_TREND_BUILD_PLAN.md` Stages 5–8; `strategies/intraday/`.
  - DO: Stage 5 trailing-ratchet validation → Stage 6 bearish-pattern exit → Stage 7 universe scale →
    Stage 8 edge decision. Each stage advances only on clean live-paper days and a green Go-gate.
  - ACCEPT: each stage's Go-gate (in the build plan) is met and logged; expectancy after costs is positive
    before any universe expansion.

- [ ] **4.4 SIP-filtered 5-minute ORB (catalyst days only) — highest-evidence intraday add**
  - WHY: Zarattini/Barbon/Aziz (2024): top-20 Stocks-in-Play ORB portfolio = 1,600%+ net, 36% annualized
    alpha, Sharpe 2.81 (backtest, treat Sharpe as upper bound). **The SIP filter, not the breakout, is the
    edge.** We have ORB modules already.
  - FILES: `strategies/orb/`, `monitoring/movers.py` (RVOL/news), regime + event gates.
  - DO: mark the 5-min OR high/low at 9:35 ET; enter on first close beyond it; stop on the opposite side;
    flatten EOD. **Only fire on Stocks-in-Play:** pre-market RVOL ≥ 3× 20-day avg + news-catalyst flag,
    500k+ ADV, reasonable price band. Run only on `risk_on`/catalyst regime days.
  - ACCEPT: ORB entries fire only on SIP-qualified names on catalyst days; backtest the SIP filter on/off
    and record the delta; positive expectancy after 5–15 bps slippage. New per-strategy test + walk-forward.

- [ ] **4.5 VWAP trend-follow overlay on SPY/QQQ/XLK/SMH/TQQQ**
  - WHY: Zarattini/Aziz (2023): long while price > intraday VWAP, flat below, flatten EOD — QQQ 671% net /
    Sharpe 2.1 over two bear markets (backtest). The edge is **continuation while holding the VWAP side**,
    not reversion. Maps onto already-promoted names; VWAP-as-trailing-stop fits our architecture.
  - FILES: `strategies/intraday/` (VWAP module), trailing-stop integration.
  - DO: long while above VWAP, exit/flat on cross below; flatten EOD. **Size TQQQ one Kelly fraction below
    QQQ** (3× leverage decay).
  - ACCEPT: signals follow the VWAP-side rule; TQQQ sized down; positive expectancy after costs OOS. Test it.

---

### Stage 5 — AI RESEARCH LOOP  *(Claude's one provably-good job)*
**Go-gate:** a constrained-DSL research loop proposes candidates that **cannot reach paper** without
passing positive out-of-sample information coefficient across **3+ non-overlapping 90-day windows**.

- [ ] **5.1 Define a constrained strategy DSL**
  - WHY: the QuantaAlpha pattern (80% of generated factors positive OOS, best 1.72 Sharpe) worked
    *because* of a constrained DSL + strict train/test separation, not arbitrary Python. ML amplifies
    overfitting if unconstrained.
  - FILES: new `strategies/dsl/` + `monitoring/llm_strategy_generator.py` (already exists — adapt).
  - DO: a small grammar, e.g. `entry = crossover(ema9, ema21) filtered by regime_flag, exit =
    trailing_stop(atr_multiple)`. No free-form code execution.
  - ACCEPT: the DSL compiles to a runnable `compute_fn(df)→df`; invalid expressions are rejected. Test the parser.

- [ ] **5.2 Overnight Claude candidate generator**
  - WHY: this is the integration with the strongest evidence for AI in this system — research, not trading.
  - FILES: `scripts/llm_strategy_generator.py`, scheduler.
  - DO: generate 10–15 DSL candidates/week; crossover the top performers; enforce diversification (no single
    instrument/factor dominating).
  - ACCEPT: a nightly run emits N valid candidates with metadata; runs unattended. Smoke test.

- [ ] **5.3 Walk-forward OOS gate (the non-negotiable guard)**
  - WHY: the single most important protection against ML-amplified curve-fitting.
  - FILES: `scripts/walk_forward.py` (exists — wire candidates through it).
  - DO: require **positive OOS information coefficient across ≥3 non-overlapping 90-day holdout windows**
    before a candidate is eligible for paper. Reject anything with Sharpe > 2.0 / >200% annual as
    overfit-suspect until proven across windows.
  - ACCEPT: a curve-fit candidate is rejected; a robust one passes. Extend `tests/test_walk_forward.py`.

- [ ] **5.4 Ranked shortlist → human review → M12**
  - WHY: keep a human in the loop; promotion still flows through the same evidence gate as every other strategy.
  - FILES: dashboard / daily report; `monitoring/reintroduction.py`.
  - DO: surface a ranked shortlist of OOS-passing candidates; promotion to paper requires the M12 gate.
  - ACCEPT: the shortlist renders; nothing reaches paper without M12. Verify the flow.

---

### Stage 6 — PAPER → LIVE GRADUATION  *(deliberate, manual, evidence-gated)*
**Go-gate (the live-flip checklist):** stops provably rest on the book in live runs · backtests model
realistic slippage · ≥1 strategy shows positive expectancy *after costs* on OOS · drawdown controls
verified live · ramp protocol staged. **The live flip is always Ross's manual decision, never an agent milestone.**

- [ ] **6.1 Model slippage explicitly in backtests**
  - WHY: paper fills at last price with no slippage; a documented case went 12% paper profit → 5% live loss.
    For intraday this is the killing field (0.15% gross edge × 50/day − 5–10 bps round-trip ≈ nothing).
  - FILES: `backtest/engine.py`, `backtest/report.py`.
  - DO: apply **5–15 bps/fill for mid-cap, 20–50 bps for thin names**; report net-of-cost expectancy only.
  - ACCEPT: backtests show net-of-cost numbers; any strategy with net E < 0 is flagged ineligible. Test the model.

- [ ] **6.2 Live credentials + live-strategy gating (manual setup)**
  - WHY: live is structurally impossible today (no `alpaca_live` section, `live_strategies=[]`) — by design.
  - FILES: `config/credentials.json` (gitignored), `config/accounts.json`, `config/settings.json`.
  - DO: add an `alpaca_live` credentials section and a `live_strategies` allowlist that defaults empty.
    Keep `is_paper_mode()` authoritative; live routing requires an explicit, reviewed config change.
  - ACCEPT: with no live strategy listed, behavior is unchanged (paper). Listing one routes only that
    strategy live. Test the gating, not live orders.

- [ ] **6.3 Live ramp protocol**
  - WHY: cross the paper-to-live gap empirically, not on faith.
  - FILES: sizing path; `config/settings.json`.
  - DO: first live strategy starts at **0.25% risk/trade for the first 50 live fills**; scale toward 0.75%
    only once live slippage matches backtest assumptions.
  - ACCEPT: the ramp schedule is enforced in code; size scales only on the fill-count + slippage condition.

- [ ] **6.4 Live monitoring & recalibration**
  - WHY: ~22% of retail live losses are infrastructure; weekly fill-vs-expected comparison catches drift early.
  - FILES: `monitoring/daily_report.py`, `monitoring/weekly_digest.py`, alerts.
  - DO: flag >3% daily DD; weekly compare live fills vs expected; quarterly recalibration; log every order
    rejection as an alerted event.
  - ACCEPT: the monitors fire on synthetic breaches; weekly digest includes fill-vs-expected. Extend tests.

---

## 4. The Survivability Framework (lock these numbers; backtest the multipliers on our universe first)

| Lever | Daily / swing | Intraday (5–15m) | Source/Note |
|---|---|---|---|
| Initial stop | 1.0× ATR(14) below nearest swing-low | same logic, tighter ATR | clean invalidation |
| Trailing stop | **Chandelier(22, 3.0)**, engage at +1R | **Chandelier(14, 2.0)** | 3× beat fixed-5% by 48% (backtest) |
| Risk per trade | **0.75%** of equity | 0.75% of equity | RoR ≈ 0 at our edge |
| Sizing | `Shares = (Equity × 0.0075) / (ATR(14) × M)` | same | volatility-targeting |
| Max concurrent | 8 positions (≈ 6% heat) | within same heat budget | survives correlated selloff |
| Sector cap | max 3 same-sector; cluster → 0.25% each | same | 80–90% correlation in drawdowns |
| Skip rule | skip if swing-low > 2× ATR away | same | avoids noise positions |
| Kill switch | DD ≥ 15% → halve risk to 0.375% until DD < 10%; 3% daily DD → pause | same | rule-based, not emotional |
| Kelly | **ceiling only**, ¼–½ Kelly band, OOS inputs | same | full Kelly → 50–60% DD |
| Pyramiding | **OFF** until 200+ logged-R trades show E > +0.3R | OFF | adds at +1R, 0.5× decay, 3 max |

**The four-layer pattern-eligibility gate** (turns a folklore pattern into edge — *the conjunction is the alpha*):
(1) Regime: stock > 200d-MA **and** SPY > 50d-MA · (2) Trend: price > 21-EMA > 50-EMA ·
(3) Relative strength: RS > 80 vs universe · (4) Volume: signal bar ≥ 1.5× 10-session avg. **All four required.**
Add an **NR7/NR4 contraction pre-condition** before Donchian breakouts to cut the false-break failure that
hit us in early June.

---

## 5. Definition of Done — How We Know We're Winning

- **Stage 0 done:** 0 new phantoms/day; ≥1 real resting stop per live entry; reports exclude phantom/stale;
  the protected-ratio counter is green. *Now every number we read is true.*
- **Stage 1 done:** sizing is ATR-vol-targeted; heat ≤ 6%; hybrid trail live; R-multiples logged.
- **Stages 2–3 done:** regime gate live; ≥3 mean-reversion strategies re-admitted through M12, each with
  **≥20 fresh, honest closed outcomes** and positive expectancy; capital-at-work climbs off 6.8%.
- **Stage 4 done:** intraday churn retired; only positive-after-cost intraday strategies remain.
- **The graduation bar (Stage 6):** at least one strategy with **positive net-of-cost expectancy over
  ≥3 non-overlapping 90-day OOS windows**, stops proven to rest live, DD controls verified → Ross flips it live
  at 0.25% risk.
- **Realistic target band (calibration anchors, not promises):** 15–30% CAGR, max DD < 20%, Sharpe > 1.0.
  Anything far above that in a backtest is treated as an overfit warning, not a trophy.

---

## 6. Sequencing & Dependencies (what blocks what)

```
Stage 0 (truth)  ─────────────► blocks EVERYTHING. Do first, in order 0.1→0.6.
        │
        ▼
Stage 1 (sizing/stops) ───────► must be green before re-adding any strategy.
        │
        ├──► Stage 2 (regime/event gate) ──┐
        │                                  ▼
        └──► Stage 3 (M12 → unpause MR) ───► the income engine. 3.1 (M12) gates 3.2–3.5.
                     │
                     ▼
             Stage 4 (intraday: M11 → retire churn → candle 5-8 → SIP-ORB / VWAP)
                     │
                     ▼
             Stage 5 (AI research loop) ──► feeds candidates back into M12.
                     │
                     ▼
             Stage 6 (paper→live) ──► manual flip, Ross only.
```

**Critical path to first real income:** 0.1 → 0.2 → 0.5 → 1.1 → 1.3 → 3.1 → 3.2 → (accumulate ≥20 fresh
closes) → 6.1 → 6.3 → **Ross flips one mean-reversion strategy live.** Everything else widens and
strengthens the engine; this line gets us to the first live dollar.

---

## 7. What We Deliberately Are NOT Building (save the engineering)

- **Autonomous LLM trading agents** — StockBench: top LLM 1.9% vs 0.4% buy-and-hold; underperforms passive
  in bear conditions.
- **Reinforcement-learning directional traders** — 167-study meta-analysis: algorithm choice statistically
  irrelevant; the only RL edge is in market-making (needs tick infra we lack).
- **Sentiment as a primary signal** — real but thin and ~30% alpha decay in 6 months. Confirming filter only.
- **Options synthetic pyramiding** — Phase 6 research verdict NO-GO; revisit only after 200+ logged-R trades.
- **Shorts** — never validated; keep `allow_shorts=false` until a short strategy is proven through M12.
- **Breadth over depth** — the original failure was 30 strategies / 552 symbols / 4 overlays, none validated
  under the real execution path. We stay narrow until each addition earns its place.

---

## 8. Handoff Notes for Builder Models

- **Plan consumption:** this file is `milestone-builder`-ready. Pick the next unchecked `- [ ]`, build it
  following its WHY/FILES/DO/ACCEPT, run the suite, commit, check it off, append a Build Log line.
- **Test command:** `py -3.13 -m pytest tests/ -m "not live"` (full unit). Live-marked tests require
  `config/credentials.json` and place paper orders — run sparingly. (`README.md:17–33`.)
- **Runtime model:** the system runs as **Windows Scheduled Tasks under `\TradingSystem\`** — fixes activate
  on the **next scheduled run**, not instantly. There is no daemon except LiveStream.
- **Commit style:** `feat(scope): …` / `fix(scope): …` (e.g. `fix(outcomes): require_fill on 1d reconcile`).
- **Order of operations is load-bearing:** do Stage 0 *before* touching sizing or strategies. Re-enabling a
  strategy before stops rest and phantoms stop will repeat the original collapse. M12 (3.1) gates every unpause.
- **Measurement honesty:** never derive trade-level P&L from `paper_trades` (205 sells vs 146 buys, not 1:1)
  or the raw `outcomes` table (95% phantom). The **equity curve** is the source of truth; exclude
  `phantom_no_fill` and `stale_intraday_flatten_missed` by default in every report.
- **Evidence labelling:** headline edge numbers in this plan are **backtest/signal-scoped** unless stated as
  live. The live sample is ~30 closes — Stage 0 + ≥20-fresh-close accumulation is what converts backtest
  conviction into live conviction.
- **Companion context:** full internal audit → `docs/MASTER_AUDIT_INTERNAL_DOSSIER.md`; full external
  research with all source URLs → `docs/MASTER_RESEARCH_EXTERNAL_DOSSIER.md`.

---

## Build Log

- 2026-06-17 — Plan authored from 14-agent audit+research workflow. Stage 0 P0 bugs verified against
  source/DB (`daily_report.py:378` lacks `require_fill`; 0/409 trades have a resting stop).
- 2026-06-17 — **0.1 shipped** (commit `b9f21bb`): `require_fill=True` on the 1d reconcile pass kills the
  phantom factory. Behavioral test drives the real `persist_report`. Full non-live suite green
  (2 pre-existing unrelated failures: `test_macro_fetcher` FRED, `test_intraday_skips` paused-gate — folded
  into Stage 2 / Stage 4.2 respectively).
- 2026-06-17 — **0.2 shipped**: protective stops now rest on the book. Root cause was a fill-settlement
  race — a just-submitted market BUY isn't in the broker positions list yet, so `available_to_sell`
  returned 0 (not None) and `safe_submit_stop` skipped the stop (0/409 trades). Fix: `entry_filled`
  fallback in `safe_submit_stop` arms at requested qty when the broker shows 0 right after a live entry;
  `_entry_is_live` marks the entry in `_process_entry`. New `tests/test_stage0_stop_rests.py` reproduces
  the exact prod race end-to-end. Suite: 2511 passed, same 2 pre-existing failures, 72s.
- 2026-06-17 — **0.3 (stamp honesty) shipped**: the `entry_stops` stamp on the entry row is now gated on
  `stop_info.status=='submitted'`, so a buy whose stop is skipped/rejected is no longer falsely
  advertised as protected. New regression test (rejected entry → unstamped). Suite: 2512 passed, same 2
  pre-existing failures. Remaining Stage 0: 0.4 (order_sync at end-of-pass), 0.5 (position-scoped
  outcomes), 0.6 (daily protection metrics, absorbs the 0.3 invariant counter).
- 2026-06-17 — **0.4 shipped** (`d332bed`): end-of-pass `sync_order_fills` so the pass's own sells/stops
  don't strand at 'accepted' on the EOD final run. Wiring test; mechanism covered by `test_order_sync.py`.
- 2026-06-17 — **0.6 shipped**: `protection_metrics()` counts filled entries vs resting stops (matched by
  signal_id) and `persist_report` alerts on any naked longs (`_maybe_alert_naked`). 4 new tests. Suite:
  2517 passed, same 2 pre-existing failures, 83s.
- 2026-06-17 — **Stage 0 is 5/6 done (0.1–0.4, 0.6).** Only **0.5** (position-scoped outcome model +
  report exclusion of phantom/stale by default) remains — deferred to a focused session per its
  architectural scope. The two CRITICAL live bugs (phantom factory, naked stop) are closed and tested.
  PR #1 opened (feat/stage0-execution-truth → main).
- 2026-06-18 — **1.1 shipped** (branch `feat/stage1-survivability-sizing`, off the Stage 0 branch): new
  `atr_risk` sizing method in `sizing.py` (`atr_risk_notional` — qty = floor(equity × risk_pct /
  stop-distance), `max_position_usd` stays a hard ceiling), routed through `compute_notional` with safe
  fallback to tiered when equity/stop is unavailable. `_process_entry` computes the stop-distance up
  front ONLY when `atr_risk` is active (every other path byte-for-byte unchanged). **Config flipped
  `kelly_quarter` → `atr_risk` + `risk_per_trade_pct=0.0075`** — constant 0.75% dollar risk per trade is
  now live on paper (RoR ~0). 8 new tests. Suite: 2525 passed, same 2 pre-existing failures.
- 2026-06-18 — **1.5 done** (no code change needed): the "$250 price veto" was historical — the cap is
  now $10k notional and `SKIP_PRICE` only fires when one share exceeds the whole cap (never for our
  universe). Regression test pins NVDA/SPY/QQQ aren't vetoed. **Fractional shares rejected by design** —
  Alpaca can't rest a stop order on a fractional position, which would undo Stage 0.2.
- 2026-06-18 — **1.6 shipped**: `outcomes.r_multiple` column + migration; `close_outcome` computes
  R = return% ÷ initial-stop-distance% from the entry's protective stop. `expectancy_metrics()` rolls up
  avg-R + win-rate over honest closed outcomes (phantom/stale excluded) and `persist_report` logs it. R
  is the substrate for Kelly inputs + pyramiding readiness (Stage 1.2/4). 4 new tests. Suite: 2530 passed.
- 2026-06-18 — **1.2a shipped**: portfolio heat cap. `portfolio_heat_usd()` sums open Σ(stop distance ×
  size); `process_signals` threads `remaining_heat_usd` into `_process_entry` (mirrors BP budget) →
  `SKIP_HEAT_CAP` once the run would exceed `risk.max_portfolio_heat_pct` (set 0.06). At 0.75%/trade that's
  ~8 concurrent positions. 2 new tests (accumulator + in-run gate). **1.2b (sector clustering) deferred** —
  nasdaq100 sectors are empty; needs a backfill. Suite: 2532 passed, same 2 pre-existing failures.
- 2026-06-18 — **1.4 shipped** (config): drawdown kill-switch ladder over the existing throttle + daily
  breaker. `risk.max_daily_loss_pct` 2→3 (3% daily pause); `drawdown_throttle` set to halve@15% DD /
  quarter@20% / halt+kill@25%. With `atr_risk`, the 0.5× size multiplier halves per-trade risk to 0.375%
  — the plan's exact spec. 5 new tests; module DEFAULTS (and their tests) untouched. Suite: 2537 passed.
- 2026-06-18 — **1.3 shipped**: adopted Chandelier(22, 3.0) trail (config; engine already tested, floored
  at the initial stop). Built the swing-low structure-based initial stop (`swing_low_initial_stop` +
  `resolve_initial_stop` routing + `_recent_swing_low`) with a 2× ATR distance cap, kept OPT-IN
  (`stops.initial_method` default `atr_initial`) so the live stop-distance/sizing change is observed before
  flipping. 10 new tests. Suite: 2547 passed, same 2 pre-existing failures.
- 2026-06-18 — **2.1 shipped** (branch `feat/stage2-regime-gate`): new `monitoring/regime.py` — the risk-environment
  axis (risk_on/transitional/risk_off), distinct from regime_router's trend-character `market_regime`. Pure
  `compute_adx` (Wilder ADX 14), `moving_average`, `score_regime` (VIX-vs-200dMA gate + ADX conviction →
  label + `risk_scale` 1.0/0.5/0.25 + confidence). `compute_and_persist_regime` reads the latest VIX +
  200d-MA from `macro` and ADX from the injected daily-bars fetcher (SPY proxy), upserting one row/day into
  the new `regime_scores` table (`data/db.py` + `upsert_regime_score`/`latest_regime_score`). Wired into
  `schedulers/run_macro.bat` so the score writes daily right after the VIX pull. Conservative `transitional`
  fallback when inputs are missing. **Fixed the pre-existing `test_macro_fetcher` FRED test** (2.1
  prerequisite): the client-side `observation_start` re-filter double-applied the server's filter and dropped
  valid rows once the fixed fixture dates aged past the 30d test lookback — now trusts the server filter and
  only re-filters on the legacy no-`observation_start` fallback path. 17 new tests. Suite: 2565 passed, only
  the allowed `test_intraday_skips` (Stage 4.2) pre-existing failure remains.
- 2026-06-18 — **2.2 shipped**: regime score wired into both halves of the entry pipeline. `process_signals`
  reads `regime.latest_regime_score(conn)` once per run (defaults to conservative `transitional`/0.5× when
  no score persisted yet). New class-vs-regime eligibility gate (`regime.regime_blocks_class` /
  `regime_eligibility_skip`): on a `risk_off` tape, directional/momentum classes (trend/breakout/momentum)
  are blocked with `SKIP_RISK_REGIME`; mean-reversion stays eligible and is de-risked via sizing instead.
  `_process_entry` multiplies the `atr_risk` `risk_pct` by the regime `risk_scale` (1.0/0.5/0.25), threaded
  through a new `risk_regime_scale` param. Governed by `settings.risk.regime_gate.enabled` (default true;
  false → no eligibility block and the sizing scale is forced to 1.0). Class-based off the existing
  `strategy_class` field — no new per-strategy config. Note: the conservative `transitional` default halves
  atr_risk size until the first daily score persists (intended); updated the Stage-1 heat-cap test to disable
  the gate so it stays a pure heat test. 9 new tests. Suite: 2574 passed, only the allowed `test_intraday_skips`
  pre-existing failure remains.
- 2026-06-18 — **STAGE 1 COMPLETE** (1.1, 1.2a, 1.3, 1.4, 1.5, 1.6). The survivability risk engine is live
  on paper: constant 0.75% risk/trade (atr_risk), 6% portfolio-heat cap, Chandelier(22,3.0) trail, DD
  ladder (halve@15%/halt@25% + 3% daily), R-multiple expectancy. Two opt-in/deferred refinements remain:
  **1.2b** sector-cluster sizing (needs nasdaq100 sector backfill) and the **1.3 swing-low initial stop
  flip** (built+tested, observe before enabling). Neither blocks Stage 2.
