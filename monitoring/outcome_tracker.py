"""
outcome_tracker.py — Convert long_entry/long_exit signals into outcomes.

For each (strategy_id, symbol):
  - On long_entry, open an outcome (idempotent — only one open at a time).
  - On long_exit, close the most recent open outcome with the exit close.

Walks signals chronologically by bar_ts then id, so same-bar entry+exit
resolve in the order they were recorded by daily_report.persist_report.

Idempotent: re-running reconcile_signals on the same DB is a no-op.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402


def _get_open_outcome_signal_id(
    conn: sqlite3.Connection,
    strategy_id: str,
    symbol: str,
    *,
    on_or_before_bar_ts: Optional[str] = None,
) -> Optional[int]:
    """
    Return the signal id of the most recent open outcome for (strategy, symbol).
    If on_or_before_bar_ts is given, only consider entries dated <= it — this
    lets backfill processing of earlier-dated signals ignore outcomes opened
    by later (already-processed) signals.
    """
    sql = (
        "SELECT s.id AS id "
        "  FROM signals s "
        "  JOIN outcomes o ON o.signal_id = s.id "
        " WHERE s.strategy_id = ? "
        "   AND s.symbol      = ? "
        "   AND s.signal_type = 'long_entry' "
        "   AND o.status      = 'open' "
    )
    params: list = [strategy_id, symbol]
    if on_or_before_bar_ts is not None:
        sql += "   AND s.bar_ts <= ? "
        params.append(on_or_before_bar_ts)
    sql += " ORDER BY s.bar_ts DESC, s.id DESC LIMIT 1"
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row["id"]) if row else None


def _bars_held_calendar(entry_bar_ts: str, exit_bar_ts: str) -> Optional[int]:
    try:
        e = date.fromisoformat(entry_bar_ts[:10])
        x = date.fromisoformat(exit_bar_ts[:10])
        return (x - e).days
    except Exception:
        return None


def open_for_entry(conn: sqlite3.Connection, signal_row: sqlite3.Row) -> bool:
    """Open an outcome for a long_entry signal. Returns True if a new outcome was opened."""
    if signal_row["close"] is None:
        return False
    existing = conn.execute(
        "SELECT 1 FROM outcomes WHERE signal_id = ?", (int(signal_row["id"]),)
    ).fetchone()
    if existing is not None:
        return False
    prior_open = _get_open_outcome_signal_id(
        conn, signal_row["strategy_id"], signal_row["symbol"],
        on_or_before_bar_ts=signal_row["bar_ts"],
    )
    if prior_open is not None:
        return False
    db.open_outcome(
        conn,
        signal_id=int(signal_row["id"]),
        entry_ts=signal_row["bar_ts"],
        entry_price=float(signal_row["close"]),
    )
    return True


def close_for_exit(conn: sqlite3.Connection, exit_signal_row: sqlite3.Row) -> bool:
    """Close the matching open outcome for a long_exit signal. Returns True if closed."""
    if exit_signal_row["close"] is None:
        return False
    open_sig_id = _get_open_outcome_signal_id(
        conn, exit_signal_row["strategy_id"], exit_signal_row["symbol"],
        on_or_before_bar_ts=exit_signal_row["bar_ts"],
    )
    if open_sig_id is None:
        return False
    entry = conn.execute(
        "SELECT bar_ts FROM signals WHERE id = ?", (open_sig_id,)
    ).fetchone()
    bars_held = _bars_held_calendar(entry["bar_ts"], exit_signal_row["bar_ts"]) if entry else None
    db.close_outcome(
        conn,
        signal_id=open_sig_id,
        exit_ts=exit_signal_row["bar_ts"],
        exit_price=float(exit_signal_row["close"]),
        exit_reason="long_exit_signal",
        bars_held=bars_held,
    )
    return True


def reconcile_signals(
    conn: sqlite3.Connection,
    *,
    since_iso: Optional[str] = None,
    bar_interval: str = "1d",
) -> Dict[str, int]:
    """
    Walk signals in (bar_ts, id) order; open or close outcomes as needed.
    Pass since_iso to scope by bar_ts; default is full history.
    """
    sql = (
        "SELECT * FROM signals "
        "WHERE signal_type IN ('long_entry','long_exit') "
        "  AND bar_interval = ?"
    )
    params = [bar_interval]
    if since_iso:
        sql += " AND bar_ts >= ?"
        params.append(since_iso)
    sql += " ORDER BY bar_ts ASC, id ASC"

    counts = {"opened": 0, "closed": 0, "noop": 0}
    for row in conn.execute(sql, tuple(params)).fetchall():
        if row["signal_type"] == "long_entry":
            counts["opened" if open_for_entry(conn, row) else "noop"] += 1
        else:
            counts["closed" if close_for_exit(conn, row) else "noop"] += 1
    return counts


def open_outcomes_summary(conn: sqlite3.Connection) -> list:
    """Return a list of (strategy_id, symbol, entry_ts, entry_price, days_open)."""
    rows = conn.execute(
        """
        SELECT s.strategy_id, s.symbol, o.entry_ts, o.entry_price
          FROM outcomes o
          JOIN signals  s ON s.id = o.signal_id
         WHERE o.status = 'open'
         ORDER BY o.entry_ts ASC
        """
    ).fetchall()
    today = date.today()
    out = []
    for r in rows:
        try:
            ed = date.fromisoformat(r["entry_ts"][:10])
            days = (today - ed).days
        except Exception:
            days = None
        out.append({
            "strategy_id": r["strategy_id"],
            "symbol":      r["symbol"],
            "entry_ts":    r["entry_ts"],
            "entry_price": r["entry_price"],
            "days_open":   days,
        })
    return out


if __name__ == "__main__":
    conn = db.init_db()
    counts = reconcile_signals(conn)
    print(f"reconciled: opened={counts['opened']}  closed={counts['closed']}  noop={counts['noop']}")
    open_pos = open_outcomes_summary(conn)
    print(f"open outcomes: {len(open_pos)}")
    for p in open_pos:
        print(f"  {p['strategy_id']:<35} {p['symbol']:<8} entry@{p['entry_price']:.2f} on {p['entry_ts'][:10]}  ({p['days_open']}d)")
    conn.close()
