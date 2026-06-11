"""
quarantine_phantom_outcomes.py — one-time hygiene for signal-scoped phantom
outcomes (docs/TICKET_PHANTOM_OUTCOMES.md, "Remaining work" #3).

A PHANTOM outcome is an `outcomes` row whose entry signal never produced a
filled buy in `paper_trades` — the strategy fired, but no order ever backed it
(paused/observe strategies, or sizing/eligibility skips). The orphan sweep
historically booked these at a last-known mark, fabricating wins/losses that
poison per-strategy expectancy, the eligibility gate, P2 validation, and the
intraday lifecycle verifier.

This tool finds those rows and re-stamps them as
`exit_reason='phantom_no_fill'`, `return_pct=NULL`, `exit_price=NULL` (via
db.mark_outcome_phantom) so they drop out of every stats/eligibility query and
out of the Stage 0/4 verifier. It NEVER touches an outcome whose signal has a
fill, and it never deletes a row.

Scope:
  --intraday-only (DEFAULT) — only intraday-interval signals (1m/5m/15m/…).
      Safe: intraday strategies are paused, so this changes no live gate, and
      it is exactly what unblocks the Stage 4 lifecycle gate.
  --all — also quarantine 1d-family phantoms. This DOES change the data the 1d
      eligibility gate sees for live strategies (e.g. Donchian); use only with
      a deliberate decision (see the ticket's "Decide the outcome model").

DRY-RUN by default — prints what it WOULD do. Pass --apply to write.

Usage:
  py -3.13 -m scripts.quarantine_phantom_outcomes                 # dry-run, intraday
  py -3.13 -m scripts.quarantine_phantom_outcomes --apply         # write, intraday
  py -3.13 -m scripts.quarantine_phantom_outcomes --all --apply   # write, all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402

# Intraday = entry signal's bar_interval ends in a minute/hour unit; mirrors
# scripts/verify_intraday_lifecycle._INTRADAY_SQL so the two stay in lockstep.
_INTRADAY_SQL = (
    "(s.bar_interval LIKE '%m' OR s.bar_interval LIKE '%h')"
    " AND s.bar_interval NOT IN ('1d','1d-intraday')"
)

_HAS_FILL_SQL = (
    "EXISTS(SELECT 1 FROM paper_trades pt WHERE pt.signal_id = o.signal_id "
    "       AND pt.side='buy' AND pt.status IN ('filled','partially_filled'))"
)


def find_phantoms(conn, *, intraday_only: bool) -> List[dict]:
    """Outcomes with no backing fill, not already tagged phantom_no_fill."""
    where = [f"NOT {_HAS_FILL_SQL}",
             f"o.exit_reason IS NOT '{db.PHANTOM_NO_FILL_REASON}'"]
    if intraday_only:
        where.append(_INTRADAY_SQL)
    sql = (
        "SELECT o.signal_id, o.status, o.exit_reason, o.return_pct, "
        "       s.strategy_id, s.symbol, s.bar_interval "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE " + " AND ".join(where)
    )
    return [dict(r) for r in conn.execute(sql).fetchall()]


def summarize(rows: List[dict]) -> Dict[str, int]:
    by_reason: Dict[str, int] = {}
    for r in rows:
        key = r.get("exit_reason") or "(open/none)"
        by_reason[key] = by_reason.get(key, 0) + 1
    return dict(sorted(by_reason.items(), key=lambda kv: -kv[1]))


def run(conn, *, intraday_only: bool, apply: bool) -> dict:
    rows = find_phantoms(conn, intraday_only=intraday_only)
    quarantined = 0
    if apply:
        for r in rows:
            if db.mark_outcome_phantom(conn, int(r["signal_id"])):
                quarantined += 1
    return {"found": len(rows), "quarantined": quarantined,
            "by_reason": summarize(rows), "applied": apply,
            "intraday_only": intraday_only}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default: dry-run, print only)")
    parser.add_argument("--all", dest="all_intervals", action="store_true",
                        help="also quarantine 1d-family phantoms (changes the "
                             "1d eligibility data — deliberate decision only)")
    args = parser.parse_args(argv)

    conn = db.init_db()
    try:
        res = run(conn, intraday_only=not args.all_intervals, apply=args.apply)
    finally:
        conn.close()

    scope = "ALL intervals" if args.all_intervals else "intraday only"
    mode = "APPLIED" if args.apply else "DRY-RUN (no changes written)"
    print(f"=== Phantom outcome quarantine — {scope} — {mode} ===")
    print(f"  phantom rows found : {res['found']}")
    for reason, n in res["by_reason"].items():
        print(f"    {reason:<32} {n}")
    if args.apply:
        print(f"  quarantined        : {res['quarantined']}")
    else:
        print("  (run again with --apply to quarantine these rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
