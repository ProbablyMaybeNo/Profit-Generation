"""
backfill_outcomes.py — Replay each active strategy's compute_fn over 2 years
of historical daily bars and persist every long_entry / long_exit signal,
then reconcile into the outcomes table.

After running:
  - signals table fills with hundreds of historical fires (bar_interval='1d',
    extra={"source":"backfill"} so they're distinguishable from live signals)
  - outcomes table shows realised return_pct + bars_held per closed trade
  - Today's existing live signals are preserved (UNIQUE constraint dedupes)
  - The currently-open outcome on today's KRE entry is preserved (the
    time-aware open_for_entry check in outcome_tracker handles this)

Run from the project root:
  conda run -n trading python scripts/backfill_outcomes.py
  (yfinance is required so use the trading conda env, not py -3.13)
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import load_bars  # noqa: E402
from data import db  # noqa: E402
from monitoring import outcome_tracker  # noqa: E402
from monitoring.intraday_monitor import _resolve_compute_fn  # noqa: E402

DEFAULT_LOOKBACK_DAYS = 730


def _persist_signals_for_pair(conn, sid, sym, signals_df) -> tuple:
    entries = exits = 0
    for ts, row in signals_df.iterrows():
        if hasattr(ts, "date"):
            bar_ts = ts.date().isoformat()
        else:
            bar_ts = str(ts)[:10]
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if bool(row.get("long_entry", False)):
            sid_in = db.record_signal(
                conn, strategy_id=sid, symbol=sym,
                bar_ts=bar_ts, signal_type="long_entry",
                close=close, bar_interval="1d",
                extra={"source": "backfill"},
            )
            if sid_in is not None:
                entries += 1
        if bool(row.get("long_exit", False)):
            sid_in = db.record_signal(
                conn, strategy_id=sid, symbol=sym,
                bar_ts=bar_ts, signal_type="long_exit",
                close=close, bar_interval="1d",
                extra={"source": "backfill"},
            )
            if sid_in is not None:
                exits += 1
    return entries, exits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute signals but don't persist or reconcile")
    args = parser.parse_args()

    conn = db.init_db()
    rows = conn.execute(
        "SELECT strategy_id, compute_fn, active_on_json FROM strategies "
        "WHERE compute_fn IS NOT NULL AND active_on_json IS NOT NULL "
        "ORDER BY strategy_id"
    ).fetchall()
    if not rows:
        print("No active strategies in DB. Run scripts/seed_strategies.py first.")
        return 1

    end = date.today()
    start = end - timedelta(days=args.lookback_days)
    print(f"Backfilling {len(rows)} active strategies over {start} -> {end} "
          f"({args.lookback_days}d).")

    universe: set = set()
    for r in rows:
        try:
            universe.update(json.loads(r["active_on_json"]) or [])
        except Exception:
            pass
    print(f"Pre-fetching daily bars for {len(universe)} unique symbols...")
    bars_by_sym = load_bars(
        sorted(universe), start=start.isoformat(), end=end.isoformat(),
        interval="1d", source="yf",
    )
    fetched = sorted(bars_by_sym.keys())
    missing = sorted(universe - set(fetched))
    print(f"  fetched: {len(fetched)}  missing: {len(missing)}"
          f"{(' (' + ', '.join(missing) + ')') if missing else ''}")

    total_entries = total_exits = 0
    pairs_processed = pairs_skipped = 0

    for r in rows:
        sid = r["strategy_id"]
        fn = _resolve_compute_fn(r["compute_fn"])
        if fn is None:
            print(f"  SKIP {sid}: compute_fn '{r['compute_fn']}' unresolvable")
            continue
        try:
            active = json.loads(r["active_on_json"]) or []
        except Exception:
            active = []
        for sym in active:
            bars = bars_by_sym.get(sym)
            if bars is None or bars.empty:
                pairs_skipped += 1
                print(f"  SKIP {sid} on {sym}: no bars")
                continue
            try:
                signals_df = fn(bars)
            except Exception as e:
                pairs_skipped += 1
                print(f"  SKIP {sid} on {sym}: compute failed — {e}")
                continue
            if args.dry_run:
                e_n = int(signals_df.get("long_entry", 0).sum() if "long_entry" in signals_df.columns else 0)
                x_n = int(signals_df.get("long_exit", 0).sum() if "long_exit" in signals_df.columns else 0)
                print(f"  DRY {sid} on {sym}: would persist entries={e_n} exits={x_n}")
                pairs_processed += 1
                continue
            ent, exi = _persist_signals_for_pair(conn, sid, sym, signals_df)
            total_entries += ent
            total_exits += exi
            pairs_processed += 1
            print(f"  {sid} on {sym}: new entries={ent} exits={exi}")

    print()
    print(f"Backfill done. Pairs processed={pairs_processed} skipped={pairs_skipped} "
          f"new_entries={total_entries} new_exits={total_exits}")

    if args.dry_run:
        return 0

    print("\nReconciling outcomes (time-aware)...")
    counts = outcome_tracker.reconcile_signals(conn)
    print(f"  reconcile: opened={counts['opened']} closed={counts['closed']} noop={counts['noop']}")

    # Cleanup: pre-existing live outcomes opened BEFORE backfill knew about
    # earlier historical entries can leave more than one open outcome on a
    # (strategy, symbol) pair. Strategies hold one position at a time, so keep
    # only the oldest open outcome per pair and drop the rest.
    dups = conn.execute(
        "SELECT s.strategy_id, s.symbol, COUNT(*) AS n "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'open' "
        " GROUP BY s.strategy_id, s.symbol HAVING COUNT(*) > 1"
    ).fetchall()
    deleted_orphans = 0
    for d in dups:
        opens = conn.execute(
            "SELECT o.signal_id "
            "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
            " WHERE o.status = 'open' AND s.strategy_id = ? AND s.symbol = ? "
            " ORDER BY s.bar_ts ASC, s.id ASC",
            (d["strategy_id"], d["symbol"]),
        ).fetchall()
        for o in opens[1:]:
            conn.execute("DELETE FROM outcomes WHERE signal_id = ?", (o["signal_id"],))
            deleted_orphans += 1
    if deleted_orphans:
        conn.commit()
        print(f"  cleanup: dropped {deleted_orphans} orphan open outcomes "
              f"({len(dups)} (strategy, symbol) pairs had multiple opens)")

    n_total   = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    n_open    = conn.execute("SELECT COUNT(*) FROM outcomes WHERE status='open'").fetchone()[0]
    n_closed  = conn.execute("SELECT COUNT(*) FROM outcomes WHERE status='closed'").fetchone()[0]
    print(f"\noutcomes: total={n_total}  open={n_open}  closed={n_closed}")

    print("\nClosed return_pct distribution:")
    stats = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       AVG(return_pct) AS mean, "
        "       MIN(return_pct) AS lo, "
        "       MAX(return_pct) AS hi, "
        "       SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS win_rate "
        "  FROM outcomes WHERE status='closed' AND return_pct IS NOT NULL"
    ).fetchone()
    if stats and stats["n"]:
        print(f"  n={stats['n']}  mean={stats['mean']:+.2f}%  "
              f"min={stats['lo']:+.2f}%  max={stats['hi']:+.2f}%  "
              f"win_rate={stats['win_rate']:.2%}")

    print("\nPer-strategy closed trades:")
    rows = conn.execute(
        "SELECT s.strategy_id, COUNT(*) AS n, "
        "       AVG(o.return_pct) AS mean_ret, "
        "       SUM(CASE WHEN o.return_pct > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS wr "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' "
        " GROUP BY s.strategy_id ORDER BY s.strategy_id"
    ).fetchall()
    for r in rows:
        print(f"  {r['strategy_id']:<40} n={r['n']:<4} "
              f"mean={r['mean_ret']:+.2f}%  WR={r['wr']:.1%}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
