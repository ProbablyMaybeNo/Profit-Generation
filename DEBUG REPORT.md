# DEBUG REPORT — Profit Generation (AI Trading System)

**Audit date:** 2026-05-14  
**Scope:** Full read-through of `D:\AI-Workstation\Antigravity\apps\Profit Generation`  
**Pytest:** `py -3.13 -m pytest tests/ --ignore=tests/test_all.py` → **94 passed**, 12 warnings (34s)  
**Action taken:** Issues documented only — **no fixes applied** (per request).

---

## Executive summary

Paper-trading-focused stack: Alpaca (orders + intraday bars), Polygon (universe + news), yfinance (EOD), SQLite (`data/trading.db`), mean-reversion strategies (Botnet101), momentum/SMC/ORB backtests, daily Notion reports, intraday monitor, TradingView webhook, Telegram alerts, Flask dashboard.

**Highest-impact problems before production scheduling:**

1. **Stale `apps\Trading` paths** in several scheduler/docs files — scheduled tasks will fail or point at the wrong folder.
2. **Two competing daily pipelines** (`monitoring.daily_report` vs `monitoring.run_daily`) sharing the **same Task Scheduler name** `TradingSystem\DailyReport`.
3. **`run_daily.py` skips DB persistence, news, outcomes, and Telegram** that `daily_report.main()` performs.
4. **`tests/test_all.py` breaks `pytest tests/`** when not ignored.
5. **Fresh clone cannot seed strategies** unless `data/scrapes/.../records.jsonl` is present (now includable in git after `.gitignore` tweak).

---

## System architecture (reference)

| Layer | Role | Key paths |
|-------|------|-----------|
| Config | Credentials, risk limits, cache | `config/utils.py`, `config/risk.py`, `config/settings.json` |
| Data | SQLite ORM-style API | `data/db.py` → `data/trading.db` |
| Backtest | Bar engine, Polygon/yfinance loaders | `backtest/` |
| Strategies | Mean-rev (live), momentum, SMC, ORB | `strategies/` |
| Monitoring | Daily report, intraday, TV webhook, Notion | `monitoring/` |
| Ops | Windows Task Scheduler wrappers | `schedulers/` |
| UI | Local status dashboard | `dashboard/server.py` :8080 |

**Safety:** `is_paper_mode()` + `config/risk.validate_order()` gate live orders; Alpaca client uses `paper=True` from credentials.

---

## Issue index

| ID | Severity | Area | Title |
|----|----------|------|-------|
| PG-001 | **Critical** | Schedulers | Wrong path `apps\Trading` in batch files |
| PG-002 | **Critical** | Schedulers | Duplicate `TradingSystem\DailyReport` task registration |
| PG-003 | **High** | Monitoring | `run_daily.py` incomplete vs `daily_report.main()` |
| PG-004 | **High** | Docs | `monitoring/SETUP.md` and `monitoring/README.md` reference old folder |
| PG-005 | **High** | Git / clone | `data/` gitignore hid strategy seed bundle |
| PG-006 | **Medium** | Tests | `test_all.py` breaks pytest collection |
| PG-007 | **Medium** | Tests | Live API tests run in default pytest (slow, need keys) |
| PG-008 | **Medium** | Deps | `requirements.txt` missing pytest; README Python version drift |
| PG-009 | **Medium** | Outcomes | Intraday / TV webhook signals never reconciled to outcomes |
| PG-010 | **Medium** | Notion | Report body truncated at 100 blocks |
| PG-011 | **Medium** | Security | Dashboard + TV webhook exposed without auth when misconfigured |
| PG-012 | **Low** | Monitoring | `monitor.py` heartbeat logging inconsistent |
| PG-013 | **Low** | Backtest | Portfolio allows implicit shorts on sell-without-position |
| PG-014 | **Low** | Strategy | `strategy_fires` only resolves `botnet101` (unlike intraday DB path) |
| PG-015 | **Low** | Telegram | Markdown special characters can break `parse_mode=Markdown` |
| PG-016 | **Info** | Naming | README title still "Trading System" |

---

## Critical

### PG-001 — Scheduler batch files still point at `apps\Trading`

**Files:**

- `schedulers/register_monitor.bat` — `python ...\apps\Trading\monitor.py`
- `schedulers/register_daily_report.bat` — task action `...\apps\Trading\schedulers\run_daily_report.bat`
- `schedulers/run_daily_report.bat` — `TRADING_ROOT=...\apps\Trading`
- `schedulers/start_dashboard.bat` — `cd ...\apps\Trading`

**Correct path:** `D:\AI-Workstation\Antigravity\apps\Profit Generation\`

**Impact:** Heartbeat monitor, legacy daily report task, and dashboard launcher fail on a machine that only has this folder.

**Evidence:** `register_intraday.bat` / `register_daily.bat` / `run_daily.bat` already use `Profit Generation`; the rest do not.

---

### PG-002 — Two scripts register the same scheduled task name

| File | Task name | Invokes |
|------|-----------|---------|
| `schedulers/register_daily.bat` | `TradingSystem\DailyReport` | `run_daily.bat` → `monitoring.daily_report` |
| `schedulers/register_daily_report.bat` | `TradingSystem\DailyReport` | `run_daily_report.bat` → `monitoring.run_daily` |

**Impact:** Whichever registration runs last wins; operators may think they enabled the full pipeline but get the slim one (or vice versa). Duplicate Notion pages possible if both runners are triggered manually.

---

## High

### PG-003 — `monitoring/run_daily.py` is a reduced pipeline

`run_daily.py` does: Notion smoke test → `build_report()` → markdown/json files → `post_daily_report()` (no idempotency check).

**Missing vs `monitoring/daily_report.py` `main()`:**

- `gather_news()` / Polygon news persistence
- `persist_report()` → snapshots, signals, `daily_reports` row, `outcome_tracker.reconcile_signals()`
- `post_to_notion()` idempotency (skip if `notion_page_id` exists)
- `telegram_alerter.send_daily_summary()`

**Impact:** Scheduled `run_daily_report.bat` produces Notion pages but **does not update `trading.db`** or track trade outcomes. Intraday/TV signals and backfill data diverge from “daily run” state.

---

### PG-004 — Documentation paths outdated

- `monitoring/SETUP.md` lines 28, 44, 88: `apps\Trading`
- `monitoring/README.md` line 50: `apps\Trading`

**Impact:** Setup steps copy-pasted from docs fail for new clones or this repo name.

---

### PG-005 — Strategy seed data and git (addressed in repo bootstrap only)

`scripts/seed_strategies.py` requires:

`data/scrapes/tradingview-in-daytrading-strategies-2026-04-26/records.jsonl`

Previous `.gitignore` excluded all of `data/`, so a **GitHub-only clone could not seed the DB**. Repo init changed ignore rules to `data/*.db` + `data/cache.db` while keeping scrape bundles committable.

**Impact:** Fresh environment: run seed script after clone or strategies table stays empty → intraday monitor skips all strategies.

---

## Medium

### PG-006 — `tests/test_all.py` is not a pytest module

Running `py -3.13 -m pytest tests/` fails at collection:

```
ERROR tests/test_all.py — Interrupted: 1 error during collection
```

`test_all.py` is a **subprocess runner** (`if __name__ == "__main__"`), not pytest-style tests. README still says `python tests/test_all.py`.

**Workaround used in audit:** `--ignore=tests/test_all.py`

**Fix direction:** Rename to `run_integration_checks.py` or add `pytest.ini` `collect_ignore`.

---

### PG-007 — Integration tests mixed with unit tests

These call real APIs when credentials exist:

- `tests/test_alpaca.py` — auth, clock, market data, **paper order**
- `tests/test_polygon.py`, `tests/test_fred.py`, `tests/test_yfinance.py`

**Impact:** CI without keys may fail or skip unpredictably; local runs place real paper orders (`test_paper_order`). No `@pytest.mark.live` separation.

---

### PG-008 — Dependencies and README drift

| Item | Found |
|------|--------|
| `requirements.txt` | No `pytest`, no explicit `pytest` marker deps |
| README | `conda` + **Python 3.11**; workspace standard is **3.13** (`py -3.13`) |
| README tests | `python tests/test_all.py` vs pytest suite |
| `run_daily.bat` | Hard-coded `D:\AI-Hub\environments\conda-envs\trading\python.exe` (machine-specific) |
| `run_intraday.bat` | Uses `conda activate trading` (differs from daily bat) |

---

### PG-009 — Outcome tracker only processes `bar_interval='1d'`

`outcome_tracker.reconcile_signals(..., bar_interval="1d")` — hardcoded.

Signals from:

- `intraday_monitor` → `1d-intraday`
- `tv_webhook` → `tv-webhook`

**never** open/close rows in `outcomes`.

**Impact:** P&L tracking and backfill stats ignore intraday and TradingView-sourced signals by design unless extended.

---

### PG-010 — Notion daily report truncated

`notion_writer.post_daily_report()`:

```python
"children": _markdown_to_blocks(markdown)[:100],
```

Long reports (full snapshot table + news) lose tail sections silently.

**Note:** `daily_report.post_to_notion()` uses the same writer — both paths affected.

---

### PG-011 — Security surface (local / tunnel exposure)

| Component | Risk |
|-----------|------|
| `dashboard/server.py` | `host="0.0.0.0"`, no auth; exposes Alpaca account summary |
| `monitoring/tv_webhook.py` | If `webhook_secret` unset, **accepts anonymous POSTs** (logged warning only) |
| `credentials.json` | Correctly gitignored; example template added as `credentials.example.json` |

---

## Low / informational

### PG-012 — `monitor.py` logging

Only the “market closed” path passes `HEARTBEAT_LOG` to `log()`. Portfolio lines during open market go to **stdout only**, not `logs/heartbeat.log` (except final heartbeat line written manually).

---

### PG-013 — Backtest portfolio sells without position

`portfolio.apply_fill()` on sell always credits cash; does not reject oversell. Can create effective short exposure in simulation. Live trading blocked by `risk.validate_order()`; backtests may not match live constraints.

---

### PG-014 — `strategy_fires` vs intraday strategy resolution

- `monitoring/strategy_fires.py` — imports only `strategies.mean_reversion.botnet101`
- `monitoring/intraday_monitor.py` — uses DB `compute_fn` + `_resolve_compute_fn` (same module list)

If new compute modules are added to DB without updating both paths, EOD fire checks and intraday scans diverge.

---

### PG-015 — Telegram Markdown fragility

`send_intraday_alert` / `send_daily_summary` use `parse_mode="Markdown"`. Strategy IDs with `_`, `*`, or `[` can cause Telegram API 400. Failures are logged as warnings (non-fatal).

---

### PG-016 — Project naming

Folder: **Profit Generation**. README H1: **Trading System**. Task Scheduler namespace: **TradingSystem\***. Consider aligning for operator clarity.

---

## Per-module notes (file-by-file)

### `config/`

| File | Status | Notes |
|------|--------|-------|
| `utils.py` | OK | Atomic `save_state`, paper check, Alpaca client |
| `risk.py` | OK | Daily loss uses `last_equity` — verify Alpaca semantics match “equity at open” intent |
| `cache.py` | OK | Pickle cache — do not cache untrusted payloads |
| `settings.json` | OK | `paper_trading` flag redundant with credentials; only credentials gate orders |

### `data/db.py`

| Status | Notes |
|--------|-------|
| OK | Idempotent schema, WAL, sensible indexes |
| Watch | `insert_news` stores `insights` JSON in `sentiment` column when present — naming mismatch |
| Watch | `record_paper_trade` not referenced elsewhere in codebase yet |

### `backtest/`

| File | Notes |
|------|-------|
| `engine.py` | Next-bar-open fills; unfilled limits dropped after one bar |
| `data.py` | `load_bars` auto picks Alpaca for intraday; yfinance for 1d |
| `polygon_data.py` | 13s rate limit sleep — correct for free tier |
| `report.py` | Sharpe on bar returns, not trade-level |
| `smoketest.py` | Good sanity check; requires network |

### `monitoring/`

| File | Notes |
|------|-------|
| `daily_report.py` | Canonical full pipeline |
| `run_daily.py` | See PG-003 |
| `movers.py` | Uses last row in yfinance window as “today” — on holidays may be prior session (acceptable if documented) |
| `strategy_fires.py` | See PG-014; load_error rows use `strategy_id: load_error` |
| `intraday_monitor.py` | Skips crypto; good dedupe on signals |
| `news_fetcher.py` | Graceful degradation |
| `notion_writer.py` | See PG-010; `smoke_test()` only checks `/users/me` |
| `outcome_tracker.py` | See PG-009 |
| `tv_webhook.py` | See PG-011 |
| `telegram_alerter.py` | See PG-015 |

### `strategies/`

| Area | Notes |
|------|-------|
| `mean_reversion/botnet101.py` | Core live strategies; `SignalStrategy` sizes on signal bar close, fills next open — minor sim bias |
| `momentum/execution.py` | Standalone sim; not wired to Alpaca execution |
| `smc/` | Research/backtest; TODOs in docstring for v2 |
| `orb/runner.py` | CLI backtest only |

### `scripts/`

| File | Notes |
|------|-------|
| `seed_strategies.py` | Requires scrape bundle (PG-005) |
| `backfill_outcomes.py` | Deletes duplicate open outcomes — document before re-run |
| `scrape_tradingview_daytrading_strategies.py` | Scraper; playwright in requirements |

### `schedulers/`

See PG-001, PG-002. `register_daily.bat` trigger 14:30 — comment says PT; confirm machine timezone.

### `dashboard/`

| File | Notes |
|------|-------|
| `server.py` | Returns 200 with `error` key when Alpaca down — UI should handle |
| `index.html` | Not audited line-by-line; polls `/api/status` |

### `tests/`

| File | Notes |
|------|-------|
| `test_db.py` | Solid unit coverage |
| `test_intraday_monitor.py` | Mocks loaders |
| `test_tv_webhook.py` | Secret + payload tests |
| `test_telegram_alerter.py` | Mocked HTTP |
| `test_*` (alpaca/polygon/fred/yfinance) | Live integration — PG-007 |

---

## Scripts & one-off artifacts (not bugs, but know they exist)

- `data/scrapes/tradingview-in-daytrading-strategies-2026-04-26/_*.py` — maintenance scripts for scrape bundle
- `strategies/mean_reversion/overlay_test.py`, `strategies/momentum/*_run.py` — research runners
- `logs/`, `data/trading.db`, `.pytest_cache/` — local runtime (gitignored)

---

## Recommended fix order (for Claude Code — do not execute yet)

1. **Unify schedulers** — single `TradingSystem\DailyReport` → `run_daily.bat` → `monitoring.daily_report`; fix all `apps\Trading` paths.
2. **Deprecate or align `run_daily.py`** — either thin wrapper calling `daily_report.main()` or delete.
3. **pytest hygiene** — ignore/rename `test_all.py`; mark live tests; add pytest to requirements.
4. **Docs** — replace `apps\Trading` in SETUP.md / README paths.
5. **Outcomes** — decide policy for `1d-intraday` / `tv-webhook` intervals.
6. **Notion** — paginate blocks or attach markdown as file when >100 blocks.
7. **TV webhook / dashboard** — require secret; bind dashboard to `127.0.0.1` by default.

---

## GitHub repository

Standalone repo initialized from this folder (not the Antigravity monorepo root):

- **Remote:** `https://github.com/ProbablyMaybeNo/Profit-Generation` (GitHub slug; display name “Profit Generation”)
- **Excluded from git:** `config/credentials.json`, `data/*.db`, `logs/`, caches
- **Included:** source, tests, schedulers, `data/scrapes/...` strategy bundle, `config/credentials.example.json`

---

## Verification commands (post-fix)

```powershell
cd "D:\AI-Workstation\Antigravity\apps\Profit Generation"
py -3.13 -m pytest tests/ --ignore=tests/test_all.py -q
py -3.13 -m monitoring.notion_writer
py -3.13 scripts/seed_strategies.py
py -3.13 -m monitoring.daily_report --no-notion --no-telegram
py -3.13 -m backtest.smoketest
```

---

*End of report.*
