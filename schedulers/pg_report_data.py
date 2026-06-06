#!/usr/bin/env python3
"""Dump today's Profit Generation system data as a plain-text block for
injection into the Hermes cron report agents (brief + analysis). Runs under
WSL python3 against the live Windows DB via /mnt/d. Stdlib only (sqlite3)."""
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

# Default to the WSL 9p mount (the cron runs under WSL python3). Fall back to a
# Windows path / env override so the script is testable from either OS without
# editing it. PG_TRADING_DB overrides both.
_WSL_DB = "/mnt/d/AI-Workstation/Antigravity/apps/Profit Generation/data/trading.db"
_WIN_DB = r"D:\AI-Workstation\Antigravity\apps\Profit Generation\data\trading.db"
_WSL_LOG = "/mnt/d/AI-Workstation/Antigravity/apps/Profit Generation/logs"
_WIN_LOG = r"D:\AI-Workstation\Antigravity\apps\Profit Generation\logs"
DB = os.environ.get("PG_TRADING_DB") or (_WSL_DB if os.path.exists(_WSL_DB) else _WIN_DB)
LOGDIR = _WSL_LOG if os.path.exists(_WSL_LOG) else _WIN_LOG
TODAY = date.today().isoformat()

out = []
def p(s=""): out.append(s)

def _open_db():
    # Direct read-only first; over the WSL 9p mount a WAL DB being written by
    # Windows often throws "disk I/O error", so fall back to a local snapshot
    # copy (main + -wal + -shm) and read that.
    try:
        cc = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        cc.execute("SELECT 1 FROM equity_snapshots LIMIT 1")
        return cc
    except Exception:
        pass
    import shutil, tempfile, os
    tmp = tempfile.mkdtemp(prefix="pgrep_")
    base = os.path.join(tmp, "trading.db")
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(DB + ext):
            try:
                shutil.copy2(DB + ext, base + ext)
            except Exception:
                pass
    return sqlite3.connect(base)

try:
    c = _open_db()
    c.row_factory = sqlite3.Row
except Exception as e:
    print(f"SYSTEM DATA UNAVAILABLE: cannot open DB: {e}")
    sys.exit(0)

def q(sql, args=()):
    try:
        return c.execute(sql, args).fetchall()
    except Exception as e:
        p(f"  (query failed: {e})")
        return []

p(f"=== PROFIT GENERATION SYSTEM DATA for {TODAY} (auto-extracted) ===")

# Portfolio / equity
p("\n[PORTFOLIO]")
latest = q("SELECT recorded_at,portfolio_value,cash,buying_power FROM equity_snapshots ORDER BY recorded_at DESC LIMIT 1")
prior = q("SELECT recorded_at,portfolio_value FROM equity_snapshots WHERE substr(recorded_at,1,10) < ? ORDER BY recorded_at DESC LIMIT 1", (TODAY,))
today_snaps = q("SELECT MIN(portfolio_value) lo, MAX(portfolio_value) hi, COUNT(*) n FROM equity_snapshots WHERE substr(recorded_at,1,10)=?", (TODAY,))
if latest:
    r = latest[0]
    p(f"  latest snapshot: {r['recorded_at'][:19]} value=${r['portfolio_value']:.2f} cash=${(r['cash'] or 0):.2f} buying_power=${(r['buying_power'] or 0):.2f}")
    if r["portfolio_value"]:
        deployed = (r["portfolio_value"] - (r["cash"] or 0))
        p(f"  deployed: ${deployed:.2f} ({100*deployed/r['portfolio_value']:.1f}% of portfolio)")
if prior:
    p(f"  prior-day close: ${prior[0]['portfolio_value']:.2f} ({prior[0]['recorded_at'][:19]})")
    if latest and latest[0]["portfolio_value"]:
        d = latest[0]["portfolio_value"] - prior[0]["portfolio_value"]
        p(f"  change vs prior close: ${d:+.2f} ({100*d/prior[0]['portfolio_value']:+.2f}%)")
if today_snaps and today_snaps[0]["n"]:
    p(f"  today snapshots: {today_snaps[0]['n']} (range ${today_snaps[0]['lo']:.2f}-${today_snaps[0]['hi']:.2f})")
    p("  EQUITY SNAPSHOT: present (today's P/L is trustworthy).")
else:
    # M8: flag loudly — a missing snapshot silently breaks every intraday-P/L
    # and deployed-% number in this report; it must not read as "flat day".
    p("  *** ALERT: NO EQUITY SNAPSHOT RECORDED TODAY ***")
    p("  intraday P/L, deployed%, and change-vs-prior are UNAVAILABLE / stale; "
      "do NOT interpret missing movement as a flat day. Check the snapshot job.")

# Signals fired today
p("\n[SIGNALS FIRED TODAY] (by strategy / interval)")
sig = q("SELECT strategy_id, bar_interval, signal_type, COUNT(*) n FROM signals WHERE substr(bar_ts,1,10)=? GROUP BY strategy_id,bar_interval,signal_type ORDER BY n DESC", (TODAY,))
if sig:
    for r in sig:
        p(f"  {r['strategy_id']} [{r['bar_interval']}] {r['signal_type']}: {r['n']}")
else:
    p("  none fired today.")

# Orders today
p("\n[PAPER ORDERS TODAY]")
od = q("SELECT side, status, COUNT(*) n FROM paper_trades WHERE substr(submitted_at,1,10)=? GROUP BY side,status ORDER BY n DESC", (TODAY,))
if od:
    for r in od:
        p(f"  {r['side']} / {r['status']}: {r['n']}")
else:
    p("  no orders submitted today.")

# Intraday entries today by strategy
p("\n[INTRADAY ENTRIES TODAY] (1m/5m/15m, filled buys)")
intr = q("""SELECT pt.strategy_id, s.bar_interval, pt.symbol, pt.qty, pt.fill_price
            FROM paper_trades pt JOIN signals s ON s.id=pt.signal_id
            WHERE substr(pt.submitted_at,1,10)=? AND pt.side='buy' AND pt.status='filled'
              AND s.bar_interval IN ('1m','5m','15m','1d-intraday')
            ORDER BY pt.strategy_id LIMIT 40""", (TODAY,))
if intr:
    for r in intr:
        p(f"  {r['strategy_id']} [{r['bar_interval']}] {r['symbol']} {r['qty']}@{r['fill_price']}")
else:
    p("  no intraday entries filled today.")

# Risk mechanics
p("\n[RISK MECHANICS]")
es = q("SELECT COUNT(*) n FROM paper_trades WHERE substr(submitted_at,1,10)=? AND entry_stops IS NOT NULL AND entry_stops!=''", (TODAY,))
p(f"  ATR initial stops attached today: {es[0]['n'] if es else 0}")
ts = q("SELECT COUNT(*) n FROM trailing_stops")
ts_ex = q("SELECT symbol,method,stop_price,extreme_price,strategy_id FROM trailing_stops ORDER BY updated_at DESC LIMIT 12")
p(f"  trailing stops armed (total): {ts[0]['n'] if ts else 0}")
for r in ts_ex:
    p(f"    {r['symbol']} {r['method']} stop={r['stop_price']:.2f} extreme={r['extreme_price']:.2f} ({r['strategy_id']})")
pyr = q("SELECT COUNT(*) n FROM paper_trades WHERE substr(submitted_at,1,10)=? AND pyramid_tier IS NOT NULL AND pyramid_tier>0", (TODAY,))
pyrsk = q("SELECT COUNT(*) n FROM intraday_skips WHERE substr(recorded_at,1,10)=? AND gate LIKE '%pyramid%'", (TODAY,))
p(f"  pyramid adds today: {pyr[0]['n'] if pyr else 0}; pyramid skips today: {pyrsk[0]['n'] if pyrsk else 0}")

# Outcomes closed today
p("\n[OUTCOMES CLOSED TODAY]")
oc = q("SELECT exit_reason, COUNT(*) n, ROUND(AVG(return_pct),3) avg_ret FROM outcomes WHERE substr(updated_at,1,10)=? AND status='closed' GROUP BY exit_reason ORDER BY n DESC", (TODAY,))
if oc:
    for r in oc:
        p(f"  {r['exit_reason']}: {r['n']} closed, avg return {r['avg_ret']}% (return_pct is already a percent)")
else:
    p("  none closed today.")
win = q("""SELECT s.symbol, s.strategy_id, o.return_pct FROM outcomes o JOIN signals s ON s.id=o.signal_id
           WHERE substr(o.updated_at,1,10)=? AND o.status='closed' ORDER BY o.return_pct DESC LIMIT 5""", (TODAY,))
los = q("""SELECT s.symbol, s.strategy_id, o.return_pct FROM outcomes o JOIN signals s ON s.id=o.signal_id
           WHERE substr(o.updated_at,1,10)=? AND o.status='closed' ORDER BY o.return_pct ASC LIMIT 5""", (TODAY,))
if win:
    p("  top winners: " + "; ".join(f"{r['symbol']} {r['return_pct']:+.2f}% ({r['strategy_id']})" for r in win))
if los:
    p("  top losers: " + "; ".join(f"{r['symbol']} {r['return_pct']:+.2f}% ({r['strategy_id']})" for r in los))
bystrat = q("""SELECT s.strategy_id, COUNT(*) n, ROUND(AVG(o.return_pct),3) avg FROM outcomes o JOIN signals s ON s.id=o.signal_id
               WHERE substr(o.updated_at,1,10)=? AND o.status='closed' GROUP BY s.strategy_id ORDER BY n DESC""", (TODAY,))
for r in bystrat:
    p(f"  closed by {r['strategy_id']}: {r['n']} at {r['avg']}% avg")

# Open positions
p("\n[OPEN POSITIONS]")
op = q("SELECT COUNT(*) n FROM outcomes WHERE status='open'")
opby = q("""SELECT s.strategy_id, COUNT(*) n FROM outcomes o JOIN signals s ON s.id=o.signal_id
            WHERE o.status='open' GROUP BY s.strategy_id ORDER BY n DESC LIMIT 8""")
p(f"  open outcomes total: {op[0]['n'] if op else 0}")
for r in opby:
    p(f"    {r['strategy_id']}: {r['n']} open")

# Recent log errors (for the analysis job)
p("\n[RECENT LOG ERRORS] (today, schtask_run_*.log)")
import glob, os
errs = []
for lf in glob.glob(f"{LOGDIR}/schtask_run_*.log"):
    try:
        with open(lf, errors="ignore") as fh:
            for line in fh.readlines()[-400:]:
                if ("ERROR" in line or "Traceback" in line) and TODAY in line:
                    errs.append(f"  {os.path.basename(lf)}: {line.strip()[:200]}")
    except Exception:
        pass
if errs:
    seen = set()
    for e in errs[:25]:
        if e not in seen:
            p(e); seen.add(e)
else:
    p("  no ERROR lines logged today (or logs not present).")

# Strategy stats over recent closed outcomes (for analysis)
p("\n[STRATEGY STATS - last 200 closed outcomes]")
st = q("""SELECT s.strategy_id, COUNT(*) n,
                 ROUND(AVG(o.return_pct),3) avg_ret,
                 ROUND(100.0*SUM(CASE WHEN o.return_pct>0 THEN 1 ELSE 0 END)/COUNT(*),1) win_rate
          FROM (SELECT * FROM outcomes WHERE status='closed' ORDER BY updated_at DESC LIMIT 200) o
          JOIN signals s ON s.id=o.signal_id GROUP BY s.strategy_id ORDER BY n DESC""")
for r in st:
    p(f"  {r['strategy_id']}: n={r['n']} win_rate={r['win_rate']}% avg_ret={r['avg_ret']}%")

# Skip gate distribution (last 2 days)
p("\n[INTRADAY SKIP GATES - last 2 days]")
since = (date.today() - timedelta(days=2)).isoformat()
sk = q("SELECT gate, COUNT(*) n FROM intraday_skips WHERE substr(recorded_at,1,10)>=? GROUP BY gate ORDER BY n DESC LIMIT 12", (since,))
for r in sk:
    p(f"  {r['gate']}: {r['n']}")

# M8: fresh trading activity vs reconciliation/cleanup. Outcomes closed by the
# reconcile/orphan/stale sweeps are bookkeeping, NOT trading — counting them as
# "trades closed today" overstates activity. Split them out explicitly.
p("\n[FRESH ACTIVITY vs RECONCILIATION] (outcomes closed today)")
RECONCILE_REASONS = (
    "reconciled_no_position", "stale_intraday_flatten_missed",
    "broker_reconcile", "orphan_sweep", "reconcile_close",
)
# M8/M9: key "closed today" on exit_ts (the trade's actual close/session date),
# NOT updated_at (the UTC wall-clock when the row was WRITTEN). A close written
# after 00:00 UTC — i.e. any close after ~17:00 PT, which is every EOD reconcile —
# has an updated_at that rolls to the NEXT calendar day, so an updated_at match
# silently dropped today's fresh-vs-cleanup split to zero. COALESCE keeps a null
# exit_ts falling back to updated_at.
split = q("""SELECT exit_reason, COUNT(*) n FROM outcomes
             WHERE substr(COALESCE(exit_ts, updated_at),1,10)=? AND status='closed'
             GROUP BY exit_reason""", (TODAY,))
fresh_n = recon_n = 0
recon_detail = []
for r in split:
    reason = r["exit_reason"] or ""
    if reason in RECONCILE_REASONS:
        recon_n += r["n"]
        recon_detail.append(f"{reason}={r['n']}")
    else:
        fresh_n += r["n"]
p(f"  fresh trading closes: {fresh_n}")
p(f"  reconciliation/cleanup closes: {recon_n}"
  + (f" ({', '.join(recon_detail)})" if recon_detail else ""))
if recon_n and not fresh_n:
    p("  NOTE: today's closes are ALL reconciliation cleanup — not trading "
      "activity. Do not read as a trading day.")

# M8: per-strategy ownership/order health from the DB's view of state. Held qty
# = open filled buy qty not yet offset by a working sell; available = held minus
# qty reserved by working sells/stops; open_orders = working sells/stops.
p("\n[STRATEGY HEALTH] (per strategy: open orders / held / available / paused)")
WORKING = "('new','accepted','partially_filled','pending_new','held')"
held = q(f"""
    SELECT pt.strategy_id, pt.symbol,
           SUM(CASE WHEN pt.side='buy'  AND pt.status IN ('filled','partially_filled') THEN pt.qty ELSE 0 END) AS buy_qty,
           SUM(CASE WHEN pt.side='sell' AND pt.status='filled' THEN pt.qty ELSE 0 END) AS sold_qty,
           SUM(CASE WHEN pt.side='sell' AND pt.status IN {WORKING} THEN pt.qty ELSE 0 END) AS working_sell_qty,
           SUM(CASE WHEN pt.status IN {WORKING} THEN 1 ELSE 0 END) AS open_orders
      FROM paper_trades pt
     GROUP BY pt.strategy_id, pt.symbol
    HAVING buy_qty > sold_qty
""")
paused_rows = q("SELECT strategy_id, reason, expires_at, source FROM paused_strategies")
paused_map = {r["strategy_id"]: r for r in paused_rows}
# symbol -> set of strategies holding it (duplicate-symbol ownership detector)
sym_owners = {}
if held:
    for r in held:
        sym_owners.setdefault(r["symbol"], set()).add(r["strategy_id"])
    for r in held:
        sid = r["strategy_id"]
        heldq = (r["buy_qty"] or 0) - (r["sold_qty"] or 0)
        availq = heldq - (r["working_sell_qty"] or 0)
        pr = paused_map.get(sid)
        pstate = "PAUSED" if pr else "active"
        if pr and pr["expires_at"]:
            pstate += f"(until {pr['expires_at'][:10]})"
        dupe = " [SHARED SYMBOL]" if len(sym_owners.get(r["symbol"], set())) > 1 else ""
        p(f"  {sid} {r['symbol']}: held={heldq} available={availq} "
          f"open_orders={r['open_orders'] or 0} [{pstate}]{dupe}")
else:
    p("  no held positions in the DB view.")
# Duplicate-symbol ownership summary (the unintended-short root cause signature).
dupes = {s: sorted(owners) for s, owners in sym_owners.items() if len(owners) > 1}
if dupes:
    p("  DUPLICATE-SYMBOL OWNERSHIP (multiple strategies on one symbol):")
    for s, owners in sorted(dupes.items()):
        p(f"    {s}: {', '.join(owners)}")
else:
    p("  no symbol is owned by more than one strategy (good).")
if paused_map:
    p(f"  paused strategies ({len(paused_map)}): "
      + ", ".join(sorted(paused_map.keys())))

c.close()
print("\n".join(out))
