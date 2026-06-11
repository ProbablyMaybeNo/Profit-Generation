# System Audit — Profit Generation — 2026-06-03

**Auditor:** systems-auditor (read-only). No code/config/DB modified; no broker orders placed.
**Scope:** Charter general sweep + code-level verification of the 7 commits from 2026-06-02/03
(0a15b18, 9064ff9, 6750198, 743923e, b787627, 239367b, 4c5a53e).
**Method:** git diff review, source read, read-only SQLite queries against `data/trading.db`,
empirical re-runs of `strategy_fires` and the compute_fns, and the touched-module test suites
(136 passed).

---

## 1. Executive summary (highest-priority findings)

1. **P1 — 2 of 3 newly-promoted strategies can never fire (EOD path).** `rsi2-oversold` and
   `rsi14-oversold` both require a 200-bar SMA filter, but `strategy_fires.check_fires` loads only
   120 *calendar* days (~84 trading bars). `sma200` is 100% NaN → `close > sma200` is always
   False → **zero fires**. Proven empirically: live window yields 0 entries; a 294-bar window
   yields 14–23. `bollinger-bandit` (20-bar) is fine and does fire (XBI).
2. **P1 — Intraday outcome capture (M1) is broken at the OPEN end, not just the close.** There are
   **0 outcome rows for any intraday signal** (16,884 1m signals, 65 open intraday buys, 0 outcomes).
   The live reconcile (`daily_report.py:371`) defaults `bar_interval='1d'`, so `open_for_entry`
   never runs for intraday entries. M1's `_close_outcome_for_eod` can therefore never find an open
   outcome to close — the new flatten-path capture is dead code in production.
3. **P1 — MFE/MAE/exit_reason still 100% NULL in production.** 1,853 closed outcomes: every one has
   `mfe_pct=NULL`, `mae_pct=NULL`, `exit_reason='long_exit_signal'`. M1 added `bars_fetcher` support
   to `close_for_exit`/`reconcile_signals`, but the only live caller passes **no** `bars_fetcher`.
   Stop and trailing exits also bypass it. Stop/trailing/pyramid effectiveness remains unmeasurable.
4. **P2 — M1 excursion windowing has a timestamp-format/timezone mismatch.** `intraday_bars.ts_utc`
   is UTC-with-offset (`...T20:46:00+00:00`); entry/exit timestamps are naive local-ish
   (`...T15:57:00`). `excursion._in_window` does raw string comparison, so even if outcomes existed,
   the window would silently drop or mis-include bars. Compounds finding #2/#3.
5. **P2 — M1 stop/trailing exits never record MFE/MAE.** `stops.reconcile_stop_fills` (stop_loss_atr)
   and the trailing-stop exit in `auto_trader._process_exit` close outcomes without excursion data.
   M1 only instrumented the signal-exit and EOD-flatten code paths.
6. **P3 — `_is_eligible` only ever measures the 1d outcome set.** Its query filters
   `s.bar_interval='1d'`, so intraday strategies' edge is never gauged by the eligibility gate. Fine
   today (intraday strategies are grace/quarantined), but a latent measurement blind spot.

**Verified GOOD:** M0 sub-penny quantization (correct, applied at the broker boundary + in
auto_trader), M2 quarantine (3 strategies paused indefinitely in the live DB), M3 qty<1 veto fix
(logic correct; cap-affordable floor added), M4 size-to-edge (PF boost clamped to 0.20, intraday
floor wired, `merge_config` extended), EOD cancel-resting-orders flatten (correct, best-effort,
settle delay). All touched-module tests green (136 passed).

---

## 2. Findings table

| ID | Sev | Category | Title | Evidence | Root cause | Recommended fix | Effort | Conf |
|----|-----|----------|-------|----------|-----------|-----------------|--------|------|
| F1 | P1 | Blockage | rsi2/rsi14-oversold can never fire (SMA200 starves on 120d window) | `strategy_fires.py:93-94` (`start = as_of - 120 days`); empirical: live window 84 bars, `sma200` non-NaN = 0; `compute_rsi2_oversold` line 23-24; live `check_fires` run shows bollinger fires, rsi2/rsi14 absent; 294-bar window → 14–23 entries | Lookback too short for the 200-period SMA trend filter in both generated compute_fns | Extend `check_fires` lookback to ≥ ~300 calendar days (covers 200 trading bars + buffer); keep the per-strategy `active_on` symbol fetch | S | High |
| F2 | P1 | SilentFailure | Intraday signals never get outcome rows (open end missing) | Query: 0 outcomes for any non-1d signal vs 16,884 1m signals / 65 open intraday buys; `daily_report.py:371` calls `reconcile_signals(conn)` with no `bar_intervals` (defaults `'1d'`) | The only live reconcile path is 1d-only; `open_for_entry` never runs for intraday entries | Add an intraday reconcile call passing `bar_intervals=["1m","5m","15m","1d-intraday"]` (and a `bars_fetcher`) on the intraday/EOD schedule | M | High |
| F3 | P1 | SilentFailure | MFE/MAE/exit_reason still 100% NULL in prod | Query: 1,853 closed outcomes, mfe_pop=0, mae_pop=0, all `exit_reason='long_exit_signal'`; `daily_report.py:371` passes no `bars_fetcher` | M1 wiring added params but live caller doesn't supply them; stop/trailing closes bypass entirely | Pass a `bars_fetcher` into the live `reconcile_signals`; thread it through stop/trailing closes (F5) | M | High |
| F4 | P2 | Bug | Excursion window TS/TZ mismatch | `intraday_bars.ts_utc='...T20:46:00+00:00'` vs signal `bar_ts='...T15:57:00'` (naive, ~ET); `excursion._in_window` (`monitoring/excursion.py`) does lexical `ts < entry_ts` | Comparing offset-aware UTC strings against naive local-time strings; also a real wall-clock TZ delta on this non-UTC box | Normalize both sides to UTC ISO before windowing (parse → astimezone(utc) → compare), or store one canonical TZ for entry/exit ts | M | High |
| F5 | P2 | SilentFailure | Stop/trailing exits record no MFE/MAE | `stops.py:265-269` (`close_outcome` no mfe/mae); `auto_trader._process_exit` 1300-1322 submits sell, relies on 1d signal-exit reconcile | M1 only touched `close_for_exit`/EOD flatten | Pass `mfe_pct/mae_pct` (+ correct `exit_reason`) into `close_outcome` in `reconcile_stop_fills`; ensure trailing exits resolve via a reconcile that computes excursion | M | High |
| F6 | P3 | Blockage | `_is_eligible` measures 1d outcomes only | `auto_trader.py:154-159` query filters `s.bar_interval='1d'` | Eligibility gate ignores intraday outcome history | Make the eligibility query honor the signal's bar_interval (or a per-class outcome set) once intraday outcomes exist (depends on F2) | S | Med |
| F7 | P3 | Optimization | `no_open_position` skip spam (187,814 rows) | `intraday_skips` gate dist: `no_open_position`=187,814 | Exit scanner records a skip per (strategy,symbol,bar) with no position; pure noise, bloats `intraday_skips` | Don't persist `no_open_position` as a skip row (it's the normal case), or sample/aggregate it | S | High |
| F8 | P3 | Optimization | M3 effectiveness not yet provable | `price_too_high`: 6,238 (Jun-1, pre-fix) → 84 (Jun-2); fix landed Jun-2 21:00 UTC, market closed; no post-fix RTH session yet | Fix landed end-of-day | Re-measure `price_too_high` and `qty_floored_to_cap_min` after the next RTH session | S | Med |

---

## 3. Per-finding detail (P1/P2)

### F1 (P1) — rsi2/rsi14-oversold can never fire via the EOD path
`monitoring/strategy_fires.py:93-94` sets `start = (as_of - timedelta(days=120))`. Loading
QQQ for `as_of=2026-06-02` returns **84 bars**; `df['close'].rolling(200).mean()` has **0**
non-NaN values. Both generated compute_fns gate entry on `close > sma200`:
`strategies/generated/rsi2_oversold.py:23-24` and `rsi14_oversold.py:23-24`. With NaN SMA the
condition is False (then `.fillna(False)`), so neither ever emits a `long_entry`. Empirical proof:
a live `python -m monitoring.strategy_fires 2026-06-02` run lists `bollinger-bandit` firing on XBI
but **no** rsi2/rsi14 fires; re-running the compute_fns on a 294-bar window produces 14–23 (rsi2)
and up to 14 (rsi14) entries. **Fix:** raise the `check_fires` lookback to ≥ ~300 calendar days
(e.g. `as_of - timedelta(days=320)`) so the 200-trading-bar SMA is populated. No other commit is
needed — the strategies, DB seeding, and compute resolution are all correct. bollinger-bandit is
unaffected (needs only ~34 bars).

### F2 (P1) — Intraday outcomes never opened; M1 flatten capture is dead in prod
The live outcome reconciler is `monitoring/daily_report.py:371`:
`return outcome_tracker.reconcile_signals(conn)` — no `bar_intervals`, so it defaults to `'1d'`
(`outcome_tracker.py:149-194`). `open_for_entry` is therefore never called for intraday entries.
DB confirms: `SELECT count(*) FROM outcomes o JOIN signals s ON s.id=o.signal_id WHERE
s.bar_interval!='1d'` → **0**, against 16,884 1m signals and 65 open intraday paper buys. M1's
`close_intraday_positions._close_outcome_for_eod` calls `_open_outcome_for_signal` which selects
`outcomes WHERE signal_id=? AND status='open'` — always None for intraday → it returns False and
records nothing. **Fix:** add an intraday reconcile pass (on the intraday or EOD schedule) calling
`reconcile_signals(conn, bar_intervals=["1m","5m","15m","1d-intraday"], bars_fetcher=<intraday
fetcher>)` so intraday entries get open outcome rows; only then can the M1 flatten close them.

### F3 (P1) — MFE/MAE/exit_reason 100% NULL in production
`SELECT status, sum(mfe_pct IS NOT NULL), sum(mae_pct IS NOT NULL) FROM outcomes GROUP BY status`
→ closed 1,853 (mfe 0, mae 0), open 195 (0,0). `SELECT exit_reason, count(*) ... GROUP BY` →
`long_exit_signal` 1,853 (only value). M1 added `bars_fetcher`/`exit_reason` plumbing to
`outcome_tracker.close_for_exit` and `reconcile_signals`, but the sole live caller
(`daily_report.py:371`) passes neither, and `reconcile_signals` threads `bars_fetcher` through to
`close_for_exit` without ever setting a non-default `exit_reason`. So even the 1d path records NULL
excursion. **Fix:** construct a daily `bars_fetcher` (the project already has
`auto_trader._build_default_bars_fetcher`) and pass it into the live `reconcile_signals`. This is
the single change that turns on MFE/MAE for the EOD signal-exit majority; combine with F5 for
stop/trailing coverage and F2 for intraday coverage.

### F4 (P2) — Excursion windowing TZ/format mismatch
`excursion._in_window(ts, entry_ts, exit_ts)` (`monitoring/excursion.py`) compares strings with
`<`/`>`. In production the bars carry `ts_utc='2026-06-02T20:46:00+00:00'` (offset-aware UTC) while
signal `bar_ts='2026-06-02T15:57:00'` (naive, and ~5h behind — this box is non-UTC/ET). Lexical
comparison of an offset-suffixed UTC string against a naive local string is not a valid temporal
comparison, so windowed MFE/MAE would silently include/exclude the wrong bars once F2/F3 are fixed.
**Fix:** parse both bar and signal timestamps to aware UTC datetimes before comparing (or persist
entry/exit ts in the same canonical UTC ISO format the bars use), rather than raw string compare.

### F5 (P2) — Stop and trailing exits omit MFE/MAE
`monitoring/stops.py:265-269` closes the outcome with `exit_reason='stop_loss_atr'` and **no**
`mfe_pct/mae_pct`. The trailing-stop exit (`auto_trader._process_exit`, lines ~1272-1322) submits
the sell and records the paper_trade with `exit_reason='trailing_stop'` in the *return dict* but
the outcome itself is closed later via the 1d signal-exit reconcile (which forces
`exit_reason='long_exit_signal'` and no excursion). So neither stop nor trailing exits ever land in
`outcomes` with their true reason or excursion. **Fix:** in `reconcile_stop_fills`, compute MFE/MAE
(via `excursion.compute_mfe_mae` with a bars_fetcher) and pass it plus the real `exit_reason` into
`close_outcome`; ensure the trailing exit's true `exit_reason` reaches the outcome row rather than
being overwritten by the generic signal-exit close.

---

## 4. Verification of recent fixes

| Fix | Verdict | Notes |
|-----|---------|-------|
| **M0** sub-penny stop quantization (9064ff9) | **PASS** | `stops.quantize_stop_price` correct (2dp ≥$1, 4dp <$1, None/≤0 sentinels); applied at broker boundary in `submit_atr_stop` AND in `auto_trader._maybe_attach_stop` so on-book/DB/log agree. Tests green. |
| **M1** MFE/MAE + intraday-outcome capture (6750198) | **FAIL (in production)** | Code/units correct in isolation, but: prod still 100% NULL MFE/MAE (F3); intraday outcomes never opened so the flatten capture is unreachable (F2); stop/trailing exits not covered (F5); window TZ mismatch (F4). The instrumentation exists but is not wired into a live caller that feeds it bars or opens intraday outcomes. |
| **M2** quarantine 3 negative-edge strategies (743923e) | **PASS** | `paused_strategies` shows intraday-1m-orb, intraday-1m-vwap-reclaim, botnet101-consec-bearish all paused with `expires_at=NULL` (indefinite), source `sprint1_quarantine`. Script idempotent (UPSERT). Entry path yields SKIP_PAUSED_STRATEGY (tested). |
| **M3** qty<1 veto fix (b787627) | **PASS (logic)** | `_process_entry` now sizes against the real cap (`_calc_qty(close, max_pos_usd)`) and buys 1 share when the shrunken notional can't, only skipping when even the full cap can't afford a share; `sizing['qty_floored_to_cap_min']` flag set. Effect not yet provable on live data (F8) — landed end-of-day, no post-fix RTH session. |
| **M4** size-to-edge (239367b) | **PASS** | `kelly.profit_factor` correct (None when no losses); `_coerce_kelly_settings` reads `pf_size_up`, clamps boost to `[base, 0.20]`; `kelly_quarter_notional` applies the boosted fraction only when measured PF > threshold and still mins against `max_position_usd`; intraday floor (`intraday.min_position_usd=800`) resolved before `compute_notional` for non-1d only; `merge_config` extended to carry the `intraday` block. Tests green. |
| **EOD** cancel-resting-orders flatten (0a15b18) | **PASS** | `_cancel_open_orders_for_symbols` sweeps OPEN orders for flattened symbols, cancels best-effort (per-order try/except), settles `settle_seconds` (2.0), then sells; never aborts the flatten; injectable for tests. Correct fix for the wash-trade / insufficient-qty rejections. |

---

## 5. Caveats / what I couldn't verify

- **Live-until-proven:** M3 and M4 effects (fewer `price_too_high` skips, larger PF-boosted fills,
  intraday floor letting liquid names fill) cannot be confirmed until the next RTH session — the
  fixes landed 2026-06-02 ~21:00 UTC, after market close (F8). The Jun-1→Jun-2 `price_too_high`
  drop (6,238→84) is suggestive but pre-/post-fix overlap makes it inconclusive.
- **New strategies have zero signals yet** (promoted 2026-06-03, market not open at audit time), so
  F1 is proven via the compute/load mechanics, not yet via a missed live signal.
- **F2/F3/F4/F5 are confirmed gaps**, but the *downstream* impact (mis-stated stop effectiveness)
  can only be quantified once outcomes carry excursion data — currently unmeasurable by design,
  which is itself the finding.
- I did not touch Alpaca (no live or read-only broker calls were needed); all evidence is from
  source, git, and the local SQLite DB.
- `no_open_position` skip volume (F7, 187,814 rows) is a logging-noise/optimization item, not a
  trading bug; flagged for DB hygiene.

---

*Report path: `docs/SYSTEM_AUDIT_2026-06-03.md`*
