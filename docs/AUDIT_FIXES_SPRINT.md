# Audit Fixes Sprint — from docs/SYSTEM_AUDIT_2026-06-03.md

Batch of builder tasks from the 2026-06-03 systems audit. Execute milestones IN ORDER.
For EACH: implement to existing conventions → run the FULL non-live suite
(`py -3.13 -m pytest tests/ -m "not live"`) → if green, tick the box here → commit + push
to main. Commit style per repo; end messages with:
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

**CRITICAL LESSON FROM THIS AUDIT:** the previous sprint's M1 passed all unit tests but
was DEAD IN PRODUCTION because the gaps were in *wiring* (live callers didn't supply
args / paths weren't invoked), not in units. Therefore: for F1–F5 you MUST add at least
one **integration/wiring test** that exercises the real production call path (e.g. the
actual `daily_report` reconcile entry point, or `strategy_fires.check_fires` end-to-end),
not just the unit in isolation. A unit-only test that would still pass with the bug
present does NOT satisfy acceptance.

Guardrails: do NOT weaken any `risk.*` limit, the paper-mode gate, or the kill switch.
No new deps / no requirements changes without flagging. If a milestone is genuinely
ambiguous, HALT on it and report the specific decision needed — do not guess, do not
continue past a halt. Live-API tests (`-m live`) may be skipped (note it). No live broker
calls, no orders.

---

## [x] F1 (P1, Blockage) — rsi2/rsi14-oversold can never fire (SMA200 starves)

**Evidence:** `monitoring/strategy_fires.py:93-94` sets `start = as_of - 120 days` (~84
trading bars). Both `strategies/generated/rsi2_oversold.py:23-24` and `rsi14_oversold.py:23-24`
gate entry on `close > sma200` (200-bar SMA) → 100% NaN on 84 bars → never fires. Empirical:
live `check_fires` shows bollinger firing but no rsi2/rsi14; a 294-bar window yields 14–23.

**Fix:** raise the `check_fires` lookback to cover ≥200 trading bars + buffer (e.g.
`start = as_of - timedelta(days=320)`). Keep per-strategy `active_on` fetch unchanged. No
other commit needs changing — strategies/seeding/resolution are correct; bollinger (≤34 bars)
is unaffected.

**Acceptance:** integration test on `check_fires` (or its bar-load) proving the loaded
history is long enough that a 200-bar SMA is non-NaN on the latest bar, and that an rsi2/rsi14
oversold setup CAN produce a fire on a constructed series. Full non-live suite green.

---

## [x] F4 (P2, Bug) — excursion window TZ/format mismatch (do before F2/F3 produce data)

**Evidence:** `intraday_bars.ts_utc='...T20:46:00+00:00'` (offset-aware UTC) vs signal
`bar_ts='...T15:57:00'` (naive, ~ET, this box is non-UTC). `monitoring/excursion.py::_in_window`
does raw lexical `<`/`>` string compare → wrong bars windowed.

**Fix:** parse both bar and signal timestamps to aware UTC datetimes before comparing (parse →
astimezone(utc) → compare), or canonicalize entry/exit ts to the same UTC ISO the bars use.
Handle naive timestamps explicitly (assume the box/ET convention already used elsewhere — match
existing tz handling in the codebase, don't invent a new one).

**Acceptance:** unit test with mixed-format timestamps (offset-aware bar ts + naive signal ts
across a real wall-clock offset) proving the correct bars are included and MFE/MAE are computed
over the right window. Full non-live suite green.

---

## [x] F3 (P1, SilentFailure) — MFE/MAE/exit_reason 100% NULL in production

**Evidence:** 1,853 closed outcomes, all `mfe_pct/mae_pct=NULL`, all
`exit_reason='long_exit_signal'`. `monitoring/daily_report.py:371` calls
`reconcile_signals(conn)` with no `bars_fetcher` (and never sets a non-default exit_reason),
so the M1 plumbing is never fed.

**Fix:** construct a daily `bars_fetcher` (reuse `auto_trader._build_default_bars_fetcher`) and
pass it into the live `reconcile_signals` call. Ensure the signal-exit close records MFE/MAE.
(exit_reason for true signal exits staying `long_exit_signal` is correct; stop/trailing reasons
are F5.)

**Acceptance:** integration test exercising the live `daily_report` reconcile path (not just
`close_for_exit` in isolation) proving a closed outcome lands with non-NULL mfe_pct/mae_pct.
Full non-live suite green.

---

## [ ] F2 (P1, SilentFailure) — intraday signals never get outcome rows

**Evidence:** 0 outcomes for any non-1d signal vs 16,884 1m signals / 65 open intraday buys.
The only live reconcile (`daily_report.py:371`) defaults `bar_interval='1d'`, so
`open_for_entry` never runs for intraday entries → M1's `_close_outcome_for_eod` can never find
an open outcome (dead in prod).

**Fix:** add an intraday reconcile pass (on the intraday or EOD schedule — pick the consistent
spot, document it) calling `reconcile_signals(conn, bar_intervals=["1m","5m","15m","1d-intraday"],
bars_fetcher=<intraday fetcher>)` so intraday entries open outcome rows; the existing M1 flatten
close then resolves them. Avoid double-opening 1d outcomes (keep the 1d pass and intraday pass
non-overlapping).

**Acceptance:** integration test proving an intraday (`bar_interval='1m'`) entry signal produces
an OPEN outcome row via the new pass, and that the EOD flatten then closes it with mfe/mae +
exit_reason='eod_close'. Full non-live suite green.

---

## [ ] F5 (P2, SilentFailure) — stop/trailing exits record no MFE/MAE

**Evidence:** `monitoring/stops.py:265-269` closes with `exit_reason='stop_loss_atr'` and no
mfe/mae; trailing exit in `auto_trader._process_exit` (~1272-1322) gets its outcome closed by the
generic 1d signal-exit reconcile, overwriting the true reason and omitting excursion.

**Fix:** in `reconcile_stop_fills`, compute MFE/MAE (`excursion.compute_mfe_mae` with a
bars_fetcher) and pass them + the real `exit_reason` into `close_outcome`; ensure a trailing
exit's true `exit_reason='trailing_stop'` reaches the outcome row rather than being overwritten by
the signal-exit close.

**Acceptance:** test proving a stop-loss close and a trailing-stop close each land in `outcomes`
with non-NULL mfe/mae and the correct exit_reason. Full non-live suite green.

---

## [ ] F7 (P3, Optimization) — `no_open_position` skip spam (187,814 rows)

**Evidence:** `intraday_skips` gate distribution: `no_open_position`=187,814 — the exit scanner
records a skip per (strategy,symbol,bar) when flat. Pure noise; bloats the table.

**Fix:** stop persisting `no_open_position` as a skip row (it's the normal flat case). Keep the
control-flow skip; just don't write the DB row (or gate it behind a debug flag). Don't change any
trading decision — only the logging/persistence.

**Acceptance:** test proving the exit scanner no longer writes a `no_open_position` skip row while
still skipping correctly. Full non-live suite green.

---

## [ ] F6 (P3, Blockage) — `_is_eligible` should count intraday outcomes, not just 1d

**RUN LAST — depends on F2** (intraday outcomes must exist before the gate can measure them).
Owner approved building this (fake money, lean risky on trade-behavior changes).

**Evidence:** `monitoring/auto_trader.py:154-159` — the eligibility query filters
`s.bar_interval='1d'`, so an intraday strategy's edge is never gauged by the gate; intraday
strategies can't graduate or be judged on their actual intraday record.

**Fix:** make the eligibility query honor the signal's bar_interval — i.e. when evaluating an
intraday strategy/signal, count that strategy's intraday closed outcomes (not just 1d). Keep
1d strategies measuring 1d outcomes. Don't pool incomparable interval stats blindly — scope the
outcome set to the strategy's own interval(s). Do NOT weaken the numeric thresholds
(min_outcomes / min_mean_ret / min_sharpe); only fix WHICH outcomes are counted.

**Acceptance:** test proving an intraday strategy's eligibility is computed from its intraday
closed outcomes (e.g. an intraday strategy with enough winning intraday outcomes becomes
eligible, where before it saw zero), and 1d strategies are unchanged. Full non-live suite green.

---

### Deferred (verification only — not a code change)
- **F8** (re-measure `price_too_high` / `qty_floored_to_cap_min` after next RTH session) — I'll
  run this measurement after the next live session; nothing to build.
