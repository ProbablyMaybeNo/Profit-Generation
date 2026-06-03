# Audit Fixes Sprint — REMAINING items (continuation of docs/AUDIT_FIXES_SPRINT.md)

Continuation batch after a mid-sprint crash. F1 and F4 from the original
`docs/AUDIT_FIXES_SPRINT.md` are already committed (`e4903d4`, `6537490`).
This file carries only the items NOT yet landed. Execute milestones IN ORDER.

For EACH: implement to existing conventions → run the FULL non-live suite
(`py -3.13 -m pytest tests/ -m "not live"`) → if green, tick the box here AND in
`docs/AUDIT_FIXES_SPRINT.md` → commit + push to main. End commit messages with:
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

**CRITICAL LESSON (carried from the audit):** the previous sprint's M1 passed all
unit tests but was DEAD IN PRODUCTION because the gaps were in *wiring* (live callers
didn't supply args / paths weren't invoked). For F2/F3/F5 you MUST include at least one
**integration/wiring test** that exercises the real production call path (the actual
`daily_report` reconcile entry point, the `stops.reconcile_stop_fills` path, etc.), not
just the unit in isolation. A unit-only test that would still pass with the bug present
does NOT satisfy acceptance.

Guardrails: do NOT weaken any `risk.*` limit, the paper-mode gate, or the kill switch.
No new deps / no requirements changes without flagging. If a milestone is genuinely
ambiguous, HALT and report the specific decision needed — do not guess, do not continue
past a halt. Live-API tests (`-m live`) may be skipped (note it). No live broker calls,
no orders.

---

## [x] F3 (P1, SilentFailure) — MFE/MAE/exit_reason 100% NULL in production

**Completed:** 2026-06-03 by milestone-builder — wiring test proven to FAIL on old code (mfe NULL) and PASS with the fix; full non-live suite green (2297 passed).

**STATUS: PARTIALLY DONE — uncommitted work already on disk. Do NOT rewrite from scratch.**
The pre-crash session already:
- edited `monitoring/daily_report.py` (`persist_report` now passes
  `bars_fetcher=_build_default_bars_fetcher()` into `reconcile_signals`), and
- added `tests/test_daily_report_reconcile_excursion.py` (a wiring test driving the real
  `persist_report` path, asserting a closed outcome lands with non-NULL mfe/mae).

Your job: **finish and verify it.** Review both for correctness, run the full non-live
suite, and confirm the new test actually FAILS on the old code (mfe/mae NULL) and PASSES
with the change. If solid, tick this box + the one in `AUDIT_FIXES_SPRINT.md` and commit
BOTH the source change and the test together. If the test is incomplete/incorrect, fix it
to meet the acceptance bar below before committing.

**Evidence:** 1,853 closed outcomes, all `mfe_pct/mae_pct=NULL`, all
`exit_reason='long_exit_signal'`. The live reconcile previously called
`reconcile_signals(conn)` with no `bars_fetcher`, so the M1 plumbing was never fed.

**Acceptance:** integration test exercising the live `daily_report` reconcile path (not
just `close_for_exit` in isolation) proving a closed outcome lands with non-NULL
mfe_pct/mae_pct. Full non-live suite green.

---

## [x] F2 (P1, SilentFailure) — intraday signals never get outcome rows

**Completed:** 2026-06-03 by milestone-builder — `reconcile_signals` gained an
`open_only` mode; `daily_report.persist_report` now runs a second intraday pass
(`bar_intervals=["1m","5m","15m","1d-intraday"]`, `open_only=True`) on the EOD
schedule that OPENS intraday outcome rows without letting an intraday scanner
long_exit pre-empt the EOD flatten. Wiring test drives the real persist_report +
close_intraday_positions path (open → eod_close with MFE/MAE); full non-live
suite green (2299 passed).

**Evidence:** 0 outcomes for any non-1d signal vs 16,884 1m signals / 65 open intraday
buys. The only live reconcile (`daily_report.py`) defaults `bar_interval='1d'`, so
`open_for_entry` never runs for intraday entries → M1's `_close_outcome_for_eod` can never
find an open outcome (dead in prod).

**Fix:** add an intraday reconcile pass (on the intraday or EOD schedule — pick the
consistent spot, document it) calling `reconcile_signals(conn,
bar_intervals=["1m","5m","15m","1d-intraday"], bars_fetcher=<intraday fetcher>)` so
intraday entries open outcome rows; the existing M1 flatten close then resolves them.
Avoid double-opening 1d outcomes (keep the 1d pass and intraday pass non-overlapping).

**Acceptance:** integration test proving an intraday (`bar_interval='1m'`) entry signal
produces an OPEN outcome row via the new pass, and that the EOD flatten then closes it with
mfe/mae + exit_reason='eod_close'. Full non-live suite green.

---

## [x] F5 (P2, SilentFailure) — stop/trailing exits record no MFE/MAE

**Completed:** 2026-06-03 by milestone-builder — `reconcile_stop_fills` gained an
optional `bars_fetcher` and now computes MFE/MAE over entry..fill, persisting them
with `exit_reason='stop_loss_atr'`. `_process_exit` now closes the outcome itself on
a trailing-stop trip (`exit_reason='trailing_stop'` + MFE/MAE) so the later generic
1d signal-exit reconcile finds no open outcome and can't overwrite it; `bars_fetcher`
threaded into both `_process_exit` call sites. Tests: stop-loss close lands MFE/MAE,
trailing-stop close lands MFE/MAE with the correct reason; full non-live suite green
(2302 passed).

**Evidence:** `monitoring/stops.py:265-269` closes with `exit_reason='stop_loss_atr'` and
no mfe/mae; trailing exit in `auto_trader._process_exit` (~1272-1322) gets its outcome
closed by the generic 1d signal-exit reconcile, overwriting the true reason and omitting
excursion.

**Fix:** in `reconcile_stop_fills`, compute MFE/MAE (`excursion.compute_mfe_mae` with a
bars_fetcher) and pass them + the real `exit_reason` into `close_outcome`; ensure a
trailing exit's true `exit_reason='trailing_stop'` reaches the outcome row rather than
being overwritten by the signal-exit close.

**Acceptance:** test proving a stop-loss close and a trailing-stop close each land in
`outcomes` with non-NULL mfe/mae and the correct exit_reason. Full non-live suite green.

---

## [x] F7 (P3, Optimization) — `no_open_position` skip spam (187,814 rows)

**Completed:** 2026-06-03 by milestone-builder — `_process_exit` no longer writes a
`no_open_position` skip row when flat (the normal case); the SKIP_NO_POSITION control
flow / return is unchanged so no trading decision moved. Existing test updated to
assert zero skip rows while still returning SKIP_NO_POSITION. Full non-live suite green
(2302 passed).

**Evidence:** `intraday_skips` gate distribution: `no_open_position`=187,814 — the exit
scanner records a skip per (strategy,symbol,bar) when flat. Pure noise; bloats the table.

**Fix:** stop persisting `no_open_position` as a skip row (it's the normal flat case). Keep
the control-flow skip; just don't write the DB row (or gate it behind a debug flag). Don't
change any trading decision — only the logging/persistence.

**Acceptance:** test proving the exit scanner no longer writes a `no_open_position` skip row
while still skipping correctly. Full non-live suite green.

---

## [x] F6 (P3, Blockage) — `_is_eligible` should count intraday outcomes, not just 1d

**Completed:** 2026-06-03 by milestone-builder — `_is_eligible` takes a
`bar_interval`; a 1d signal counts 1d closed outcomes, any non-1d (intraday)
signal counts the strategy's non-1d closed outcomes. Classes kept separate (no
pooling); thresholds untouched. Call site passes the signal's bar_interval.
Tests prove an intraday signal is judged on its intraday record (eligible) while
its 1d view stays empty, and a 1d strategy's count is unaffected by intraday
outcomes. Full non-live suite green (2304 passed).

**RUN LAST — depends on F2** (intraday outcomes must exist before the gate can measure them).
Owner approved building this (fake money, lean risky on trade-behavior changes).

**Evidence:** `monitoring/auto_trader.py:154-159` — the eligibility query filters
`s.bar_interval='1d'`, so an intraday strategy's edge is never gauged by the gate; intraday
strategies can't graduate or be judged on their actual intraday record.

**Fix:** make the eligibility query honor the signal's bar_interval — i.e. when evaluating an
intraday strategy/signal, count that strategy's intraday closed outcomes (not just 1d). Keep
1d strategies measuring 1d outcomes. Don't pool incomparable interval stats blindly — scope
the outcome set to the strategy's own interval(s). Do NOT weaken the numeric thresholds
(min_outcomes / min_mean_ret / min_sharpe); only fix WHICH outcomes are counted.

**Acceptance:** test proving an intraday strategy's eligibility is computed from its intraday
closed outcomes, and 1d strategies are unchanged. Full non-live suite green.

---

### Verification

## [x] F8 (verification) — re-measure `price_too_high` / `qty_floored_to_cap_min`

**Verified:** 2026-06-03. Measured against `data/trading.db` `intraday_skips` at matched
signal volume:

| Day | Total skips | `price_too_high` | share |
|-----|------------:|-----------------:|------:|
| 2026-06-01 | 99,468 | 6,238 | 6.3% |
| 2026-06-02 | 99,905 | 84 | 0.08% |
| 2026-06-03 (partial) | 16,603 | 0 | 0% |

The M3 sizing fix (`b787627`) collapsed the `price_too_high` veto by 98.7% at constant
volume (06-01 vs 06-02 both ~99k skips). `qty_floored_to_cap_min` records NO skip rows —
the path that used to veto now buys 1 share at cap-min instead of discarding the signal.
Remaining `price_too_high` hits are genuine (cap can't afford even one share). PASS.

**Operational note:** F7 (`no_open_position` skip suppression, `5f2afbe`) is committed but
`no_open_position` rows are still being written on 2026-06-03 (15,940) — the live trading
process must be RESTARTED to pick up F7 / F5-LIVE / F2-SAFETY. Code is correct; runtime is
stale until the next process bounce.
