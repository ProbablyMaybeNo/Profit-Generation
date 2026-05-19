# Intraday First-Day Playbook

The day-one procedure for flipping `auto_trade.intraday_enabled=true` for
the first time. This is the moment paper intraday trading goes from
observe-only (signals committed, no orders) to live-paper (5.2.2
submits paper orders inside the trading day).

Each procedure  5 steps. **Trip the kill switch (`py -3.13 -m
monitoring.kill_switch engage "intraday first day"`) at any sign of
trouble.**

Prerequisites already in place (do not start without these):

- Phase 5 milestones 5.1 through 5.6 all checked complete in
  `docs/PHASE5_PLAN CURRENT.md`
- `scripts/smoke_intraday_lifecycle.py` returns a clean run (entry +
  exit + EOD sweep) under `py -3.13`
- `scripts/preflight.py` returns all-PASS
- A Monday open is scheduled  never go live on a Fri PM or
  holiday-bracketed day
- Telegram `/halt` listener schtask is RUNNING

---

## 1. Pre-flip verification (the morning of)

1. Run `py -3.13 scripts/preflight.py`  must return all-PASS.
2. Confirm `monitoring.kill_switch` is NOT engaged.
3. Confirm the every-15-min `run_intraday.bat` schtask history shows
   green runs for the prior 2 sessions  this is the existing
   observe-only pipeline. Check `logs/schtask_run_intraday.log`.
4. Confirm Alpaca paper buying_power > $50,000 (one full intraday
   cap + headroom).
5. Open the dashboard at http://localhost:8080 and stay on the
   **Monitor** tab for the open  the intraday-signals card and
   PDT counter must be visible.

## 2. Flip the master switch

1. Read the current value:
   `py -3.13 -c "from config.utils import load_settings; s = load_settings(); print(s['auto_trade']['intraday_enabled'])"`
    must be `False`.
2. Edit `config/settings.json` and set `auto_trade.intraday_enabled`
   to `true`. Leave `intraday_intervals` at `["15m"]`  do not add
   `5m` or `1h` on day 1.
3. Re-run `py -3.13 scripts/preflight.py`  must still return all-PASS.
4. Confirm the dashboard auto-trader control card now shows the
   `ACTIVE + INTRADAY` badge (5.6.3). If it still shows `ACTIVE`,
   refresh; if it persists, settings did not reload.
5. Post a Notion entry titled "Intraday flip armed  day 1" tagged
   `Intraday-Smoke`.

## 3. Watch the first 15-min cycle

The intraday schtask fires every 15 min starting at 09:30 ET. The
first cycle is the most informative  it tells you whether the
fire-check, the signal commit, and the auto-trader path all woke up.

1. At 09:45 ET, tail `logs/schtask_run_intraday.log`  the run should
   contain `intraday_fires exit 0` AND `auto_trader_intraday exit 0`.
2. If `auto_trader_intraday` printed `DISABLED_INTRADAY` the flip did
   not stick  re-check `intraday_enabled` in settings.
3. If any intraday strategy fired a long_entry signal in this cycle,
   the dashboard's intraday-signals card (5.6.1) shows it within 30s.
4. If a paper order submitted, the `paper_trades_today` card shows
   it with the `INTRADAY (15m)` tag (5.6.2).
5. Cross-check the Alpaca paper dashboard at
   https://app.alpaca.markets/  the order_id from the log must match
   the order_id Alpaca shows.

## 4. Verify sizing is correct

Intraday entries are sized at half the EOD-equivalent (5.5.1). The
first fill is the only easy moment to confirm this  later in the
day, regime / drawdown multipliers will obscure it.

1. From the first fill's row in `paper_trades`, compute notional:
   `qty * fill_price`.
2. Confirm it is roughly `max_position_usd / 2` (with any whole-share
   rounding accounted for).
3. If the notional looks closer to the full `max_position_usd`, the
   intraday multiplier is not applying  trip the kill switch and
   inspect `sizing` in the order's auto_trader action log.
4. If the notional is far below half, check the grace-period
   multiplier in settings (`grace_period_size_multiplier`, default
   0.25  intraday strategies start in grace until they accumulate
   `min_outcomes` closed outcomes).
5. Document the day-1 expected sizing in `data/intraday_smoke.json`
   for audit.

## 5. Monitor the PDT counter

Paper accounts ignore PDT, but the counter (5.4.1 / 5.4.2) must show
correct numbers so the guard works the day this strategy goes live.

1. The dashboard PDT card shows "Day trades today: N/3" and
   "5-day rolling: N/3" with a "paper unlimited" subtitle.
2. After each completed round trip (buy + sell on the same day on
   the same intraday signal), the day count must increment by 1.
3. If the counter does not increment within 30s of a round-trip
   close, the dashboard query is broken  fall back to
   `py -3.13 -m monitoring.pdt_guard --status`.
4. The 5-day rolling number must equal the day-1 number on day 1
   (since day 1 is the first day with any intraday trades).
5. Do not abort over a stuck PDT counter on paper  the guard math
   is independent of the dashboard display and will still block if
   the live threshold is hit.

## 6. Monitor the 16:00 close-out

5.5.3 sweeps any still-open intraday position at the close so the
position is flat overnight. Day 1 is the first day this fires for
real, so watch it.

1. At 15:55 ET, count the open intraday positions:
   `py -3.13 -c "from monitoring.close_intraday_positions import _open_intraday_buys; from data.db import connect; print(_open_intraday_buys(connect()))"`.
2. At 16:00 ET, `schedulers/run_daily.bat` fires  it invokes
   `monitoring.close_intraday_positions`. Tail
   `logs/schtask_run_daily.log` for `EOD_CLOSE_INTRADAY SELL N <sym>`
   lines (5.5.3).
3. At 16:01 ET, re-run the open-positions query from step 1  the
   list must be empty.
4. If any intraday position is still open, the close-out failed 
   trip the kill switch and follow `RUNBOOK.md` for manual close.
5. Reconcile: `py -3.13 -m monitoring.reconcile_positions`  the
   live position count and qty must match Alpaca exactly.

## 7. Abort criteria (any of these  trip the kill switch immediately)

1. The intraday schtask emitted exit code != 0 for two consecutive
   15-min cycles.
2. An intraday fill price differs from the signal's `close` by
   > 0.5% (slippage burn  same threshold as the live-equity smoke).
3. Any intraday position remains open past 16:05 ET (close-out
   failed).
4. The dashboard `ACTIVE + INTRADAY` badge disappears mid-session
   (settings reload broke).
5. Any ERROR-level log line in `logs/schtask_run_intraday.log` from
   `auto_trader_intraday` or `intraday_fires`.

## 8. End-of-day sign-off

1. Confirm zero open intraday positions in `paper_trades` (joined
   against signals where `bar_interval != '1d'`).
2. Run `py -3.13 -m monitoring.daily_report`  intraday trades
   appear in their own section, tagged INTRADAY.
3. Compute day-1 intraday P&L:
   `py -3.13 -c "from data.db import connect; rows = list(connect().execute(\"SELECT o.return_pct FROM outcomes o JOIN signals s ON s.id = o.signal_id WHERE s.bar_interval != '1d' AND o.status = 'closed'\")); print(sum(r[0] for r in rows))"`.
4. Post a Notion sign-off page tagged `Intraday-Smoke-Day1` with the
   P&L, intraday fill log, and any abort triggers.
5. If sign-off is **GO**, leave `intraday_enabled=true` for day 2.
   If **NO-GO**, follow 9. Rollback.

## 9. Rollback (flip intraday off)

If at any point intraday must come off:

1. Trip the kill switch:
   `py -3.13 -m monitoring.kill_switch engage "intraday rollback"`.
2. Edit `config/settings.json` and set
   `auto_trade.intraday_enabled` to `false`. Save.
3. Run `py -3.13 -m monitoring.close_intraday_positions`  any open
   intraday position is closed; idempotent if already flat.
4. Re-run `py -3.13 scripts/preflight.py`  must show
   `intraday_enabled=false`.
5. Release kill switch only after the next full Phase 5 review
   resolves the root cause that triggered the rollback.

## 10. Cross-references

- `docs/PHASE5_PLAN CURRENT.md` 5.7.1  smoke test script that
  proves the wiring offline
- `docs/LIVE_SMOKE_TEST.md`  the equivalent procedure for the
  first live-equity strategy (separate concern; do not run both
  smokes on the same day)
- `docs/RUNBOOK.md`  full disaster-recovery playbook (kill switch,
  outage handling, DB corruption)
- `scripts/preflight.py`  must pass before procedures 1, 2, and 8
- `monitoring.close_intraday_positions`  the EOD sweep at 16:00 ET
- `monitoring.kill_switch`  engage at any abort criterion in  7
- `scripts/smoke_intraday_lifecycle.py` (5.7.1)  the dry-run
  end-to-end the day before the flip
