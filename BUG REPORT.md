# BUG REPORT — Profit Generation (refreshed audit)

**Audit date:** 2026-05-17  
**Auditor scope:** Read-only review of repo at `D:\AI-Workstation\Antigravity\apps\Profit Generation`  
**Branch tip:** `d8a085b` (Phase 4.1.3 live smoke playbook committed)  
**Pytest:** `py -3.13 -m pytest tests/` → **1271 passed**, 2 skipped, 12 warnings (~55s)  
**Unit-only:** `py -3.13 -m pytest tests/ -m "not live"` → **1259 passed**, 12 deselected (~23s)  

**No code was changed during this audit.** Hand off to Claude Code / milestone-builder for fixes.

---

## Executive summary

The system has grown from a monitoring + backtest scaffold into a **production-shaped paper-trading stack**: auto-trader with extensive vetoes, kill switch, crypto adapter, dashboard control plane, reconciliation, tax export, strategy health, and Phase 4 live-promotion tooling.

Most issues from the **May-14 DEBUG REPORT** were addressed in milestone **3.5.1** (regression tests in `tests/test_debug_report_cleanup.py`; original `DEBUG REPORT.md` was deleted per plan).

**What still matters before live money (Phase 4.1):**

| Priority | Issue |
|----------|--------|
| **Critical** | Daily pipeline only reconciles `1d` outcomes — intraday/TV signals stay orphaned in `outcomes` |
| **Critical** | `auto_trader` never calls `config/risk.validate_order` — no `max_orders_per_day` / central risk path on real submissions |
| **High** | `config/settings.json` has `auto_trade.enabled: true`, `dry_run: false` — **real Alpaca paper orders** if scheduler runs |
| **High** | Live-promotion scorer vs auto-trader eligibility use **different outcome definitions** |
| **High** | `monitoring/accounts.py` capital split not wired into order submission (documented deferral) |
| **Medium** | Plan/doc drift (Phase 4 header still says DRAFT; README still points at Phase 3; stale monitoring README) |

Phase 4 agent work is active: **4.1.1–4.1.3 done in git**; **4.1.2 code shipped** (Ross must run wizard with live keys); **4.1.4 crypto smoke doc untracked** at audit time.

---

## System snapshot (for handoff context)

| Layer | Maturity | Notes |
|-------|----------|-------|
| Monitoring / daily report | Mature | Notion pagination, idempotent post, news, Telegram |
| Intraday + TV webhook | Mature | DB signals; webhook refuses boot without secret (unless override flag) |
| Auto-trader | Mature (paper) | Kelly/tiered sizing, stops, drawdown throttle, regime router, live routing via `live_strategies` |
| Dashboard | Mature | `/api/state`, action queue, loopback-gated POSTs, many analytics widgets |
| Live transition (4.1) | In progress | Scorer + wizard + equity smoke playbook committed |
| Trend / pyramiding (4.6) | Not started | Plan only |
| Public performance page (4.4) | Not started | Plan only |
| Claude codegen (4.3) | Not started | Still Ollama path |

**Safety rails present:** `is_paper_mode()`, kill switch, `live_strategies` carve-out to `alpaca_live`, preflight, reconcile_positions, drawdown auto-trip.

---

## Issue index

| ID | Sev | Area | Title |
|----|-----|------|-------|
| BR-001 | **Critical** | Outcomes | `daily_report.persist_report` reconciles `1d` only |
| BR-002 | **Critical** | Risk | `auto_trader` bypasses `config/risk.validate_order` |
| BR-003 | **High** | Ops / config | `auto_trade.enabled=true`, `dry_run=false` in committed `settings.json` |
| BR-004 | **High** | Live transition | `is_paper_mode()` gate + live routing semantics easy to misconfigure |
| BR-005 | **High** | Live promotion | Scorer vs `_is_eligible` use different outcome populations |
| BR-006 | **High** | Multi-account | `accounts.split_notional` not used by auto_trader |
| BR-007 | **Medium** | Docs | Phase plan file says DRAFT while named `PHASE4_PLAN CURRENT.md` |
| BR-008 | **Medium** | Docs | `README.md` still references Phase 3 as “current” |
| BR-009 | **Medium** | Docs | `monitoring/README.md` “Scheduling (TODO)” is stale |
| BR-010 | **Medium** | Docs | `LIVE_SMOKE_TEST.md` committed; plan checkbox for 4.1.3 may lag |
| BR-011 | **Medium** | Preflight | No dedicated `--tunnel` CLI flag (4.5.1 still open) |
| BR-012 | **Medium** | Tests / deps | `pytest` not listed in `requirements.txt` |
| BR-013 | **Medium** | Tests | Live API tests still run by default (`pytest tests/`) |
| BR-014 | **Low** | Monitor | Final heartbeat `SUCCESS` log omits `HEARTBEAT_LOG` path |
| BR-015 | **Low** | Data bundle | Scrape README still references `apps/Trading` path |
| BR-016 | **Low** | TV webhook | Default bind `0.0.0.0` (mitigated by secret requirement) |
| BR-017 | **Low** | Auto-trader | `MARKET_OPEN_UTC` fixed offset — ~1h DST drift possible |
| BR-018 | **Info** | Git | Untracked: `docs/CRYPTO_SMOKE_TEST.md`, `docs/PHASE5_PLAN DRAFT.md` |

---

## Critical

### BR-001 — Daily reconcile ignores intraday and TradingView intervals

**Where:** `monitoring/daily_report.py` → `persist_report()` calls:

```python
return outcome_tracker.reconcile_signals(conn)
```

**Expected (per PG-009 fix + `test_debug_report_cleanup.py`):** pass all signal sources:

```python
reconcile_signals(conn, bar_intervals=["1d", "1d-intraday", "tv-webhook"])
```

**Impact:** Intraday-projected fires and TV webhook entries land in `signals` and appear on the dashboard action queue, but **never open/close `outcomes` rows** when the daily job runs. P&L stats, live-promotion scorer (paper_trades-joined outcomes), and strategy health diverge from what the UI shows as “actionable today.”

**Regression guard exists** in `tests/test_debug_report_cleanup.py` for the *function* — not for the *call site*.

---

### BR-002 — Auto-trader does not use central risk module on submit

**Where:** `monitoring/auto_trader.py` submits via `_submit_market_order` / `_submit_limit_order` directly.

**Not used:** `config/risk.validate_order`, `config/risk.submit_order_safely`.

**What auto_trader *does* implement instead:** drawdown throttle, concentration cap, cool-down losers, earnings veto, sentiment veto, kill switch, regime router, sizing module, optional ATR stops.

**Gap vs `config/risk.py`:** `max_orders_per_day`, `max_open_positions` (as enforced in risk), and the explicit `is_paper_mode()` check **per order** are absent on the submission path. Pipeline-level `BLOCKED_LIVE_MODE` only runs when `credentials.alpaca.paper` is false — not the same as per-order validation.

**Impact:** A runaway scheduler or bug could submit more orders or sizes than `risk.py` was designed to cap, while still passing auto_trader’s own gates.

---

## High

### BR-003 — Committed settings enable real paper submissions

**File:** `config/settings.json`

```json
"auto_trade": {
  "enabled": true,
  "dry_run": false,
  ...
  "live_strategies": []
}
```

**Impact:** Any trigger of `monitoring.auto_trader` (scheduler, dashboard `/api/run/auto_trader`, daily pipeline if wired) **places real orders on the Alpaca paper account**, not log-only dry runs.

**Note:** This may be intentional for your current phase — flag for Ross to confirm it matches operational intent. Fresh clones inherit aggressive defaults.

---

### BR-004 — Live routing vs `is_paper_mode()` — configuration foot-gun

**Behavior:**

- `process_signals()` returns `BLOCKED_LIVE_MODE` when `is_paper_mode()` is false (`credentials.alpaca.paper` not true).
- Strategies in `auto_trade.live_strategies` use `get_alpaca_client(live=True)` → `alpaca_live` section.
- `config/risk.validate_order` refuses orders when `is_paper_mode()` is false (if that path were used).

**Foot-gun:** Setting `alpaca.paper: false` on the **paper** section blocks the **entire** auto-trader, including strategies meant to stay on paper. Live promotion playbook correctly assumes `alpaca.paper` stays true and only listed strategies hit `alpaca_live`.

**Missing for Phase 4:** No automated check that `live_strategies` ⊆ `READY_FOR_LIVE` from scorer before first live flip (human process only).

---

### BR-005 — Live promotion scorer ≠ auto-trader eligibility

| Tool | Population |
|------|------------|
| `scripts/score_live_candidates.py` | Closed outcomes where a **`paper_trades` BUY** exists for the signal (true “went through auto-trader”) |
| `monitoring/auto_trader._is_eligible()` | All closed outcomes on **`bar_interval='1d'`** for the strategy (includes validator/backfill-only signals) |

**Impact:** A strategy can show `READY_FOR_LIVE` in the scorer while auto-trader still treats it as ineligible (or vice versa). Operators need a written rule for which metric is authoritative.

**Recommendation (product):** Align thresholds to the same SQL join, or document that scorer is “live path only” and `_is_eligible` is “signal history only.”

---

### BR-006 — Multi-account registry not wired to execution

**Where:** `monitoring/accounts.py` — explicitly states auto_trader multi-account iteration is **Phase-4 deferred**.

**Impact:** `config/accounts.json` and `split_notional()` are test-only until wired. Live transition playbook discusses `live_strategies` but not per-account capital splits.

---

## Medium

### BR-007 — Phase 4 plan metadata contradicts filename

**File:** `docs/PHASE4_PLAN CURRENT.md`  
**Header:** still says `⚠️ DRAFT` and “Rename to `PHASE4_PLAN CURRENT.md` before running milestone-builder” — but the file **is already** named `PHASE4_PLAN CURRENT.md` and milestones are being ticked.

**Impact:** Confusing for humans and agents; risk of thinking Phase 4 is not approved for autonomous runs.

---

### BR-008 — README points at wrong active phase

**File:** `README.md` § “Phase 3 — current” references `docs/PHASE3_PLAN CURRENT.md` (file on disk is `PHASE3_PLAN COMPLETE.md`).

**Impact:** New contributors run wrong plan / wrong milestones.

---

### BR-009 — `monitoring/README.md` scheduling section obsolete

Still says scheduling is **“Not yet automated”** and lists manual options. Repo now has extensive `schedulers/*.bat` (daily, intraday, reconcile, crypto, weekly, backup, telegram listener).

---

### BR-010 — Plan vs repo drift on 4.1.3 / 4.1.4

At audit time:

- `docs/LIVE_SMOKE_TEST.md` — **committed** (`f7b73fb` / `d8a085b`); plan section 4.1.3 may still show `[ ]` until next plan tick.
- `docs/CRYPTO_SMOKE_TEST.md` — **untracked** (`??` in git status); plan 4.1.4 open.

Not a runtime bug — process hygiene for milestone-builder.

---

### BR-011 — Preflight `--tunnel` flag not implemented (Phase 4.5.1)

**Current:** `scripts/preflight.py` always runs `check_tunnel_url` as part of `run_all()`; no `py -3.13 scripts/preflight.py --tunnel` shortcut.

**Impact:** RUNBOOK procedure still heavier than planned; cannot gate tunnel-only without running full preflight.

---

### BR-012 — `pytest` absent from `requirements.txt`

**Present:** `pyproject.toml` markers only.  
**Impact:** Fresh `pip install -r requirements.txt` environments may lack pytest until installed separately (workspace uses global/py -3.13 pytest today).

---

### BR-013 — Live API tests default-on

**Files:** `tests/test_alpaca.py`, `test_polygon.py`, `test_fred.py`, `test_yfinance.py` — marked `live` in `pyproject.toml` but **not** excluded unless `-m "not live"`.

**Impact:** CI or `pytest tests/` without credentials may fail or hit Alpaca paper with a test order (`test_paper_order`). README documents the split; default behavior remains aggressive.

---

## Low / informational

### BR-014 — Heartbeat log incomplete on success line

`monitor.py` routes most lines to `HEARTBEAT_LOG` (PG-012 fix), but the final `log(heartbeat, "SUCCESS")` call **does not** pass `str(HEARTBEAT_LOG)` — only the manual `with open(HEARTBEAT_LOG)` write captures the summary line.

---

### BR-015 — Stale path in scrape bundle README

`data/scrapes/tradingview-in-daytrading-strategies-2026-04-26/README.md` still says code paths relative to `apps/Trading/`.

---

### BR-016 — TV webhook binds all interfaces by default

`monitoring/tv_webhook.py` defaults `--host 0.0.0.0`. Startup **refuses** without secret unless `--allow-unauthenticated`. Safer than May-14 audit, but tunnel + `0.0.0.0` is still a exposure surface if misconfigured.

---

### BR-017 — Entry time offset uses fixed UTC market open

`auto_trader.MARKET_OPEN_UTC = dtime(13, 30, 0)` with comment acknowledging DST. Scheduled entries can drift ~1 hour twice yearly.

---

### BR-018 — Untracked docs at audit time

- `docs/CRYPTO_SMOKE_TEST.md` — likely 4.1.4 deliverable awaiting commit  
- `docs/PHASE5_PLAN DRAFT.md` — forward planning  

---

## Verified fixes (prior DEBUG REPORT — do not re-open)

Confirmed fixed or superseded; guarded by `tests/test_debug_report_cleanup.py`:

| Old ID | Resolution |
|--------|------------|
| PG-001 | Scheduler paths → `Profit Generation` |
| PG-002 | Duplicate daily task scripts removed |
| PG-003 | `run_daily.py` removed; canonical `monitoring.daily_report` |
| PG-004 | SETUP paths updated (minor stale sections remain — see BR-008/009) |
| PG-005 | `.gitignore` allows scrape bundle; seed works |
| PG-006 | `test_all.py` → `scripts/run_integration_checks.py` |
| PG-009 | **API exists**; call site in daily_report **not updated** (BR-001) |
| PG-010 | Notion block pagination in `notion_writer.post_daily_report` |
| PG-011 | Dashboard defaults `127.0.0.1`; TV webhook requires secret |
| PG-012 | Mostly fixed (BR-014 minor remainder) |
| PG-013 | Portfolio long-only cap on oversell |
| PG-014 | `strategy_fires` shares resolver pattern with intraday |
| PG-015 | `telegram_alerter.escape_markdown` |
| PG-016 | README title still generic (“Profit Generation” body OK) |

---

## Phase 4 — build gaps (not bugs, but scope holes)

These are **planned unchecked milestones**, not defects:

| Milestone | Status |
|-----------|--------|
| 4.1.2 wizard | Code committed; **Ross must run** with live keys |
| 4.1.4 crypto smoke | Doc untracked at audit |
| 4.2–4.4 | Research / public API / Claude codegen — not built |
| 4.5.1 preflight `--tunnel` | Open (BR-011) |
| 4.6 trailing stops / pyramiding / trend strategies | Not started |

---

## Suggested fix order (for implementing agent)

1. **BR-001** — One-line call-site fix in `daily_report.persist_report` (+ integration test asserting intraday signal creates outcome).  
2. **BR-002** — Route auto_trader submits through `submit_order_safely` or duplicate critical limits (`max_orders_per_day`, open position count).  
3. **BR-003** — Change default `settings.json` to `enabled: false` or `dry_run: true` for repo template; document production override in RUNBOOK.  
4. **BR-005** — Align scorer SQL with `_is_eligible` or document dual metrics in LIVE_SMOKE_TEST.  
5. **BR-007 / BR-008 / BR-009** — Doc sweep (15 min).  
6. **BR-006** — Wire when second account goes live.  
7. **BR-011–BR-013** — Hygiene before CI hardening.

---

## Verification commands (post-fix)

```powershell
cd "D:\AI-Workstation\Antigravity\apps\Profit Generation"

# Fast unit suite
py -3.13 -m pytest tests/ -m "not live" -q

# Full suite (needs credentials for live-marked tests)
py -3.13 -m pytest tests/ -q

# Regression guards for prior cleanup
py -3.13 -m pytest tests/test_debug_report_cleanup.py -v

# Operational smoke (no orders if dry_run)
py -3.13 scripts/preflight.py
py -3.13 -m monitoring.daily_report --no-notion --no-telegram
py -3.13 scripts/score_live_candidates.py --no-notion

# Live API integration (optional — places one paper order)
py -3.13 scripts/run_integration_checks.py
```

---

## Handoff note for Phase 4 agent

- **Do not re-litigate** fixed PG items unless a regression test fails.  
- **Prioritize BR-001 and BR-002** before Ross adds symbols to `live_strategies`.  
- **4.1.2** is “code complete, human-in-the-loop” — wizard must not be run by agent with real keys.  
- After BR-001 fix, re-run daily report once and confirm `outcomes` rows exist for same-day `1d-intraday` / `tv-webhook` signals.

---

*End of report.*
