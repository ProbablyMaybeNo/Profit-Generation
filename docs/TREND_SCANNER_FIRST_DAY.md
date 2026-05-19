# Trend Scanner First-Day Playbook

The day-one procedure for flipping `auto_trade.trend_scanner_enabled=true`
for the first time. This is the moment the wide-universe trend scanner
goes from observe-only (code shipped, master flag off) to live-paper
(EOD scans ~600 symbols and submits paper orders on the top-ranked
fires, capped by `max_new_entries_per_day`).

Each procedure has  5 steps. **Trip the kill switch
(`py -3.13 -m monitoring.kill_switch engage "scanner first day"`) at
any sign of trouble.**

Prerequisites already in place (do not start without these):

- Phase 5.5 milestones 5.5.1 through 5.5.7.1 all checked in
  `docs/PHASE5_5_PLAN CURRENT.md`
- `scripts/smoke_trend_scanner.py` returns PASS under `py -3.13`
- `data/universes/sp500.csv`, `nasdaq100.csv`, `etfs.csv` exist and
  load cleanly via `monitoring.universe.load_trend_universe`
- `liquidity_snapshots` table has  500 rows updated within the
  last 7 days (run `scripts/bootstrap_liquidity.py` if not)
- Phase 3 paper trading is healthy: at least 2 weeks of clean EOD
  paper runs with no kill-switch trips
- Telegram alerter is RUNNING (the scanner posts a fires-count
  summary at EOD)

---

## 1. Pre-flip verification (the morning of)

1. Run `py -3.13 scripts/smoke_trend_scanner.py`  must end with `PASS
   ` every pipeline stage fired as expected.
2. Confirm `monitoring.kill_switch` is NOT engaged:
   `py -3.13 -m monitoring.kill_switch status`.
3. Check the liquidity-snapshot freshness:
   `py -3.13 -c "from data import db; conn = db.init_db(); print(conn.execute(\"SELECT COUNT(*), MAX(as_of_date) FROM liquidity_snapshots\").fetchone())"`
   Expect ~500+ rows, as_of_date within last 7 days.
4. Confirm Alpaca paper buying_power > $25,000 (5 entries  $1k +
   headroom for trailing stops + pyramiding).
5. Open http://localhost:8080/research and confirm the
   `scanner activity` card renders with "none today" (no fires yet
   because the flag is off).

## 2. Flip the master switch

1. Read the current value:
   `py -3.13 -c "from config.utils import load_settings; s = load_settings(); print(s['auto_trade'].get('trend_scanner_enabled', False))"`
    must be `False`.
2. Edit `config/settings.json` and set
   `auto_trade.trend_scanner_enabled` to `true`. Set
   `auto_trade.max_new_entries_per_day` to `3` for day 1 (tighter
   than the eventual default of 5  let the system warm up).
3. Confirm the dashboard control card still shows `ACTIVE` with no
   error banner. The scanner flag does not change the badge, only
   adds capacity to the EOD path.
4. Post a Notion entry titled "Scanner flip armed  day 1" tagged
   `Scanner-Smoke`. Include the current universe count and
   max_new_entries_per_day.
5. Do NOT alter any other auto_trade setting in the same change 
   one variable per day.

## 3. Watch the EOD scan kick off

The scanner runs inside `monitoring.daily_report.main()` (5.5.5.1) at
the scheduled 14:30 PT (16:30 ET) daily run. Day 1 is the first time
the wide path is exercised against live db state.

1. At 16:31 ET, tail `logs/schtask_run_daily.log`  the run should
   contain a `trend_scanner: N fires recorded for wide universe` log
   line where N is between 0 and ~30 on a normal day. Hundreds means
   something is wrong.
2. Confirm `auto_trader: status=OK actions=M` printed afterward.
   M should be larger than the regular narrow-EOD count by the
   number of scanner fires processed.
3. If `trend_scanner` exited with WARNING, search the same log file
   for the WARNING line  the wiring isolates scanner crashes from
   the daily report (5.5.5.1) but you still need to fix it.
4. Open http://localhost:8080/research  the `scanner activity` card
   shows today's fires with action labels (SUBMITTED / SKIP_CAPACITY
   / SKIP_INELIGIBLE / PENDING).
5. Open http://localhost:8080/  the `paper_trades_today` card shows
   any scanner-sourced fills with the `scanner` badge next to the
   strategy name.

## 4. Verify the capacity cap is binding

If fires > cap, the lowest-scoring rows must come back as
SKIP_CAPACITY. This is the design guardrail  if it's not firing,
the system is over-trading.

1. Count today's scanner fires: open the `scanner activity` card and
   read the `n fires` counter (top right).
2. If fires > 3 (the day-1 cap), the rows should split: top-3 by
   score = SUBMITTED, rest = SKIP_CAPACITY.
3. If fires > 3 AND no SKIP_CAPACITY rows appear,
   `max_new_entries_per_day` is not honoured  inspect
   auto_trader.py:_reorder_signals_by_rank and the cap branch.
4. If fires  3, you cannot exercise the cap on day 1  re-check at
   the next session and document.
5. Cross-check the Alpaca paper dashboard: the order_ids from the
   scanner-tagged paper trades must match what Alpaca shows.

## 5. Monitor the ranker's top picks

The signal_ranker (5.5.4.1) composites four multipliers:
regime alignment, volume confirmation, edge tier, liquidity tier.
Day 1 is the first time you see what it actually picks on live data.

1. From the `scanner activity` card, read the top-5 scores. Hover
   each score cell  the breakdown tooltip shows the four
   multipliers per row.
2. Expect scores in the 1.0  3.0 range. > 3.5 means multiple
   multipliers piled on  could be legit but flag for review.
3. The top-ranked symbol should be a high-liquidity ($500M+ dvol)
   name during an aligned regime  if it's a $50M illiquid micro-
   cap, the liquidity bands are mis-tuned.
4. If two scores tie, the symbol with alphabetically lower symbol
   wins (signal_ranker.rank_signals tie-break).
5. Document the top-5 in the Notion sign-off page (procedure 8) so
   day-2's picks can be diffed against it.

## 6. Trailing stops update on scanner positions

Scanner-sourced positions get the same trailing-stop treatment as
narrow EOD positions (5.5.4.2 routes through the existing pipeline).

1. The morning after the flip, open http://localhost:8080  the
   `open positions` card shows scanner symbols with a `stop` column
   and a `to stop` distance.
2. Confirm the trailing-stop method matches the strategy declaration
   (atr_trail / chandelier / pct). If a scanner position has no
   trailing stop, something de-routed.
3. Tail `logs/schtask_run_daily.log` for the next session: the
   `trailing_stops` block must include each scanner position by
   symbol with an updated stop_price.
4. If a scanner position is hit on the trailing stop, the exit
   appears in `paper_trades_today` with side=`sell` and a
   `trailing` order_type tag.
5. Reconcile: the position count in the dashboard must match Alpaca
   exactly  no orphaned scanner positions.

## 7. Abort criteria (any of these  trip the kill switch immediately)

1. Scanner fires > 50 in one session  the universe or compute_fn
   is mis-wired.
2. Any scanner-sourced fill price differs from the signal's `close`
   by > 1% (illiquid name slipped through liquidity filter).
3. The `scanner activity` card shows > 10 SKIP_INELIGIBLE rows for
   the same strategy  the strategy may have lost edge and is
   firing on bad regime.
4. `trend_scanner: N fires recorded` log line is missing for two
   consecutive EOD runs after the flag flip  the scanner is not
   actually running.
5. Any ERROR-level log line in `logs/schtask_run_daily.log` from
   `trend_scanner`, `signal_ranker`, or `auto_trader` referencing
   `wide_universe` or `source: trend_scanner`.

## 8. End-of-day sign-off

1. Confirm scanner-sourced paper trades reconcile with Alpaca:
   `py -3.13 -m monitoring.reconcile_positions` returns OK.
2. Compute day-1 scanner contribution:
   open the dashboard `scanner activity` card and count SUBMITTED.
   Cross-check against the same count from
   `paper_trades_today` filtered by `is_scanner=true`.
3. Compute (or set aside for day-2) expected day-1 scanner P&L 
   closed scanner outcomes won't exist until trades exit.
4. Post a Notion sign-off page tagged `Scanner-Smoke-Day1` with the
   top-5 ranked scores, the SUBMITTED + SKIP_CAPACITY tally, any
   abort triggers, and the new universe size.
5. If sign-off is **GO**, leave `trend_scanner_enabled=true` for
   day 2 but keep `max_new_entries_per_day` at 3 for the first
   week. If **NO-GO**, follow 9. Rollback.

## 9. Rollback (flip scanner off)

If at any point the scanner must come off:

1. Trip the kill switch:
   `py -3.13 -m monitoring.kill_switch engage "scanner rollback"`.
2. Edit `config/settings.json` and set
   `auto_trade.trend_scanner_enabled` to `false`. Save.
3. Open scanner-sourced positions stay open under the narrow EOD
   trailing-stop path  do NOT auto-close them on rollback unless
   the rollback reason is "scanner picked a bad symbol":
   `py -3.13 -m monitoring.auto_trader --dry-run` first to see the
   exit decisions.
4. Re-run `scripts/smoke_trend_scanner.py`  must still return PASS
   to prove the wiring still works (the rollback was a settings
   flip, not a code break).
5. Release kill switch only after a Phase 5.5 review resolves the
   root cause that triggered the rollback.

## 10. Cross-references

- `docs/PHASE5_5_PLAN CURRENT.md` 5.5.7.1  smoke test script that
  proves the wiring offline before the flip
- `docs/RUNBOOK.md`  full disaster-recovery playbook
- `scripts/smoke_trend_scanner.py`  the dry-run end-to-end the day
  before the flip
- `monitoring.kill_switch`  engage at any abort criterion in  7
- `scripts/bootstrap_liquidity.py`  populates liquidity_snapshots
  (run if 1 fails)
- `monitoring.daily_report.maybe_run_trend_scanner`  the wired-up
  entry point that respects `trend_scanner_enabled`
