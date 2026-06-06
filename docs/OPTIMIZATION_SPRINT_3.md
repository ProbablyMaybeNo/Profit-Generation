# Sprint 3 — Controlled Execution-Core Rebuild

This is NOT more strategy-level patching. It replaces the broken control model with a
clean one, per the 2026-06-05 reset (Ross + Claude + Hermie aligned). It runs IN
PARALLEL with the Donchian-only live system (the strip-down made it safe to rebuild
under a running system; it did not finish the job).

**The root failure being replaced:** strategies behave as if they independently own
positions. Alpaca sees ONE broker position per symbol. So multiple strategies submit
competing stops/exits/flattens for the same symbol → `40310000` wash-trade rejects,
"insufficient qty" failures, and overselling past flat into unintended shorts. There is
no single authority for: who owns a symbol, who may submit orders, what qty is
available, whether an exit already exists, and what counts as real performance vs
cleanup. Phase 1 builds that authority.

**HARD LESSON — every order-management milestone must be VERIFIED IN PROD, not just
unit-green.** Sprint 2's `position_manager.py` passed its tests and the shorts STILL
grew the next session because it wasn't actually on the live oversell path. For each
Phase-1 milestone: (a) the acceptance test must drive the REAL production function
(`auto_trader` / `close_intraday_positions` / `stops` submit paths) with multi-strategy
shared-symbol fixtures (IWM/KRE/NVDA/QQQ) and FAIL on current code; (b) grep-confirm the
new authority is actually CALLED from every live order-submit entry point — a module
that exists but isn't wired in does NOT pass. Extend `monitoring/position_manager.py`;
do not add a parallel unused system.

Execute IN ORDER. Per milestone: implement → FULL non-live suite (`py -3.13 -m pytest
tests/ -m "not live"`) → tick box → commit + push. Commit trailer:
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

**Guardrails:** OOM monkeypatch rule (never re-patch a function to call its own patched
name); no risk.*/paper-gate/kill-switch weakening; no new deps; NO autonomous live
orders. HALT on test failure, abnormal memory, or an architectural decision you can't
make safely.

---

## PHASE 1 — the execution core (build now, in order)

### [x] M1 — broker-state reconciler: broker position is the single source of truth
Make the broker the authority. Each cycle, derive the DB/in-memory position view FROM
the broker's actual positions + open orders; treat strategy "ownership" as metadata, not
an independent position claim. Every consumer (sizing, exits, stops, flatten, outcome
tracking) reads available qty from this reconciled view, never from a strategy's private
assumption. **Acceptance (prod path):** a test where the DB thinks a strategy holds qty
the broker doesn't have proves the system trusts the broker and computes available
correctly; wired into the real order-submit path.
  - **Completed:** 2026-06-05 by milestone-builder.
  - **Root cause closed:** Sprint 2 read broker `qty_available`, but two failure
    modes survived on the live path: (1) a market SELL submitted this pass is
    `accepted`, not yet `filled`, so a second strategy's exit microseconds later
    re-read the broker and saw the full long qty again → re-sold it (oversell into
    short); (2) `reconcile=True` cancel-then-resell re-inflated available. Fix: an
    in-run per-symbol sell-reservation ledger in `position_manager` that
    `available_to_sell(include_run_reservations=True)` subtracts, so a committed
    sell is netted out before the broker settles. Ledger is reset at the top of
    each pass.
  - **Wired into (real submit paths):** `auto_trader._process_exit`
    (auto_trader.py:1364, single- AND multi-strategy exits) and
    `close_intraday_positions` (close_intraday_positions.py:389, EOD flatten) both
    route through `position_manager.safe_submit_sell`, which now nets the run
    ledger; `auto_trader._maybe_attach_stop` (auto_trader.py:2625) caps stop qty
    via `available_to_sell`. Reset wired at `process_signals` (auto_trader.py:2756)
    and `close_intraday_positions` (close_intraday_positions.py:306).
  - **Behavioral test (fails-on-old / passes-on-new):**
    `tests/test_broker_truth_m1.py` drives the REAL `_process_exit`. (a) DB says 10,
    broker holds 4 → old code sold 10 (oversold 6 into short), new sells ≤4. (b) two
    strategies share one IWM position (10 held, 20 DB-claimed) → old code sold 20
    (the −$101k oversell), new sells ≤10. Both PROVEN red on pre-fix code.
  - **Handoff to M2/M3:** the stop-arming path (`stops.submit_atr_stop`) caps qty
    but does NOT yet coordinate owners or make stop submission idempotent — that is
    the remaining `40310000` wash-trade source and is explicitly M2/M3 scope.

### [x] M2 — single symbol-owner authority
One active owner (or one parent risk bucket) per symbol. No strategy may submit an
exit/stop/flatten for a symbol it does not solely own; shared symbols route through one
coordinator. **Acceptance:** with IWM/KRE/NVDA owned by multiple strategies, exactly one
valid exit/stop stack exists per live position; a second strategy's exit is rejected/
coordinated, never duplicated. Real submit path.
  - **Completed:** 2026-06-05 by milestone-builder. OPTION A (one owner per symbol).
  - **Owner registry (persisted, stateless-safe):** ownership is DERIVED from the live
    DB each pass — the owner is the strategy with the OLDEST still-open buy in
    `paper_trades` (same working-status set `_open_buy_for_pair` uses). No new schema /
    migration; reconstructs deterministically across the 15-min scheduled subprocess
    runs because `paper_trades` already persists. Helpers in `position_manager.py`:
    `open_buy_owners`, `symbol_owner`, `owns_symbol`, `entry_owner_conflict`.
  - **Wired into (real submit paths):** entry — `auto_trader._process_entry`
    (auto_trader.py:553) rejects a non-owner entry as `SKIP_SYMBOL_OWNED`; exit —
    `auto_trader._process_exit` (auto_trader.py:1345) returns `SKIP_NOT_OWNER` for a
    non-owner; stop — `auto_trader._maybe_attach_stop` (auto_trader.py:2673) suppresses
    a non-owner's protective stop (`status=skip_not_owner`).
  - **Behavioral test (fails-on-old / passes-on-new):** `tests/test_owner_authority_m2.py`
    drives REAL `_process_entry` / `_process_exit`. Proven RED on pre-M2 code: a second
    strategy's KRE entry submitted a BUY; a non-owner's IWM exit fired a SELL. New code
    rejects both; exactly one valid exit fires for the owner.

### [x] M3 — idempotent stop / flatten / sell
Before ANY sell/stop/flatten: query existing open orders for the symbol, compute
`available = position_qty − held_for_orders`, cancel/replace incompatible orders, submit
only net-available, and NEVER cross zero into a short for a long-only strategy.
**Acceptance:** `held_for_orders` reserving shares → submits only available, never
oversells; a duplicate flatten cancels/replaces rather than re-firing a failing SELL.
Real path; fails on current code.
  - **Completed:** 2026-06-05 by milestone-builder.
  - **Root cause closed:** `stops.submit_atr_stop` was a raw passthrough — re-arming
    a symbol STACKED a second SELL STOP (40310000 wash / double held_for_orders).
    New `position_manager.safe_submit_stop` reconciles (cancels) the resting SELL
    first (cancel/replace, not stack), caps qty to net-available (held_for_orders +
    run ledger), and submits only ≥1. Also fixed an ownership-release bug surfaced
    by M2: a resting protective stop (`order_type LIKE '%stop%'`, unfilled) no longer
    counts as a position-closing sell, so a still-protected long stays OWNED.
  - **Wired into (real submit path):** `auto_trader._maybe_attach_stop`
    (auto_trader.py:2682) routes the long-side protective stop through
    `safe_submit_stop`. Flatten/sell idempotency already lands via M1's
    `safe_submit_sell` (reconcile + run-ledger net) on `_process_exit` and
    `close_intraday_positions`.
  - **Behavioral test (fails-on-old / passes-on-new):** `tests/test_idempotent_stop_m3.py`
    drives REAL `_maybe_attach_stop`. Proven RED on pre-M3 code (re-arm stacked TWO
    SELL STOPs: `assert 2 == 1`). New code cancels the resting stop and leaves exactly
    one; a stop is never armed for more than the long qty.

### [x] M4 — exit-signal gating to real owned holdings
Only emit/record `long_exit` when the strategy has a live OWNED position (per M1/M2).
Kills the thousands-of-exits spam. **Acceptance:** positionless/paused strategy emits 0
exits; a real holding emits its single exit. Real signal path.
  - **Completed:** 2026-06-05 by milestone-builder.
  - **Root cause closed:** the scanners recorded a `long_exit` on EVERY bar in the
    scan window where the rule was true, for EVERY (strategy, symbol) — even ones the
    strategy never held → thousands of phantom exit signals/run + a SELL signal handed
    to non-owners.
  - **Wired into (real signal-record paths):** `intraday_fires.check_intraday_fires`
    (intraday_fires.py:194) and `trend_scanner.scan_trend_universe`
    (trend_scanner.py:191) now gate `db.record_signal(... long_exit)` on
    `position_manager.owns_symbol(conn, sid, symbol)`. A positionless/paused/non-owner
    strategy records zero exits. (`_process_exit` already enforces the same on the
    submit side via M1/M2.)
  - **Behavioral test (fails-on-old / passes-on-new):** `tests/test_exit_gating_m4.py`
    drives REAL `check_intraday_fires`. Proven RED on pre-M4 code (positionless +
    non-owner each recorded 1 phantom exit). New code records 0; an owned holding
    records exactly its single exit. Updated `test_trend_scanner`'s exit sanity test
    to seed the owned position M4 now requires.

### [x] M5 — paused-strategy position policy
Define + enforce: paused = no new entries AND no silent holding. On pause, flatten
holdings via the owner authority (or set an explicit carried flag). **Acceptance:**
pausing a strategy with holdings flattens them and stops new stop-arming. Real path.
  - **Completed:** 2026-06-05 by milestone-builder.
  - **Policy:** paused = no new entries (existing entry gate) AND no silent holding
    (new). On each EOD pass, `_flatten_paused_holdings` flattens every paused
    strategy's still-OWNED holdings via `position_manager.safe_submit_sell`
    (reconcile + cap-to-available + run-ledger net — never oversells). Once flat the
    symbol is unowned, so M2's owner gate also stops new stop-arming for it.
  - **Wired into (real submit path):** `auto_trader.process_signals` runs the M5
    pause-flatten pass BEFORE the signal loop (auto_trader.py, after `live_set`
    resolution, EOD-only, non-dry-run), routing each flatten to the strategy's own
    paper/live client via `_resolve_strategy_client`. New helper
    `position_manager.owned_symbols_for`. Flatten actions surface in the run report.
  - **Behavioral test (fails-on-old / passes-on-new):** `tests/test_paused_flatten_m5.py`
    drives REAL `process_signals` (non-dry-run, injected client). Proven RED on pre-M5
    code (paused strategy left silently holding — no SELL, still owner). New code
    submits the flatten SELL (≤ held qty), releases ownership, and leaves live
    strategies' holdings untouched.

### [x] M6 — stale-flatten audit + end-of-session flat assertion
Trace why intraday positions reach `stale_intraday_flatten_missed`; add an EOD assertion
that every intraday-owned position is flat or explicitly carried, failing/alerting
loudly otherwise. **Acceptance:** an unflattened intraday position at session end trips
the assertion; a clean session is silent. Real EOD path.
  - **Completed:** 2026-06-05 by milestone-builder.
  - **Audit (why positions reach the stale tag):** F2 opens an intraday outcome at
    entry and lets ONLY the EOD flatten close it. If that flatten is MISSED (crash,
    restart, schedule gap) OR the broker rejected/partial-filled the closing SELL, the
    position survives overnight and the outcome strands OPEN until a LATER session's
    bounded `sweep_stale_intraday_outcomes` closes it with
    `stale_intraday_flatten_missed`. The sweep is a band-aid — it never flagged that
    the flatten silently failed THIS session.
  - **Wired into (real EOD path):** new `close_intraday_positions.assert_intraday_flat`
    runs after the flatten loop inside `close_intraday_positions` (non-dry-run); it
    reads the live BROKER position for every intraday symbol just processed and fires a
    loud ERROR-log + telegram alert on any still-held (non-flat) symbol. Result surfaces
    as `flat_assert` in the close-out return.
  - **Behavioral test (fails-on-old / passes-on-new):** `tests/test_eod_flat_assert_m6.py`
    drives REAL `close_intraday_positions`. An unflattened broker position (SELL didn't
    actually flatten) trips the assertion and fires the alert; a clean session is
    silent; a non-reporting stub broker raises no false alarm. Pre-M6 had no flat
    assertion (param/function didn't exist) → tests fail on old code.

### [x] M7 — post-fill stop-protection verification
After every buy fill, verify a valid protective stop is attached (or a verified
equivalent open order); alert loudly if a fill is left unprotected. **Acceptance:** a
fill without a stop raises the alert; a protected fill passes.
  - **Completed:** 2026-06-05 by milestone-builder.
  - **Wired into (real submit path):** `auto_trader._process_entry` now calls
    `position_manager.verify_fill_protected` right after `_maybe_attach_stop`. A fill is
    PROTECTED when this run's stop submitted (status=='submitted' + order_id) OR the
    broker shows a working SELL STOP for the symbol (verified equivalent via
    `has_protective_stop`/`open_stop_orders`). An unprotected fill (stop submit failed /
    rejected) fires a loud ERROR log + telegram alert. When stops are globally disabled
    (`stop_info is None`) verification is skipped — no false alarm. Result surfaces as
    `stop_protection` in the BUY action.
  - **Behavioral test (fails-on-old / passes-on-new):** `tests/test_stop_protection_m7.py`
    drives REAL `_process_entry`. A failed stop submission (naked ENPH/AVGO long) raises
    the alert; a clean stop submit passes silently; a resting broker stop counts as
    protected; no-stops-config raises no false alarm. Pre-M7 had no `stop_protection`
    key / verification → tests fail on old code.

### [x] M8 — separate performance from cleanup
Tag `reconciled_no_position` + `stale_intraday_flatten_missed` so they NEVER enter
fresh-trading expectancy/win-rate used by `strategy_health`, the eligibility gate, or the
report. **Acceptance:** strategy stats computed over fresh closes only; report shows a
fresh-vs-cleanup split.
  - **Completed:** 2026-06-05 by milestone-builder.
  - **Cleanup reason set:** `strategy_health.CLEANUP_EXIT_REASONS` =
    {reconciled_no_position, stale_intraday_flatten_missed, broker_reconcile,
    orphan_sweep, reconcile_close} + `_fresh_only_clause()` helper. NULL exit_reason
    treated as fresh (legacy rows).
  - **Wired into (real stats/gate paths):** excluded from `_closed_returns_for_strategy`,
    `closed_returns_in_class`, `_live_outcomes_for_strategy` (auto-pause divergence
    input) in `strategy_health.py`, and from the eligibility gate
    `auto_trader._is_eligible`. The report's `[FRESH ACTIVITY vs RECONCILIATION]`
    section in `schedulers/pg_report_data.py` shows the split.
  - **Report split date-boundary fix (M8/M9):** the fresh-vs-cleanup split keyed on
    `updated_at` (UTC wall-clock when the row was WRITTEN) — any close written after
    00:00 UTC (every EOD reconcile, ~17:00 PT) rolled to the next calendar day and
    silently dropped today's split to zero. Switched to
    `substr(COALESCE(exit_ts, updated_at),1,10)` (the trade's session date).
  - **Behavioral tests (fails-on-old / passes-on-new):**
    `tests/test_perf_cleanup_split_m8.py` drives REAL `_is_eligible` +
    `closed_returns_in_class` + `_live_outcomes_for_strategy`; proven RED on pre-M8
    code (10 cleanup closes inflated n 2→12, leaked 0.0% returns). The pre-existing
    `tests/test_pg_report_data_sprint2.py::test_report_renders_new_sections` (M8/M9
    scope) is now GREEN.

### [x] M9 — correct exposure/accounting in the report
Fix `schedulers/pg_report_data.py` to compute exposure from long/short market value +
equity (not `portfolio_value − cash`), and alert loudly on any `short_market_value < 0`
for this long-only system. Re-install note for the `~/.hermes/scripts/` copy
(`tr -d '\r'`) — do not touch the WSL copy. **Acceptance:** correct long/short exposure
reported; net-short alert fires; runs against the live DB.
  - **Completed:** 2026-06-05 by milestone-builder.
  - **Schema (additive, low-risk):** added nullable `long_market_value` /
    `short_market_value` columns to `equity_snapshots` (DDL + idempotent
    `_ensure_columns` ALTER; old rows stay NULL). `db.record_equity_snapshot` +
    `config.utils.get_account_summary` now capture them from the Alpaca account;
    `auto_trader.process_signals` passes them into the snapshot.
  - **Report (real path):** `[PORTFOLIO]` now reports long MV, short MV, and gross/net
    exposure as a % of equity, and fires `*** ALERT: SHORT MARKET VALUE < 0 ON A
    LONG-ONLY SYSTEM ***` on any negative short MV (the oversell-into-short signature).
    Pre-M9 snapshots (NULL MV) fall back to the legacy `portfolio_value−cash` proxy
    (labelled), so the report never crashes on old data.
  - **Behavioral test (fails-on-old / passes-on-new):** `tests/test_report_exposure_m9.py`
    drives REAL `pg_report_data.py` + the recorder. A negative short MV trips the loud
    alert; a clean long-only book reports gross exposure and is silent; the recorder
    persists long/short MV; a legacy row uses the proxy. Pre-M9 lacked the capability
    (recorder rejected the kwargs, report had no exposure/alert) → tests fail on old.
  - **RE-INSTALL NOTE (manual, not done here):** `schedulers/pg_report_data.py` changed.
    The Hermes cron runs the `~/.hermes/scripts/` copy. Refresh it with a CRLF strip:
    `tr -d '\r' < schedulers/pg_report_data.py > ~/.hermes/scripts/pg_report_data.py`.
    Do NOT touch the WSL copy.

---

## PHASE 2 — risk hardening + disciplined reintroduction (after Phase 1 is prod-verified)

### [ ] M10 — trend loser cap (applies to the live Donchian book now)
Per-position max-loss / tighter stop so single-name blowups are capped (ENPH −16%,
AVGO −16% this week). Keep Donchian active; cap the tail only.

### [ ] M11 — intraday time-stop / max-loss overlay (for when intraday returns)
Hard per-intraday-position max-loss + max-hold-time; force-close on breach.

### [ ] M12 — strategy reintroduction framework
Evidence gate (≥20 FRESH closes before size-up; depends on M8), one-strategy-at-a-time
re-enable, kill gates, and IWM/KRE/NVDA/QQQ conflict regression fixtures. This is the
controlled on-ramp back to multi-strategy — used only after Phase 1 holds in prod and
Donchian has shown a clean run.
