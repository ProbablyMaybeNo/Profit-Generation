# Profit Generation full audit and implementation roadmap

Date: 2026-06-06
Mode: READ-ONLY AUDIT. No broker API calls, no paper orders, no live orders, no code/config/cron changes.
Project root: `/mnt/d/AI-Workstation/Antigravity/apps/Profit Generation`

## 1. Executive verdict

Verdict: **rebuild-needed, but salvageable**.

The system is not ready for broad paper validation. It has a useful research/reporting core and recent execution-authority work exists in code/tests, but the live database still shows the old failure pattern: multiple strategies and intraday variants claim the same broker symbols, stale cleanup outcomes dominate several strategy stats, and reporting/accounting migrations are not fully reflected in the live DB.

Paper trading here is engineering validation only, not proof of edge. The next real milestone is not “make more strategies trade”; it is **prove one Donchian-only paper lifecycle end-to-end** with broker, DB, open orders, stops, outcomes, and report all agreeing.

Status labels:
- `safe-now`: source review, DB read-only analysis, local non-live tests after inspecting them, report generation.
- `approval-needed`: broker account reads, paper order validation, Windows Task Scheduler changes, Hermes cron changes, external report delivery.
- `do-not-build-yet`: live trading, live broker cron, autonomous live orders, adding more active strategies before order authority is production-proven.

## 2. Audit method

Inspected:
- Project docs and sprint plans: README, scheduler docs, system audits, optimization sprint plans.
- Scheduler scripts and scheduler docs.
- Key execution modules: `monitoring/auto_trader.py`, `monitoring/position_manager.py`, `monitoring/close_intraday_positions.py`, `monitoring/stops.py`, `monitoring/trailing_stops.py` via searches and targeted reads.
- DB/schema code: `data/db.py`, `config/utils.py`, reporting query paths.
- Strategy wrappers/packages and strategy registry evidence.
- Tests inventory and likely dangerous test classes.
- SQLite DB `data/trading.db` opened read-only via URI `mode=ro&immutable=1`.

Not done:
- No broker calls.
- No paper or live orders.
- No test execution, because several tests touch broker/API paths and the task was read-only audit.
- No credential file reads.

## 3. Project map

### Core config and credentials

| Path | Purpose | Risk |
|---|---|---|
| `config/settings.json` | Non-secret runtime settings. Shows `paper_trading=true`, `auto_trade.enabled=true`, `dry_run=false`, intraday enabled, risk caps, stops, Kelly, trailing-stop policy. | High operational risk: paper orders can fire when schedulers run. |
| `config/utils.py` | Loads credentials/settings; builds Alpaca `TradingClient`; account summary; market clock; state saves. | Broker read/write gateway. Do not call casually. |
| `config/credentials.json` | Credential store, gitignored. | Secret; not read in this audit. |
| `config/kill_switch.json` | New-entry halt mechanism per README. | Safety-critical. |

### DB/data

| Path | Purpose | Notes |
|---|---|---|
| `data/trading.db` | Main trading SQLite DB. | 46,111 signals, 2,492 outcomes, 380 paper trades, 476 equity snapshots. |
| `data/cache.db` | Cache DB. | Present; not deeply inspected. |
| `data/tradingview_strategies.db` | TradingView strategy/source DB. | Present; not deeply inspected. |
| `data/db.py` | DDL, migrations, signal/outcome/trade/equity writers. | Has additive schema code for long/short market value, but live DB schema currently lacks those columns. |

### Signal and strategy evaluation

| Path | Purpose | Notes |
|---|---|---|
| `monitoring/strategy_fires.py` | EOD strategy fire detection. | Historically 1d outcome/strategy path. |
| `monitoring/intraday_fires.py` | Intraday scan/fire recording. | Sprint 3 docs say exit recording now owner-gated. DB still contains old 1m exit spam. |
| `monitoring/intraday_monitor.py` | Scheduled intraday monitor entrypoint per scheduler docs. | Broker/data API risk. |
| `strategies/generated/*.py` | Generated strategy compute functions: RSI, Bollinger, etc. | Several strategies are research/demo grade. |
| `strategies/trend/*` | Trend strategy package/wrappers. | Donchian and MA cross live candidates. |
| `strategies/breakout/*` | Breakout package/wrappers. | ORB/ORBO and Donchian retest variants. |

### Execution and order management

| Path | Purpose | Side effects |
|---|---|---|
| `monitoring/auto_trader.py` | Main signal -> eligibility -> sizing -> broker order -> DB trade/outcome/report flow. | Submits paper/live orders depending strategy routing. |
| `monitoring/position_manager.py` | Recent authority layer: broker-state reconciler helpers, run reservation ledger, `safe_submit_sell`, `safe_submit_stop`, owner helpers, stop verification. | Broker reads and order submit/cancel wrappers. |
| `monitoring/stops.py` | ATR stop calculation/submission and stop fill reconciliation. | Stop order submit path. |
| `monitoring/trailing_stops.py` | Trailing-stop state and exit decisions. | Exit trigger path. |
| `monitoring/close_intraday_positions.py` | EOD intraday flatten and flat assertion. | Broker cancel/sell path. |
| `scripts/flatten_unintended_shorts.py` | Guarded short-cover tool per Sprint 2. | Must remain dry-run unless explicitly approved. |

### Reporting/dashboard

| Path | Purpose | Notes |
|---|---|---|
| `monitoring/daily_report.py` | Daily report generation and outcome reconciliation. | May query broker/data APIs depending path. |
| `monitoring/_report_data.py` | Report data helpers. | Still has legacy deployed-capital formula using `max(0, (portfolio_value - cash)/portfolio_value*100)`. |
| `schedulers/pg_report_data.py` | Hermes/report script; Sprint 3 says it has fresh-vs-cleanup split and exposure fixes. | Reads DB, but may have schema mismatch with live DB. |
| `dashboard/server.py` | Flask dashboard, state, health, trading views. | Health may call Alpaca. |
| `dashboard/public_api.py` | Sanitized public API. | Must avoid account identifiers/secrets. |

### Schedulers and automation

| Path | Purpose | Risk |
|---|---|---|
| `schedulers/register_intraday.bat` / `run_intraday.bat` | Windows Task Scheduler intraday scan every 15 minutes. | Writes DB; may trigger paper execution path. |
| `schedulers/register_daily.bat` / `run_daily.bat` | Daily report / EOD routines. | Reconcile/report side effects. |
| `schedulers/register_reconcile.bat` | Nightly position reconciliation. | Broker read/write risk depending implementation. |
| `schedulers/register_telegram_listener.bat` | Telegram halt/resume controls. | External command surface. |
| `schedulers/register_crypto.bat` | 24/7 crypto scan. | Separate execution/data risk. |
| `schedulers/README.md` | Scheduler runbook. | Documents TradingView webhook and open-mode risk if no secret. |

## 4. Current architecture: data -> signal -> order -> broker -> DB -> report

Observed intended lifecycle:

1. Data ingestion:
   - EOD and intraday bars from yfinance / Alpaca / cached DB.
   - Macro/news/liquidity enrichment from FRED/Polygon/yfinance where configured.
   - Intraday bars cached in `intraday_bars`.

2. Signal generation:
   - Strategy compute functions emit `long_entry` / `long_exit`.
   - Signals are written to `signals` with `strategy_id`, `symbol`, `bar_interval`, `bar_ts`, `signal_type`, `close`, `extra_json`.
   - Intraday flows can emit high-volume signals; DB evidence still has massive 1m exit spam.

3. Eligibility and sizing:
   - `auto_trader` gates on settings, strategy pause state, kill switch, samples, recent outcome health, cool-down, earnings, sentiment, max positions, symbol caps, regime, edge/friction gates.
   - Sizing uses caps, tiering, and Kelly-quarter logic.

4. Order/execution:
   - Entries and exits route through `auto_trader._process_entry` and `_process_exit`.
   - Recent architecture adds `position_manager.safe_submit_sell` and `safe_submit_stop` for sell/stop idempotency.
   - `position_manager` also has owner helpers (`symbol_owner`, `owns_symbol`) and run-reservation ledger.
   - Stops are attached via `_maybe_attach_stop`; post-fill stop protection verification exists in code.

5. Broker:
   - Alpaca TradingClient is created by `config.utils.get_alpaca_client(live=False|True)`.
   - `settings.json` has `paper_trading=true`, but also `auto_trade.enabled=true` and `dry_run=false`, meaning scheduled paper order submission is possible.
   - `auto_trade.live_strategies=[]` means live routing should be empty by default, but the live path exists.

6. DB writes:
   - `paper_trades` stores order IDs, signal IDs, strategy/symbol/side/qty/order/status/fill/stop info.
   - `outcomes` stores entry/exit, return_pct, MFE/MAE, bars held, status.
   - `equity_snapshots` stores account snapshots.
   - `paused_strategies`, `intraday_skips`, overlays, trailing stops track gates/state.

7. Reporting:
   - Daily reports in DB (`daily_reports`) and scripts aggregate signals, outcomes, paper trades, portfolio snapshots.
   - Correct reporting must separate fresh trading from cleanup/reconciliation outcomes.
   - Correct exposure must use broker long/short market value or a clearly labelled fallback, not `(portfolio_value - cash)`.

## 5. SQLite DB/schema audit

Read-only target: `data/trading.db`.

DB files present in `data/`:
- `cache.db`
- `trading.db`
- `tradingview_strategies.db`

Main table row counts:

| Table | Rows |
|---|---:|
| `signals` | 46,111 |
| `outcomes` | 2,492 |
| `paper_trades` | 380 |
| `intraday_skips` | 225,323 |
| `intraday_bars` | 37,924 |
| `daily_reports` | 19 |
| `equity_snapshots` | 476 |
| `strategies` | 29 |
| `paused_strategies` | 7 |
| `trailing_stops` | 24 |
| `news` | 115 |
| `macro` | 790 |
| `liquidity_snapshots` | 538 |

Key schemas:

### `signals`
Columns: `id`, `ts`, `bar_ts`, `bar_interval`, `strategy_id`, `symbol`, `signal_type`, `close`, `extra_json`.

### `outcomes`
Columns: `signal_id`, `entry_ts`, `entry_price`, `exit_ts`, `exit_price`, `exit_reason`, `return_pct`, `mfe_pct`, `mae_pct`, `bars_held`, `status`, `updated_at`.

Important: `return_pct` is already a percent. Do not multiply by 100 in reports.

### `paper_trades`
Columns: `id`, `alpaca_order_id`, `signal_id`, `strategy_id`, `symbol`, `side`, `qty`, `order_type`, `limit_price`, `stop_price`, `submitted_at`, `filled_at`, `fill_price`, `status`, `notes`, `pyramid_tier`, `entry_stops`.

### `equity_snapshots`
Live DB columns observed: `recorded_at`, `portfolio_value`, `cash`, `equity`, `buying_power`, `source`.

Code now expects/adds `long_market_value` and `short_market_value`, but the live DB schema observed in read-only mode does **not** contain them. That means the M9 exposure fix has not been applied to this DB file yet or the audited DB copy is behind the code. This directly affects the negative-deployed-capital/short-exposure reporting fix.

Latest snapshots show cash far above portfolio/equity:
- `2026-06-05T19:58:18+00:00`: portfolio_value/equity `103421.33`, cash `197019.75`, buying_power `192435.63`, source `auto_trader`.

This makes the legacy deployment formula `(portfolio_value - cash) / portfolio_value` negative. Reporting must not use that as “capital deployed”.

## 6. Strategy inventory

DB has 29 strategies. Important active/promoted families include:

### Trend
- `trend-donchian-breakout-20`
  - Verdict in DB: `PROMOTED`.
  - Active symbols in DB record: `SPY`, `QQQ`, `IWM`.
  - Compute fn: `compute_donchian_breakout_20`.
  - Known posture: only strategy with recurring meaningful positive operational evidence in recent reports, but DB cleanup pollution means fresh stats must be isolated.
- `trend-ma-cross-20-50`
  - Verdict in DB: `PROMOTED`.
  - Active symbols: `SPY`, `QQQ`, `IWM`.
  - Current evidence weaker; docs cite large loser risk and regime/stop review need.
- `trend-new-high-volume`
  - Verdict in DB: `PROMOTED`.
  - Active symbols: `SPY`, `QQQ`, `IWM`.

### Breakout / ORB / ORBO
- `intraday-orbo-5m`: `SPY`, `QQQ`, `IWM`, `NVDA`, `TSLA`.
- `intraday-orb-pivots-5m`: `SPY`, `QQQ`, `IWM`, `NVDA`, `TSLA`.
- `intraday-1m-orb`: wide 20-symbol mega-cap/ETF universe.
- `breakout-donchian-retest-20`: `SPY`, `QQQ`, `IWM`.
- `breakout-donchian-retest-short-20`: `SPY`, `QQQ`, `IWM`.

### Intraday 1m
- `intraday-1m-momentum`: wide 20-symbol universe: `SPY`, `QQQ`, `IWM`, `XLK`, `SMH`, `XLE`, `XBI`, `KRE`, `GDX`, `AAPL`, `MSFT`, `NVDA`, `GOOGL`, `META`, `AMZN`, `TSLA`, `AMD`, `AVGO`, `NFLX`, `COIN`.
- `intraday-1m-vwap-reclaim`: same wide universe.
- `intraday-1m-orb`: same wide universe.

These overlap heavily and are the main source of symbol ownership conflict.

### Mean reversion
- `intraday-mr-3bar-low-15m`: `SPY`, `QQQ`, `IWM`.
- `rsi2-oversold`: active/present in DB; recent entries/open outcomes exist.
- `rsi14-oversold`: active/present in DB; docs previously flagged SMA200 lookback issue.
- `bollinger-bandit`: active/present in DB.

### Botnet101 variants
- `botnet101-3-bar-low`
- `botnet101-4bar-momentum-reversal`
- `botnet101-buy-5day-low`
- `botnet101-consec-below-ema`
- `botnet101-consec-bearish`
- `botnet101-turn-around-tuesday`
- `botnet101-turn-of-month`

Several show strong historical/outcome averages, but these are not automatically proof of tradable edge. They need fresh-vs-cleanup split, sample controls, slippage/cost checks, and ownership gates before paper expansion.

### Research/demo/fail strategies
- `tjr-smc-2025`: DB verdict `FAIL`; documentation says mechanical SMC/ICT version underperformed SPY and matches retail-latency concerns.
- `ross-cameron-five-pillar`: DB verdict `FAIL`; small-cap momentum universe may have volatility but mechanical execution failed under realistic slippage.
- Several Phase 2 demos (`inside-day-breakout`, `bollinger-bandit`, RSI variants) have mixed/weak verdicts and should not be activated blindly.

## 7. Strategy health evidence from DB/reports

### Signal volume, last 30 days

Top signal volumes:

| Strategy | Interval | Type | Count | Comment |
|---|---|---:|---:|---|
| `intraday-1m-momentum` | 1m | long_exit | 17,821 | Exit spam signature. |
| `intraday-1m-vwap-reclaim` | 1m | long_exit | 17,490 | Exit spam signature. |
| `intraday-1m-momentum` | 1m | long_entry | 2,505 | Heavy churn. |
| `intraday-1m-orb` | 1m | long_exit | 681 | Lower but still noisy. |
| `trend-donchian-breakout-20` | 1d | long_entry | 523 | EOD strategy, many signals. |
| `trend-donchian-breakout-20` | 1d | long_exit | 513 | EOD exits. |
| `trend-new-high-volume` | 1d | long_exit | 333 | Needs ownership gating. |
| `intraday-1m-vwap-reclaim` | 1m | long_entry | 325 | Heavy churn. |
| `intraday-mr-3bar-low-15m` | 15m | long_entry | 80 | Candidate but sample small. |
| `trend-ma-cross-20-50` | 1d | long_entry | 66 | Needs stop/regime review. |

### Outcome evidence

| Strategy | Interval | Closed n | Cleanup n | Avg fresh return pct | Verdict |
|---|---:|---:|---:|---:|---|
| `botnet101-consec-below-ema` | 1d | 567 | 0 | +1.4894 | Interesting, but paper-forward/ownership gate needed. |
| `botnet101-3-bar-low` | 1d | 394 | 2 | +2.2067 | Interesting, but not first priority. |
| `botnet101-4bar-momentum-reversal` | 1d | 361 | 0 | +2.0956 | Interesting, but not first priority. |
| `trend-donchian-breakout-20` | 1d | 291 | 261 | -7.7186 fresh avg in observed query | Cleanup dominates. Need fresh-only Donchian lifecycle validation before believing any stat. |
| `botnet101-buy-5day-low` | 1d | 245 | 0 | +0.3624 | Marginal after costs/slippage. |
| `botnet101-consec-bearish` | 1d | 168 | 0 | -0.0558 | Pause/observe. |
| `intraday-1m-momentum` | 1m | 60 | 54 | -0.4153 | Observe-only; do not re-enable. |
| `intraday-1m-vwap-reclaim` | 1m | 55 | 54 | -0.0799 | Observe-only; tiny fresh sample. |
| `trend-ma-cross-20-50` | 1d | 51 | 45 | -4.3739 | Do not expand; fix stop/regime or pause. |
| `intraday-1m-orb` | 1m | 39 | 34 | +0.1914 | Mostly cleanup; not credible. |
| `intraday-orb-pivots-5m` | 5m | 8 | 7 | -0.2070 | Not credible. |
| `intraday-orbo-5m` | 5m | 7 | 4 | -0.0500 | Not credible. |
| `intraday-mr-3bar-low-15m` | 15m | 6 | 5 | +4.0549 | One fresh close only; not credible. |

Closed outcome reasons:

| Exit reason | Count | Avg return pct | Interpretation |
|---|---:|---:|---|
| `long_exit_signal` | 1,875 | +1.3146 | Normal signal exits, but still needs strategy and freshness context. |
| `reconciled_no_position` | 359 | +1.8319 | Cleanup/reconciliation, not fresh trading performance. |
| `stale_intraday_flatten_missed` | 116 | -2.5472 | Operational failure cleanup; should never be counted as strategy edge. |
| `eod_close` | 10 | +0.0520 | EOD flatten. |
| `trailing_stop` | 2 | -1.0080 | Too little evidence; trailing plumbing not yet proven. |

Important contradiction: recent memory/docs say Donchian is the only credible recurring positive core, but current DB query shows Donchian closed outcomes are dominated by cleanup (`261` cleanup out of `291` closed). That does not disprove Donchian; it proves the DB cannot be trusted unless filtered to truly fresh broker-backed lifecycle rows.

## 8. Broker/execution audit

### What exists and is good

Recent Sprint 3 code/docs show a real attempt to fix the correct root cause:

- `position_manager.reset_run_reservations()` exists.
- `position_manager.safe_submit_sell()` exists.
- `position_manager.safe_submit_stop()` exists.
- `position_manager.symbol_owner()` and `owns_symbol()` exist.
- `position_manager.verify_fill_protected()` exists.
- `auto_trader._process_entry`, `_process_exit`, and `_maybe_attach_stop` are real submit paths.
- `close_intraday_positions.assert_intraday_flat()` exists.
- Search results show these helpers are wired into `auto_trader.py` and `close_intraday_positions.py` paths.

This is the right architecture direction.

### What remains unproven/dangerous

1. Production proof is missing.
   - Sprint 3 docs explicitly warn prior unit-green code was not on the real live oversell path.
   - The DB still contains the pre-fix behavior: heavy duplicate symbol ownership, filled sell stacks, and cleanup outcomes.
   - Need one controlled Donchian paper lifecycle to prove new paths work in actual scheduler/broker context.

2. Paper/live routing exists.
   - `get_alpaca_client(live=True)` can build a live client if `alpaca_live` is configured.
   - `auto_trade.live_strategies=[]` is currently safe by default, but this must remain empty until live is explicitly out of scope.

3. `settings.json` has `auto_trade.enabled=true`, `dry_run=false`.
   - That is acceptable for scheduled paper trading only after approval.
   - For audit mode, do not run order paths.

4. Multiple active strategies still share symbols.
   - Open/working DB paper-trade evidence shows several strategies have claimed the same symbols such as AAPL, AMD, AMZN, AVGO, COIN, GDX, GOOGL, IWM, KRE, NVDA, QQQ, SPY.
   - This confirms the known thesis: Alpaca has one net position per symbol; strategies acting independently cause conflicts.

## 9. Risk-system audit

### Current risk controls in settings/code

From `config/settings.json`:
- `paper_trading=true`.
- `risk.max_position_usd=10000`.
- `risk.max_daily_loss_pct=2.0`.
- `risk.max_open_positions=12`.
- `risk.max_open_per_strategy=5`.
- `risk.max_orders_per_day=100`.
- `risk.allow_shorts=false`.
- `auto_trade.max_new_entries_per_day=25`.
- `auto_trade.sizing_method=kelly_quarter`.
- `auto_trade.skip_intraday_signals=true` but `intraday_enabled=true`; this needs code-path confirmation before trusting it.
- Stops: ATR 14, multiplier 2.5, fixed fallback 5%, per-strategy Donchian-retest override 1.0.
- Trailing stop: ATR trail multiplier 3.0.
- Intraday min position floor: 800 USD.

### Risk weaknesses

1. Stop/trailing evidence is thin.
   - Only 2 closed outcomes with `trailing_stop` in DB.
   - Stop/trailing effectiveness cannot be treated as proven.

2. Pyramid logic is present but not strategically justified yet.
   - `paper_trades.pyramid_tier` exists.
   - Pyramiding should be disabled or ignored until base lifecycle + stop protection is proven.

3. Strategy-level gates exist but are only as good as clean outcomes.
   - If cleanup outcomes leak into health/eligibility, gates misclassify strategies.
   - Sprint 3 says M8 fixed cleanup exclusion in code; DB/report output still needs production verification.

4. Kill switch and pause logic exist, but pause must mean “no new entries and no silent holding”.
   - Sprint 3 says paused flatten policy exists.
   - Must prove in paper with broker/DB reconciliation.

5. Symbol caps are conceptually mandatory.
   - Current issue is not just position size; it is authority: who may trade/exit a symbol.

## 10. Reporting audit

### Good

- Daily reports are being written: latest DB reports from 2026-05-22 through 2026-06-05.
- `daily_reports` contains report date, regime, fires, watchlist, generated timestamp, markdown.
- `schedulers/pg_report_data.py` explicitly notes `return_pct is already a percent`.
- Fresh-vs-cleanup split exists in Sprint 3 plan and appears in report script code.

### Broken/unproven

1. Exposure/accounting migration mismatch.
   - Code has `long_market_value` / `short_market_value` fields.
   - Live DB `equity_snapshots` does not have those columns in read-only inspection.
   - Latest snapshots show cash > portfolio value, so legacy deployment formula goes negative.
   - `monitoring/_report_data.py` still contains `deployed_pct = max(0.0, (portfolio_value - cash) / portfolio_value * 100.0)` — it clamps but still uses the wrong proxy.

2. Fresh vs cleanup must be default everywhere.
   - Closed outcomes include 359 `reconciled_no_position` and 116 `stale_intraday_flatten_missed`.
   - These must be shown as operational cleanup, not strategy performance.

3. Date boundary risk.
   - Sprint 3 docs mention report split previously keyed on `updated_at`, which can roll EOD writes into next UTC date.
   - Correct date should use `COALESCE(exit_ts, updated_at)` where appropriate.

4. Dashboard/public APIs may still aggregate all closed outcomes unless filtered.
   - Search shows dashboard paths reading `o.return_pct` broadly.
   - They need audit before being used for strategy promotion decisions.

## 11. Test coverage audit

Tests exist and are broad. The repo has many targeted tests for exactly the current architecture:

Likely important safe/unit-ish tests after inspection:
- `tests/test_broker_truth_m1.py`
- `tests/test_owner_authority_m2.py`
- `tests/test_idempotent_stop_m3.py`
- `tests/test_exit_gating_m4.py`
- `tests/test_paused_flatten_m5.py`
- `tests/test_eod_flat_assert_m6.py`
- `tests/test_stop_protection_m7.py`
- `tests/test_perf_cleanup_split_m8.py`
- `tests/test_report_exposure_m9.py`
- `tests/test_position_manager.py`
- `tests/test_auto_trader.py`
- `tests/test_close_intraday_positions.py`
- `tests/test_stops.py`
- `tests/test_trailing_stops.py`
- `tests/test_pyramiding.py`
- `tests/test_intraday_edge_gate.py`
- `tests/test_expectancy_gate.py`

Dangerous/API/live tests to inspect/classify before running:
- `tests/test_alpaca.py`: contains `submit_order`, `get_alpaca_client()`, `TradingClient`, `StockHistoricalDataClient` markers/patterns; README says it hits real APIs.
- `tests/test_polygon.py`, `tests/test_fred.py`, `tests/test_yfinance.py`: real API/data-provider tests per README.
- `tests/test_setup_live_credentials.py`: live credential setup risk.
- `tests/test_get_alpaca_client.py`: client construction/routing risk.
- Any test marked `live` or importing real broker/data clients.

Do not run repo-wide `pytest` blindly. Preferred after inspection:
- `py -3.13 -m pytest tests/ -m "not live"`
- But only after confirming the `live` markers are correctly applied and broker submit tests use fakes/mocks.

## 12. Root-cause diagnosis

Root cause: **the system evolved as multiple independent strategies, but the broker only has one net position and one open-order book per symbol.**

That mismatch created these recurring failures:

1. Duplicate symbol ownership.
   - Same symbols are active across Donchian, MA cross, ORB/ORBO, 1m momentum, 1m VWAP, MR, and botnet variants.
   - DB open/working buy evidence shows multiple strategy claims per symbol.

2. Missing single execution authority.
   - Strategies historically submitted exits/stops/flattens as if they owned separate broker positions.
   - Broker rejects or fills based on net symbol quantity, not strategy metadata.

3. Order conflicts.
   - Existing sell/stop orders reserve shares.
   - New sells/stops collide with held quantity, causing wash-trade / insufficient-available-qty errors.

4. Intraday exit spam.
   - 17,821 `intraday-1m-momentum` long_exit signals and 17,490 `intraday-1m-vwap-reclaim` long_exit signals in 30 days are not healthy trading information.
   - They are state-gating failures or legacy residue.

5. Cleanup polluted strategy stats.
   - `reconciled_no_position` and `stale_intraday_flatten_missed` are operational events.
   - Counting them as fresh strategy performance creates false conclusions.

6. Reporting used the wrong exposure proxy.
   - Cash > portfolio/equity makes `(portfolio_value - cash)` negative.
   - Need broker long/short market value fields and loud long-only short alerts.

7. Fixes keep failing because they were implemented as local patches, not production-verified authority boundaries.
   - Sprint 3 rightly states every order-management milestone must drive the real production function, not a parallel unused helper.

## 13. Target architecture

### A. Broker-state reconciler

Broker positions and open orders are source of truth.

Responsibilities:
- Fetch broker positions and open orders once per cycle.
- Compute per-symbol:
  - broker quantity
  - open sell/stop quantity
  - in-run sell reservations
  - net available to sell
  - stop coverage
  - unexpected shorts
- Derive DB/in-memory position state from broker state, not the reverse.

Gate: `approval-needed` for live broker reads; safe to develop with fixtures.

### B. Symbol ownership registry

One active execution authority per symbol.

Preferred near-term policy:
- Single owner per symbol, derived deterministically from oldest still-open broker-backed buy, until flat.
- Non-owner entries are rejected or queued.
- Non-owner exits/stops are suppressed.
- If multiple strategies want same symbol, parent risk bucket arbitrates one request.

Regression fixtures must include IWM, KRE, NVDA, QQQ, SPY.

### C. Order manager

All order side effects go through one idempotent path.

Responsibilities:
- Submit buy only after kill/strategy/symbol/risk gates pass.
- Submit sell/flatten only up to available quantity.
- Cancel/replace/reuse existing stop/sell orders; never stack contradictory exits.
- Never cross long-only books into shorts.
- Attach/verify stop protection after fill.
- Use idempotency keys/state for flatten attempts.

### D. Outcome writer

Fresh trading outcomes and cleanup outcomes are separated by default.

Rules:
- Fresh exits: signal exit, stop loss, trailing stop, EOD close that actually closes a fresh held trade.
- Cleanup exits: `reconciled_no_position`, `stale_intraday_flatten_missed`, broker reconcile, orphan sweep.
- Strategy health, promotion, sizing, and report headline stats use fresh-only by default.
- Cleanup section is shown separately as operational health.

### E. Strategy activation gates

Promotion requires:
- Minimum sample size: at least 20 fresh paper-forward closes to continue at tiny size; 50+ for meaningful sizing; 200+ before serious Kelly changes.
- Positive expectancy after costs/slippage.
- Clean operational behavior: no duplicate ownership, no unprotected fills, no stale flat misses, no stop conflicts.
- Walk-forward/backtest support and paper-forward confirmation.
- One strategy reintroduced at a time.

### F. Reporting/review gates

Every report must show:
- Fresh vs cleanup split.
- Open broker positions and open orders by symbol/owner.
- Stop coverage.
- Long/short market value and exposure.
- Unexpected short alert.
- Strategy gate state: active, paused, observe-only, blocked.
- “Paper trading is engineering validation, not proof of edge.”

## 14. What should be paused now

Keep observe-only / no new entries until lifecycle authority is production-proven:
- `intraday-1m-momentum`
- `intraday-1m-vwap-reclaim`
- `intraday-1m-orb`
- `intraday-orbo-5m`
- `intraday-orb-pivots-5m`
- `trend-ma-cross-20-50` unless explicitly under stop/regime review
- `breakout-donchian-retest-20` and short variant until single-strategy Donchian is clean
- RSI/Bollinger demo strategies until sample + lookback + gate issues are proven

Keep as primary validation candidate:
- `trend-donchian-breakout-20`, but **Donchian-only**, tiny paper size, one clean lifecycle at a time.

Do not activate live:
- Any strategy. `do-not-build-yet`.

## 15. Day-by-day implementation plan

### Day 0 — complete authority audit and freeze unsafe expansion

Goal: lock scope before touching execution.

Tasks:
- Confirm current scheduled tasks and whether they are running. `approval-needed` if checking Windows scheduler state changes; read-only query is safer but still automation-sensitive.
- Inspect `config/kill_switch.json`, paused strategy table, and current active strategy declarations without modifying.
- Verify DB schema migration state: why `equity_snapshots` lacks `long_market_value` / `short_market_value` despite code expecting it.
- Inspect all tests that mention broker submit/cancel before running any suite.
- Define Donchian-only validation config plan but do not execute orders yet.

Pass criteria:
- Written list of all side-effecting entrypoints.
- Clear list of active vs observe-only strategy settings.
- Report exposure schema mismatch understood.

Gate:
- `safe-now` for file/DB reads.
- `approval-needed` for broker/scheduler reads.

### Day 1 — Donchian buy/sell lifecycle paper validation

Goal: prove `trend-donchian-breakout-20` actually buys, protects, exits, and records correctly under single-strategy conditions.

Preconditions:
- Ross explicitly approves paper validation.
- Live routing remains disabled.
- Non-Donchian strategies observe-only/no new entries.
- Tiny size.
- Kill switch and manual halt path verified.

Tasks:
- Run a single Donchian-only paper cycle using real production path.
- Verify entry signal -> order submission -> fill -> broker position -> DB `paper_trades` row -> `outcomes` open row.
- Verify stop attachment and `verify_fill_protected` result.
- Force/observe a sell lifecycle only through the authorized exit path; no duplicate owner.
- Verify broker flat/open order state, DB close outcome, and report fresh close all agree.

Pass criteria:
- One clean buy lifecycle.
- One clean sell/exit lifecycle.
- No duplicate symbol ownership.
- No unprotected position.
- No wash-trade/insufficient-qty errors.
- No cleanup outcome counted as fresh performance.

Fail criteria:
- Any non-Donchian entry fires.
- Any non-owner exit/stop is submitted.
- Broker and DB disagree after reconciliation.
- Stop fails without loud alert.

Gate: `approval-needed` because paper orders/broker reads are involved.

### Day 2 — Donchian-only risk tuning

Goal: improve Donchian risk control only after lifecycle works.

Tasks:
- Analyze fresh Donchian outcomes only.
- Exclude cleanup reasons.
- Review loser distribution, stop distance, MFE/MAE, bars held, trailing-stop behavior.
- Add/tune max-loss cap and trailing behavior only if evidence supports it.
- Verify report shows Donchian fresh stats, stop coverage, open positions, and cleanup separately.

Pass criteria:
- Donchian report has clean fresh stats.
- Stop/trailing metrics populated or explicitly marked unavailable.
- Max loser cap defined and tested with fixtures.
- No new strategies enabled.

Gate:
- `safe-now` for analysis/tests.
- `approval-needed` for paper validation of tuned stops.

### Day 3 — production-grade symbol ownership and broker-state authority

Goal: make symbol ownership impossible to bypass.

Tasks:
- Confirm all production submit paths call `position_manager` authority methods.
- Add regression fixtures for IWM/KRE/NVDA/QQQ/SPY shared-symbol conflicts.
- Enforce one owner per symbol or parent risk bucket arbitration.
- Ensure ownership release does not happen while protective stop exists for an open long.
- Ensure entries are blocked when another owner holds the symbol.

Pass criteria:
- Non-owner entry returns `SKIP_SYMBOL_OWNED`.
- Non-owner exit returns `SKIP_NOT_OWNER`.
- Non-owner stop returns skip/suppressed.
- Fixtures drive real `_process_entry`, `_process_exit`, `_maybe_attach_stop`, and `close_intraday_positions` paths.

Gate: `safe-now` for fixture tests with fake clients; `approval-needed` for paper verification.

### Day 4 — idempotent order manager and flattening

Goal: no duplicate stops/sells, no oversell, no short flips.

Tasks:
- Before any stop/flatten/sell, compute broker qty, held-for-orders, run reservation, available qty.
- Cancel/replace/reuse existing exits/stops instead of stacking.
- Add idempotency state for EOD flatten attempts.
- Add loud alert on unexpected short in long-only book.

Pass criteria:
- Existing stop gets replaced, not stacked.
- Duplicate flatten does not submit duplicate sell.
- Sell never exceeds broker available qty.
- No long-only strategy can flip short in fixture.

Gate: `safe-now` for tests; `approval-needed` for broker paper verification.

### Day 5 — analytics/reporting truth gate

Goal: reports cannot lie about strategy edge or exposure.

Tasks:
- Apply/verify `equity_snapshots` long/short market value migration.
- Update all report/dashboard/public strategy stats to fresh-only unless explicitly labelled cleanup/all.
- Fix any remaining deployment formula using `(portfolio_value - cash)`.
- Ensure `return_pct` is never multiplied by 100.
- Add report sections: broker positions, open orders, owner, available qty, stop coverage, cleanup split, unexpected short alert.

Pass criteria:
- Report shows long MV/short MV/gross/net exposure when schema present.
- Missing schema is loud, not silent.
- Cleanup outcomes excluded from headline performance and eligibility.
- Dashboard does not promote strategies off cleanup stats.

Gate: `safe-now` for local report generation; `approval-needed` for live broker-account snapshot.

### Day 6 — intraday flatten/exit observe-only repair

Goal: make intraday safe before any reactivation.

Tasks:
- Keep intraday entries disabled/observe-only.
- Verify exit signals are only recorded for owned live positions.
- Suppress normal `no_open_position` skip spam or aggregate it.
- Verify EOD flat assertion and stale flatten alerting.
- Add intraday max-hold and max-loss overlay in observe-only first.

Pass criteria:
- 1m strategies no longer emit thousands of positionless exits.
- EOD flat assertion catches unflattened broker positions.
- `stale_intraday_flatten_missed` count trends to zero in future sessions.
- No intraday strategy submits paper entries.

Gate: `safe-now` for observe-only code/tests; `approval-needed` for broker-state verification.

### Day 7 — strategy promotion/kill framework

Goal: controlled reintroduction.

Tasks:
- Define strategy states: disabled, observe-only, probation-paper, active-paper, killed.
- Gate promotion on fresh sample size, costs/slippage, drawdown, operational errors, and symbol conflicts.
- Add one-strategy-at-a-time reactivation workflow.
- Add review gate before any strategy promotion.

Pass criteria:
- No strategy can move to active-paper without explicit evidence and clean ops.
- Strategy state is visible in reports/dashboard.
- Negative expectancy strategies auto-pause after sample threshold.

Gate: `safe-now` for implementation/tests; `approval-needed` for paper activation.

### Day 8+ — reintroduce candidates one at a time

Order:
1. Donchian remains core.
2. Consider one botnet EOD variant with clean historical/fresh split and no symbol conflict.
3. Consider `intraday-mr-3bar-low-15m` only after intraday flattening is stable and sample improves.
4. Keep 1m momentum/VWAP/ORB observe-only until they pass edge-after-cost and no-spam gates.

Pass criteria per strategy:
- 20 fresh paper-forward closes before continuing beyond probation.
- 50 fresh closes before meaningful sizing.
- No stale flatten misses.
- No stop conflicts.
- No duplicate symbol ownership.
- Slippage/cost-adjusted expectancy positive.

Gate: `approval-needed` for each strategy paper activation.

## 16. Phase backlog

### Phase A — safe-now audit/hardening

- [ ] Classify every test as safe, mocked broker, live API, or order-submitting.
- [ ] Add a test manifest documenting dangerous tests.
- [ ] Verify current DB schema vs `data/db.py` migrations without mutating DB.
- [ ] Audit dashboard/public API stats for cleanup leakage.
- [ ] Audit all report paths for deployment formula and return_pct handling.
- [ ] Produce “side-effecting paths” map in repo docs.

### Phase B — Donchian-only validation

- [ ] Disable/observe-only all non-Donchian strategies for validation run. `approval-needed`.
- [ ] Run Donchian paper buy lifecycle. `approval-needed`.
- [ ] Run Donchian paper sell lifecycle. `approval-needed`.
- [ ] Verify broker positions/open orders/DB/report consistency.
- [ ] Verify post-fill stop protection.
- [ ] Verify cleanup outcomes not counted as fresh.

### Phase C — authority core

- [ ] Broker-state reconciler fixture tests.
- [ ] Symbol owner registry fixture tests for IWM/KRE/NVDA/QQQ/SPY.
- [ ] Idempotent stop replacement tests.
- [ ] Idempotent flatten tests.
- [ ] Unexpected short alert test.
- [ ] Wire confirmation grep/test for every submit path.

### Phase D — analytics truth

- [ ] Apply/verify `equity_snapshots` long/short MV migration.
- [ ] Replace legacy deployment formula everywhere.
- [ ] Fresh-vs-cleanup split in report/dashboard/public API.
- [ ] Outcome writer labels true exit reasons: signal, stop, trailing, EOD, cleanup.
- [ ] MFE/MAE populated for stop/trailing/signal exits.

### Phase E — intraday repair

- [ ] Intraday exit recording requires owned broker-backed position.
- [ ] Suppress/aggregate `no_open_position` skip spam.
- [ ] EOD flat assertion production verification.
- [ ] Max-hold/max-loss intraday observe-only overlay.
- [ ] Intraday strategy-specific cost/slippage gate.

### Phase F — strategy reintroduction

- [ ] Define promotion/kill states.
- [ ] Add report review gate.
- [ ] Reintroduce one EOD candidate at tiny paper size.
- [ ] Reintroduce one intraday candidate only after flat/stops stable.
- [ ] Keep live trading blocked.

## 17. Side-effecting path inventory

### Broker credentials/client
- `config/utils.py:load_credentials`
- `config/utils.py:get_alpaca_client`
- `config/utils.py:get_account_summary`
- `monitoring/wide_bars.py` uses Alpaca historical data client.

### Order placement/cancel/replace
- `monitoring/auto_trader.py:_process_entry`
- `monitoring/auto_trader.py:_process_exit`
- `monitoring/auto_trader.py:_maybe_attach_stop`
- `monitoring/position_manager.py:safe_submit_sell`
- `monitoring/position_manager.py:safe_submit_stop`
- `monitoring/stops.py`
- `monitoring/close_intraday_positions.py`
- `scripts/flatten_unintended_shorts.py`

### DB writes
- `data/db.py` signal/outcome/paper trade/equity writers.
- `monitoring/daily_report.py` report and reconcile writes.
- `monitoring/intraday_fires.py` signal and skip writes.
- `monitoring/intraday_monitor.py` intraday writes.
- `monitoring/tv_webhook.py` TradingView webhook writes.

### Automation/external delivery
- Windows Task Scheduler `.bat` registration scripts.
- `schedulers/pg_report_data.py` Hermes report copy/install path.
- Telegram listener/alerts.
- TradingView webhook receiver.
- Dashboard health endpoints that may call Alpaca.

## 18. Blockers and warnings

1. `reports/` directory did not exist before this audit report was written.
2. `equity_snapshots` live DB schema lacks `long_market_value` / `short_market_value`, despite code/docs expecting them.
3. `settings.json` is paper-enabled and non-dry-run; do not run scheduler/order paths during audits.
4. Tests are numerous and some mention `submit_order`, `get_alpaca_client`, Polygon/FRED/yfinance, or `live`; do not run broad tests until classified.
5. DB evidence still shows duplicate symbol ownership and intraday exit spam, even if recent code aims to stop it going forward.
6. Donchian credibility is not a profit claim. It is the best engineering validation candidate, not proven edge.

## 19. Bottom line

The system should be rebuilt around the authority model already started in Sprint 3, but the next step is not another broad strategy patch. The next step is a controlled Donchian-only paper validation with broker-state authority, one symbol owner, idempotent order management, protected fills, and fresh-only reporting.

Until that passes, every non-Donchian strategy should stay observe-only or paused, and live trading remains `do-not-build-yet`.
