"""
verify_intraday_lifecycle.py — Stage 0 instrument for the intraday
trend-following build (docs/INTRADAY_TREND_BUILD_PLAN.md).

Read-only. Never submits orders, never writes the DB.

It answers one question per trading session: did every intraday position
opened that session reach a CLEAN, fully-measured close the same session?

  CLEAN exit reasons  : eod_close, trailing_stop, long_exit_signal
  BAD   exit reasons  : stale_intraday_flatten_missed, reconciled_no_position
                        (band-aids — the position leaked or vanished)
  OTHER               : any reason not in the two sets above (flagged, not clean)

A session PASSES (gate green) when, for that session's intraday entries:
  - every outcome is closed (none left OPEN / carried overnight), AND
  - every exit_reason is CLEAN, AND
  - every closed outcome has non-null exit_price, mfe_pct, mae_pct.

Intraday = the entry signal's bar_interval ends in 'm' or 'h' (1m/5m/15m/1h…),
which excludes '1d' and the synthesized '1d-intraday' family.

CLI:
  py -3.13 -m scripts.verify_intraday_lifecycle                 # all-time + last 10 sessions
  py -3.13 -m scripts.verify_intraday_lifecycle --days 20       # last 20 sessions
  py -3.13 -m scripts.verify_intraday_lifecycle --session 2026-06-08   # gate ONE session (exit 0/1)
"""

from __future__ import annotations

import argparse
from datetime import date
import sqlite3
import sys
from typing import Dict, List, Optional

CLEAN_EXITS = {"eod_close", "trailing_stop", "long_exit_signal"}
BAD_EXITS = {"stale_intraday_flatten_missed", "reconciled_no_position"}

# bar_interval that counts as intraday: ends in a minute/hour unit.
_INTRADAY_SQL = (
    "(s.bar_interval LIKE '%m' OR s.bar_interval LIKE '%h')"
    " AND s.bar_interval NOT IN ('1d','1d-intraday')"
)


def _session_of(entry_ts: Optional[str]) -> Optional[str]:
    """Leading YYYY-MM-DD of an ISO entry_ts (naive ET), or None."""
    if not entry_ts:
        return None
    s = str(entry_ts)[:10]
    return s if len(s) == 10 and s[4] == "-" else None


def _fetch_intraday_outcomes(conn: sqlite3.Connection,
                             session: Optional[str] = None) -> List[dict]:
    """Intraday outcome rows joined to their entry signal. When `session` is
    given, restrict to entries whose entry_ts falls on that calendar date."""
    sql = (
        "SELECT o.signal_id, o.status, o.entry_ts, o.exit_ts, o.entry_price, "
        "       o.exit_price, o.exit_reason, o.return_pct, o.mfe_pct, o.mae_pct, "
        "       s.strategy_id, s.symbol, s.bar_interval "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        f" WHERE {_INTRADAY_SQL}"
    )
    params: tuple = ()
    if session:
        sql += " AND substr(o.entry_ts,1,10) = ?"
        params = (session,)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def classify(row: dict) -> str:
    """One of: open, clean, bad, other, unmeasured.

    `unmeasured` = a CLEAN closed exit that is missing exit_price/mfe/mae —
    it closed for the right reason but we can't trust its excursion stats.
    """
    if (row.get("status") or "").lower() != "closed":
        return "open"
    reason = row.get("exit_reason") or ""
    if reason in BAD_EXITS:
        return "bad"
    if reason not in CLEAN_EXITS:
        return "other"
    if row.get("exit_price") is None \
            or row.get("mfe_pct") is None or row.get("mae_pct") is None:
        return "unmeasured"
    return "clean"


def summarize(rows: List[dict]) -> Dict[str, int]:
    out = {"total": len(rows), "clean": 0, "bad": 0, "other": 0,
           "open": 0, "unmeasured": 0}
    for r in rows:
        out[classify(r)] += 1
    return out


def gate_session(conn: sqlite3.Connection, session: str) -> dict:
    """Pass/fail for ONE session. Green only when every intraday entry that
    session closed clean AND fully measured (no open carry, no band-aids)."""
    rows = _fetch_intraday_outcomes(conn, session=session)
    counts = summarize(rows)
    offenders = [
        {"strategy_id": r["strategy_id"], "symbol": r["symbol"],
         "bar_interval": r["bar_interval"], "entry_ts": r["entry_ts"],
         "exit_reason": r["exit_reason"], "verdict": classify(r)}
        for r in rows if classify(r) != "clean"
    ]
    passed = counts["total"] > 0 and not offenders
    return {"session": session, "passed": passed,
            "counts": counts, "offenders": offenders}


def per_session_breakdown(conn: sqlite3.Connection,
                          limit: int = 10) -> List[dict]:
    rows = _fetch_intraday_outcomes(conn)
    by_session: Dict[str, List[dict]] = {}
    for r in rows:
        d = _session_of(r.get("entry_ts"))
        if d:
            by_session.setdefault(d, []).append(r)
    out = []
    for d in sorted(by_session, reverse=True)[:limit]:
        c = summarize(by_session[d])
        c["session"] = d
        c["passed"] = c["total"] > 0 and c["clean"] == c["total"]
        out.append(c)
    return out


def resolve_session_arg(session: str) -> str:
    """Resolve CLI session shorthands to YYYY-MM-DD."""
    if str(session).strip().lower() == "today":
        return date.today().isoformat()
    return str(session)


def _open_conn() -> sqlite3.Connection:
    from data import db
    return db.init_db()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=str, default=None,
                        help="Gate ONE session (YYYY-MM-DD). Exit 0 if green.")
    parser.add_argument("--days", type=int, default=10,
                        help="How many recent sessions to tabulate.")
    args = parser.parse_args(argv)

    conn = _open_conn()
    try:
        if args.session:
            session = resolve_session_arg(args.session)
            res = gate_session(conn, session)
            c = res["counts"]
            print(f"=== Stage 0 gate - session {res['session']} ===")
            print(f"  intraday entries : {c['total']}")
            print(f"  clean            : {c['clean']}")
            print(f"  bad (leaked)     : {c['bad']}")
            print(f"  other reason     : {c['other']}")
            print(f"  unmeasured       : {c['unmeasured']}")
            print(f"  still open       : {c['open']}")
            for o in res["offenders"]:
                print(f"    XX {o['verdict']:<10} {o['strategy_id']}/{o['symbol']}"
                      f" [{o['bar_interval']}] entered {o['entry_ts']}"
                      f" reason={o['exit_reason']}")
            print(f"  GATE: {'GREEN' if res['passed'] else 'RED'}")
            return 0 if res["passed"] else 1

        all_rows = _fetch_intraday_outcomes(conn)
        c = summarize(all_rows)
        print("=== Intraday lifecycle - all-time baseline ===")
        clean_pct = (100.0 * c["clean"] / c["total"]) if c["total"] else 0.0
        print(f"  total {c['total']} | clean {c['clean']} ({clean_pct:.0f}%) | "
              f"bad {c['bad']} | other {c['other']} | "
              f"unmeasured {c['unmeasured']} | open {c['open']}")
        print(f"\n=== Last {args.days} sessions ===")
        rows = per_session_breakdown(conn, limit=args.days)
        if not rows:
            print("  (no intraday outcomes recorded yet)")
        for r in rows:
            flag = "GREEN" if r["passed"] else "RED"
            print(f"  {r['session']}  entries={r['total']:<3} clean={r['clean']:<3}"
                  f" bad={r['bad']:<3} other={r['other']:<2} open={r['open']:<2}"
                  f" unmeasured={r['unmeasured']:<2}  {flag}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
