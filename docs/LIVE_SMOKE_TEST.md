# Live-Equity Smoke-Test Playbook

The day-one procedure for flipping the very first strategy to **live**
Alpaca equity trading. This is the most operationally consequential moment
in the project — every check below must pass before the first share clears.

Each procedure ≤ 5 steps. **Trip the kill switch (`py -3.13 -m
monitoring.kill_switch engage "smoke test"`) at any sign of trouble.**

Prerequisites already in place (do not start without these):
- `4.1.1` live-promotion scorer flagged at least one `READY_FOR_LIVE`
  strategy with N ≥ 50 closed paper outcomes
- `4.1.2` wizard installed `alpaca_live` into `config/credentials.json`
- `scripts/preflight.py` returns all-PASS
- A Monday open is scheduled — never go live on a Fri PM or holiday-bracketed day
- Telegram `/halt` listener schtask is RUNNING

---

## 1. Pick the candidate strategy

ONE strategy. ONE ETF. ONE share. Anything else is over-scope for day 1.

1. Run `py -3.13 scripts/score_live_candidates.py` and pick the top
   `READY_FOR_LIVE` row.
2. Cross-check: the strategy must be mean-reversion (not trend-following —
   4.6 trend strategies need their own smoke test). Mean-reversion fires
   quickly, so a same-week exit is likely → faster signal.
3. Pick the lowest-priced ETF in the strategy's `active_on` list (GDX or
   KRE typically). Lower notional = smaller possible loss on the smoke.
4. Cap `auto_trade.max_position_usd` to **$100** in `config/settings.json`.
   The strategy's own sizing will floor at 1 share regardless.
5. Record the chosen strategy + symbol in `data/live_smoke.json` for audit.

## 2. Add to live_strategies (the actual flip)

1. Read the current value: `py -3.13 -c "from config.utils import
   load_settings; print(load_settings()['auto_trade']['live_strategies'])"`
   — confirm it is `[]`.
2. Edit `config/settings.json` and add the chosen strategy_id to
   `auto_trade.live_strategies`. ONE id. Save.
3. Re-run `py -3.13 scripts/preflight.py` — must still return all-PASS
   AND show the live strategy listed.
4. Confirm `monitoring.auto_trader._is_live_strategy(sid)` returns True
   for the chosen sid and False for every other tracked strategy.
5. Post a Notion entry titled "Live smoke armed — \<sid\> \<symbol\>"
   tagged `Live-Smoke`.

## 3. Pre-open verification (the morning of)

1. Run `py -3.13 scripts/preflight.py` — must return all-PASS with both
   `alpaca` AND `alpaca_live` checks green (Phase 3 only checked paper —
   add the live preflight before this milestone runs).
2. Confirm Alpaca live dashboard at https://app.alpaca.markets/ shows
   account ACTIVE + buying_power > $1000.
3. Confirm `monitoring.kill_switch` is NOT engaged.
4. Confirm Telegram listener heartbeat in `logs/heartbeat.log` is < 5 min
   old.
5. Open the dashboard at http://localhost:8080 and stay on the Live tab
   for the open.

## 4. Monitor the first fill in real-time

1. Watch the dashboard's Live-Positions card. The first signal should
   submit within 5 min of the entry bar's close (mean-reversion strategies
   typically fire near the open).
2. As soon as the order submits, **paste the Alpaca order_id into
   Telegram `/status`** — confirm both Alpaca and our DB agree on the
   fill price.
3. If the fill price differs from `signals.close` by **more than 0.5%**,
   trip the kill switch (`py -3.13 -m monitoring.kill_switch engage
   "smoke slippage > 0.5%"`) and investigate before another fill.
4. If the order is still UNFILLED 60 seconds after submission, cancel via
   Alpaca dashboard, trip the kill switch, and inspect Alpaca order
   rejections.
5. Log every event (fill, partial, cancel) into `data/live_smoke.json`
   with timestamps.

## 5. Monitor the first 5 fills

1. The fifth fill is the cutoff — do not extend the smoke window beyond
   five round trips on day 1 even if everything looks fine.
2. After each fill, run `py -3.13 -m monitoring.reconcile_positions` —
   the live position count and qty must match Alpaca exactly.
3. After each EXIT, verify the outcome row landed in `outcomes` with
   `status='closed'` and the return_pct is within 0.1% of (exit_price -
   entry_price) / entry_price.
4. If any reconcile mismatch appears, trip the kill switch and follow
   `RUNBOOK.md` § 5 (corrupted DB) → § 2 (Alpaca outage) procedures.
5. After fill #5 (round-trip complete), trip the kill switch deliberately
   to pause overnight: `py -3.13 -m monitoring.kill_switch engage "smoke
   day 1 complete"`.

## 6. Abort criteria (any of these → trip the kill switch immediately)

1. Fill price > 0.5% from the signal's `close` (slippage burn).
2. Two consecutive losing trades on the smoke strategy.
3. Any reconciliation mismatch between Alpaca positions and our
   `paper_trades` table.
4. Any Telegram alert from `strategy_health` flagging the live strategy
   as degraded.
5. Any unexpected ERROR-level log line in `logs/auto_trader.log`.

## 7. End-of-day sign-off

1. Confirm kill switch is engaged for overnight (it should be from
   step 5.5).
2. Run `py -3.13 -m monitoring.daily_report` — verify the live trades
   appear in the report under their own section.
3. Compute the smoke's day-1 P&L: `py -3.13 -c "from data.db import
   connect; rows = list(connect().execute('SELECT return_pct FROM
   outcomes WHERE signal_id IN (SELECT id FROM signals WHERE
   strategy_id=?)', ('<sid>',))); print(sum(r[0] for r in rows))"`.
4. Post a Notion sign-off page tagged `Live-Smoke-Day1` with the P&L,
   fill log, and any abort triggers.
5. If sign-off is **GO**, release the kill switch the next morning and
   continue the smoke for a second day. If **NO-GO**, remove the
   strategy from `auto_trade.live_strategies` and open a Phase 4 review.

## 8. Rollback to paper

If at any point during the smoke the strategy must come off live:

1. Trip the kill switch.
2. Edit `config/settings.json` and remove the strategy_id from
   `auto_trade.live_strategies`. Save.
3. Run `py -3.13 -m monitoring.reconcile_positions` — any open live
   position must be closed via the Alpaca dashboard manually before
   re-arming.
4. Re-run `py -3.13 scripts/preflight.py` — must show
   `live_strategies=[]`.
5. Release the kill switch.

## 9. Day-2+ ramp criteria

Only after day 1 closes clean:

1. Day 2 — keep the same single strategy + single ETF + max_position_usd=$100.
2. Day 3-5 — bump max_position_usd to $250 (still single strategy).
3. Day 6-10 — bump to $500.
4. Day 11+ — restore $1000 cap AND consider adding a second
   `READY_FOR_LIVE` strategy from `4.1.1`.
5. Promotion to a third live strategy requires a fresh `4.1.1` run + a
   week of continuous P&L above the paper baseline.

## 10. Cross-references

- `docs/RUNBOOK.md` — full disaster-recovery playbook (kill switch,
  outage handling, DB corruption)
- `scripts/preflight.py` — must pass before steps 2 and 3
- `monitoring.reconcile_positions` — must show zero drift after every
  fill in step 5
- `monitoring.kill_switch` — engage at any abort criterion in § 6
- `4.1.1 scripts/score_live_candidates.py` — picks the candidate in § 1
- `4.1.2 scripts/setup_live_credentials.py` — the prereq for the entire
  playbook
