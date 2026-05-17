# Disaster-Recovery Runbook

When things break. Each procedure ≤ 5 steps, ordered fastest → most invasive.
**Trip the kill switch first if money is at risk.** Everything else can wait.

Kill switch one-liner (engage):

```powershell
py -3.13 -m monitoring.kill_switch engage "<short reason>"
```

Or via Telegram: `/halt <reason>` (requires `\TradingSystem\TelegramListener` schtask running).

---

## 1. Machine reboot (planned or after crash)

After a Windows restart, none of the schtasks restart automatically and the
dashboard / TV tunnel do not run as services. Bring them back in this order:

1. `py -3.13 -m monitoring.kill_switch` — confirm switch is **OFF**; if it was
   tripped by `3.2.2` drawdown throttle pre-crash, release with `release`.
2. `schedulers\start_tv_tunnel.bat` — restart the Cloudflare quick-tunnel so
   `data\tunnel_url.txt` refreshes. The dashboard / TV webhook depend on it.
3. `schedulers\start_dashboard.bat` — restart Flask on `127.0.0.1:8080`.
4. `py -3.13 scripts/preflight.py` — must return all-PASS. If any check FAILs,
   follow the matching procedure below before trading.
5. `py -3.13 -m monitoring.reconcile_positions` — reconcile against Alpaca to
   confirm no drift accumulated during downtime.

If the machine was down across the market open, the intraday scan schtask
will catch up on its next 15-min tick — no action needed.

## 2. Alpaca outage (API errors, account_blocked, market_data unreachable)

Alpaca outages do not auto-trip the kill switch. The auto_trader logs the
error and skips that signal; outcome_tracker is read-only and is fine.

1. Trip the kill switch: `py -3.13 -m monitoring.kill_switch engage "alpaca outage"`.
2. Check status: https://status.alpaca.markets/ — note ETA.
3. Verify account state when Alpaca recovers:
   `py -3.13 scripts/preflight.py` (the Alpaca check must show ACTIVE + unblocked).
4. Run reconciliation: `py -3.13 -m monitoring.reconcile_positions` — Alpaca's
   position view may have lost intermediate state during outage.
5. Release kill switch: `py -3.13 -m monitoring.kill_switch release`.

## 3. Polygon outage (data feed errors mid-session)

Polygon backs the intraday scanner and the crypto / intraday strategy variants.
EOD strategies use the yfinance fallback path and are unaffected.

1. Confirm Polygon outage at https://status.polygon.io/ (not a credentials issue).
2. Trip the kill switch on intraday scope only — there is no per-scope flag,
   so engage globally: `py -3.13 -m monitoring.kill_switch engage "polygon outage"`.
3. EOD report (`monitoring.daily_report`) will still run via yfinance — let it.
4. When Polygon recovers, manually run `py -3.13 -m monitoring.intraday_monitor`
   once to confirm bars flow, then release the kill switch.
5. If outage spans the close, run `py -3.13 scripts/backfill_outcomes.py` so
   outcome rows aren't left in `OPEN` state past their exit bar.

## 4. Accidental kill-switch trip (drawdown throttle or fat-finger)

The drawdown throttle (3.2.2) trips at ≤ 85% of trailing 30-day peak.
False positives happen on data glitches (a bad equity snapshot).

1. Read the current state and reason: `py -3.13 -m monitoring.kill_switch`.
2. If reason is `drawdown_throttle`, query the equity_snapshots table:
   `py -3.13 -c "from data.db import connect; print(list(connect().execute('SELECT * FROM equity_snapshots ORDER BY captured_at DESC LIMIT 10')))"`.
3. If the most-recent snapshot is an outlier (e.g. NaN, $0, or 10× the prior),
   delete it: `DELETE FROM equity_snapshots WHERE id = <bad_id>`. The next
   auto_trader run recomputes drawdown from the surviving rows.
4. Release the switch: `py -3.13 -m monitoring.kill_switch release`.
5. Confirm release in dashboard banner and Telegram `/status` echo.

## 5. Corrupted trading.db (sqlite reports malformed / disk-image)

Possible causes: hard power loss mid-write, full disk, antivirus locked WAL.

1. **STOP every schtask that writes to trading.db immediately**:
   `schtasks /End /TN "\TradingSystem\Monitor"` (repeat for `Intraday`,
   `Daily`, `Reconcile`, `Crypto`, `TelegramListener`, `Backup`).
2. Trip the kill switch: `py -3.13 -m monitoring.kill_switch engage "db corruption"`.
3. Try the gentlest fix first — sqlite integrity check + dump/reload:
   `sqlite3 data\trading.db ".recover" | sqlite3 data\trading_recovered.db`,
   inspect, then `move data\trading.db data\trading.db.corrupt &&
   move data\trading_recovered.db data\trading.db`.
4. If recovery fails: restore from last good backup —
   `py -3.13 scripts/backup.py --list` then
   `py -3.13 scripts/backup.py --restore YYYY-MM-DD`.
5. Restart schtasks, reconcile against Alpaca to backfill the gap:
   `py -3.13 -m monitoring.reconcile_positions`, then release kill switch.

## 6. Cloudflare tunnel URL expired (TV webhook can't reach us)

Quick-tunnel URLs (`*.trycloudflare.com`) are ephemeral — they die with the
`cloudflared` process and the next one gets a new URL. TradingView alerts
hit the old URL until the alert's webhook is updated.

1. Check freshness: `py -3.13 -c "from pathlib import Path; from datetime import datetime; p=Path('data/tunnel_url.txt'); import os; print(p.read_text().strip(), 'age_h=', round((datetime.now().timestamp()-os.path.getmtime(p))/3600, 1))"`.
   The preflight script (`scripts/preflight.py`) FAILs if older than 24h.
2. Confirm `cloudflared` is still running: `tasklist | findstr cloudflared`.
   If absent, `schedulers\start_tv_tunnel.bat` to relaunch.
3. Read the new URL: `type data\tunnel_url.txt` — it's auto-written by the
   bat script when it scrapes the Cloudflare log.
4. Update every TradingView alert pointing at this webhook: replace the
   old URL with the new one in the alert's "Webhook URL" field. There is
   no API for this — it's a manual web-UI edit per alert.
5. Send a test alert from TradingView and watch `logs\tv_webhook.log` for
   the inbound request. If it lands, you're back.

## 7. Accidental `dry_run: true` re-flip mid-session

`config/settings.auto_trade.dry_run` was flipped to `false` on 2026-05-16
for the first real paper orders Monday 2026-05-18. If a stray edit or merge
re-flips it to `true`, every entry becomes a logged "would-be" order and
nothing reaches Alpaca — paper trades silently stop accumulating.

1. Detect: `py -3.13 -c "import json; print(json.load(open('config/settings.json'))['auto_trade']['dry_run'])"`.
   Should be `False`. If `True`, you have the bug.
2. Trip the kill switch defensively so no half-state orders fire while you
   edit: `py -3.13 -m monitoring.kill_switch engage "dry_run flip review"`.
3. Edit `config/settings.json`: set `"dry_run": false`. Validate JSON parses
   cleanly: `py -3.13 -c "import json; json.load(open('config/settings.json'))"`.
4. Check git blame for the regression:
   `git log -p --all -- config/settings.json | head -100` — surface the
   commit that re-flipped, file an issue, and decide whether to revert.
5. Release the kill switch and re-run preflight:
   `py -3.13 scripts/preflight.py` must be all-PASS before live signals fire.

---

## Operational reference

| Subsystem | Schtask name | Window | Owner script |
|---|---|---|---|
| Heartbeat | `\TradingSystem\Monitor` | every 15 min | `schedulers\run_monitor.bat` |
| Intraday scanner | `\TradingSystem\Intraday` | every 15 min, market hours | `schedulers\run_intraday.bat` |
| Daily report | `\TradingSystem\Daily` | 17:15 ET weekdays | `schedulers\run_daily.bat` |
| Position reconcile | `\TradingSystem\Reconcile` | 21:00 ET nightly | `schedulers\run_reconcile.bat` |
| Backup | `\TradingSystem\Backup` | 22:30 nightly | `schedulers\run_backup.bat` |
| Crypto scan | `\TradingSystem\Crypto` | every 15 min, 24/7 | `schedulers\run_crypto.bat` |
| Weekly digest | `\TradingSystem\Weekly` | Sunday 18:00 ET | `schedulers\run_weekly.bat` |
| Telegram listener | `\TradingSystem\TelegramListener` | onstart | `schedulers\run_telegram_listener.bat` |

The dashboard (`dashboard\server.py`) and the Cloudflare tunnel
(`schedulers\start_tv_tunnel.bat`) run as foreground processes — not as
schtasks. They die on reboot and need manual restart (Procedure 1).

`/api/health` at `http://127.0.0.1:8080/api/health` returns the
UptimeRobot-style JSON rollup of every subsystem age + status.
Hit it any time to skip running preflight from scratch.
