"""
cross_validation.py — Weekly comparison across the three signal sources
(EOD '1d', intraday projection '1d-intraday', TV webhook 'tv-webhook').

For each (strategy, symbol, bar_ts) tuple seen in the last N days,
collects the set of (bar_interval, signal_type) pairs that fired and
flags the tuple as a disagreement whenever the signal_types differ
across the sources. The intent is to surface places where the EOD
truth disagrees with the intraday projection or the TV webhook —
e.g., intraday projected a long_entry that the EOD bar never confirmed.

Output gets posted to the Notion patterns DB as one row per
disagreement, plus a markdown summary returned for any caller that
wants to log it.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402

DEFAULT_WINDOW_DAYS = 7
SOURCES = ("1d", "1d-intraday", "tv-webhook")


def collect_signals_in_window(
    conn: sqlite3.Connection,
    *,
    asof: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> List[Dict]:
    """Pull every signal in the window whose bar_interval is one of SOURCES."""
    start = asof - timedelta(days=window_days - 1)
    rows = conn.execute(
        "SELECT strategy_id, symbol, bar_ts, bar_interval, signal_type "
        "  FROM signals "
        " WHERE bar_ts BETWEEN ? AND ? "
        "   AND bar_interval IN ('1d', '1d-intraday', 'tv-webhook')",
        (start.isoformat(), asof.isoformat()),
    ).fetchall()
    return [{
        "strategy_id": r["strategy_id"],
        "symbol":       r["symbol"],
        "bar_ts":       r["bar_ts"],
        "bar_interval": r["bar_interval"],
        "signal_type":  r["signal_type"],
    } for r in rows]


def group_by_tuple(rows: List[Dict]) -> Dict[Tuple[str, str, str], Dict[str, set]]:
    """Return {(strategy, symbol, bar_ts): {bar_interval: {signal_types}}}."""
    out: Dict[Tuple[str, str, str], Dict[str, set]] = {}
    for r in rows:
        key = (r["strategy_id"], r["symbol"], r["bar_ts"])
        bucket = out.setdefault(key, {})
        bucket.setdefault(r["bar_interval"], set()).add(r["signal_type"])
    return out


def find_disagreements(
    grouped: Dict[Tuple[str, str, str], Dict[str, set]],
) -> List[Dict]:
    """Flag every (strategy, symbol, bar_ts) where ≥2 sources fired and
    their signal_type sets differ.

    Output rows: {strategy_id, symbol, bar_ts, sources: {bar_interval: [types]},
                   disagreement_type: str, diff: list[str]}.
    """
    out: List[Dict] = []
    for (sid, sym, bar_ts), by_source in grouped.items():
        if len(by_source) < 2:
            continue
        type_sets = list(by_source.values())
        if all(ts == type_sets[0] for ts in type_sets):
            continue
        union: set = set().union(*type_sets)
        intersection: set = set(type_sets[0]).intersection(*type_sets[1:])
        diff = sorted(union - intersection)
        # Disagreement_type narrative: which source said what extra.
        per_source = {
            iv: sorted(ts) for iv, ts in by_source.items()
        }
        out.append({
            "strategy_id":      sid,
            "symbol":           sym,
            "bar_ts":           bar_ts,
            "sources":          per_source,
            "disagreement_type": ", ".join(diff) or "mismatch",
            "diff":             diff,
        })
    out.sort(key=lambda r: (r["bar_ts"], r["strategy_id"], r["symbol"]))
    return out


def compute_cross_validation(
    conn: sqlite3.Connection,
    *,
    asof: Optional[date] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Dict:
    """End-to-end rollup with metadata."""
    asof = asof or date.today()
    rows = collect_signals_in_window(conn, asof=asof, window_days=window_days)
    grouped = group_by_tuple(rows)
    disagreements = find_disagreements(grouped)
    return {
        "window_start": (asof - timedelta(days=window_days - 1)).isoformat(),
        "window_end":   asof.isoformat(),
        "n_signals":    len(rows),
        "n_tuples":     len(grouped),
        "n_disagreements": len(disagreements),
        "disagreements": disagreements,
        "evaluated_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def render_markdown(rollup: Dict) -> str:
    lines: List[str] = []
    lines.append(
        f"## Cross-validation — {rollup['window_start']} → {rollup['window_end']}"
    )
    lines.append("")
    lines.append(
        f"Reviewed **{rollup['n_signals']}** signals across "
        f"**{rollup['n_tuples']}** (strategy, symbol, date) tuples — "
        f"**{rollup['n_disagreements']}** disagreement(s) found."
    )
    lines.append("")
    if not rollup["disagreements"]:
        lines.append("_No source disagreements in this window._")
        return "\n".join(lines)
    lines.append("| date | strategy | symbol | 1d | 1d-intraday | tv-webhook | diff |")
    lines.append("|---|---|---|---|---|---|---|")
    for d in rollup["disagreements"]:
        s = d["sources"]
        eod = ", ".join(s.get("1d", [])) or "—"
        intr = ", ".join(s.get("1d-intraday", [])) or "—"
        tv = ", ".join(s.get("tv-webhook", [])) or "—"
        diff = ", ".join(d["diff"]) or "—"
        lines.append(
            f"| {d['bar_ts'][:10]} | `{d['strategy_id']}` | {d['symbol']} | "
            f"{eod} | {intr} | {tv} | {diff} |"
        )
    return "\n".join(lines)


def post_to_notion(rollup: Dict, *, database_id: Optional[str] = None) -> Optional[Dict]:
    """Post one Notion patterns row per disagreement. Returns None if
    there are no disagreements; otherwise returns the last response."""
    if not rollup["disagreements"]:
        return None
    from monitoring import notion_writer
    from monitoring.config import NOTION_PATTERNS_DB_ID
    db_id = database_id or NOTION_PATTERNS_DB_ID
    last_response: Optional[Dict] = None
    for d in rollup["disagreements"]:
        title = (
            f"Cross-val: {d['strategy_id']} / {d['symbol']} "
            f"on {d['bar_ts'][:10]}"
        )
        description_lines = [
            f"Sources disagree on {d['symbol']} for {d['bar_ts'][:10]}.",
            "",
        ]
        for iv in SOURCES:
            types = d["sources"].get(iv, [])
            description_lines.append(
                f"- **{iv}**: {', '.join(types) if types else '(no fire)'}"
            )
        description_lines.append("")
        description_lines.append(
            f"Diff: {', '.join(d['diff']) or 'mismatch'}"
        )
        description = "\n".join(description_lines)
        last_response = notion_writer.post_pattern(
            title=title,
            description=description,
            importance="Medium",
            pattern_type="cross-validation",
            symbols=[d["symbol"]],
            status="Observation",
            date_observed=d["bar_ts"][:10],
            database_id=db_id,
        )
    return last_response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", help="ISO date (default: today)")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the markdown instead of posting to Notion")
    args = parser.parse_args()
    asof = date.fromisoformat(args.asof) if args.asof else date.today()
    conn = db.init_db()
    try:
        rollup = compute_cross_validation(
            conn, asof=asof, window_days=args.window_days,
        )
    finally:
        conn.close()
    md = render_markdown(rollup)
    if args.dry_run or rollup["n_disagreements"] == 0:
        print(md)
        return
    try:
        post_to_notion(rollup)
        log(
            f"Posted {rollup['n_disagreements']} cross-validation rows to Notion",
            "SUCCESS",
        )
    except Exception as e:
        log(f"Notion post failed: {e}", "ERROR")
        print(md)
        sys.exit(1)


if __name__ == "__main__":
    main()
