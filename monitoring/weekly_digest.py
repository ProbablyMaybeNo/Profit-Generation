"""
weekly_digest.py — Sunday recap posted to Notion's daily-reports DB.

Aggregates the last 7 calendar days of activity from trading.db:
  - signals fired (split by signal_type)
  - outcomes closed in the window (mean / win-rate)
  - top performer + biggest loser by total return contribution
  - newly added strategies (any record with first_logged_iso in the window)

Renders as a markdown summary. Posts as a fresh Notion page tagged
"Weekly Digest" so the existing daily-reports DB stays the single
source of truth without forcing the dashboard to learn a second source.

CLI:
  py -3.13 -m monitoring.weekly_digest              # window = last 7 days
  py -3.13 -m monitoring.weekly_digest --asof 2026-05-17
  py -3.13 -m monitoring.weekly_digest --window-days 14
  py -3.13 -m monitoring.weekly_digest --dry-run    # print, don't post

Scheduling: register_weekly.bat fires this at 18:00 PT on Sundays.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402

DEFAULT_WINDOW_DAYS = 7


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_stats(rets: List[float]) -> Dict[str, float]:
    n = len(rets)
    if n == 0:
        return {"n": 0, "mean": 0.0, "win_rate": 0.0,
                "sum_ret": 0.0, "best": 0.0, "worst": 0.0}
    mean = sum(rets) / n
    wins = sum(1 for r in rets if r > 0)
    return {
        "n": n,
        "mean": round(mean, 4),
        "win_rate": round(wins / n, 4),
        "sum_ret": round(sum(rets), 4),
        "best": round(max(rets), 4),
        "worst": round(min(rets), 4),
    }


def aggregate_window(
    conn: sqlite3.Connection,
    *,
    asof: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Dict:
    """Roll up the last `window_days` days ending on `asof` (inclusive).

    Returns a dict shaped:
      {"window_start": "YYYY-MM-DD",
       "window_end": "YYYY-MM-DD",
       "fires": {"long_entry": N, "long_exit": N, ...},
       "outcomes": {"n": N, "mean": M, "win_rate": W,
                     "sum_ret": S, "best": B, "worst": W},
       "by_strategy": [{"strategy_id": "...", **stats}, ...],
       "top_performer": {...} | None,
       "biggest_loser": {...} | None,
       "new_strategies": ["...", ...]}
    """
    start = asof - timedelta(days=window_days - 1)
    start_iso, end_iso = start.isoformat(), asof.isoformat()

    fires: Dict[str, int] = {}
    rows = conn.execute(
        "SELECT signal_type, COUNT(*) AS c FROM signals "
        " WHERE bar_ts BETWEEN ? AND ? "
        " GROUP BY signal_type",
        (start_iso, end_iso),
    ).fetchall()
    for r in rows:
        fires[r["signal_type"]] = int(r["c"])

    outcome_rows = conn.execute(
        "SELECT s.strategy_id, o.return_pct, o.exit_ts "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval='1d' "
        "   AND DATE(o.exit_ts) BETWEEN ? AND ? ",
        (start_iso, end_iso),
    ).fetchall()
    all_returns = [float(r["return_pct"]) for r in outcome_rows]
    outcomes_summary = _safe_stats(all_returns)

    by_strat_returns: Dict[str, List[float]] = {}
    for r in outcome_rows:
        by_strat_returns.setdefault(r["strategy_id"], []).append(
            float(r["return_pct"]),
        )
    by_strategy = [
        {"strategy_id": sid, **_safe_stats(rs)}
        for sid, rs in by_strat_returns.items()
    ]
    by_strategy.sort(key=lambda x: x["sum_ret"], reverse=True)

    top_performer = by_strategy[0] if by_strategy else None
    biggest_loser = by_strategy[-1] if by_strategy else None
    if top_performer is biggest_loser:
        biggest_loser = None
    if biggest_loser is not None and biggest_loser["sum_ret"] >= 0:
        biggest_loser = None

    new_strats = conn.execute(
        "SELECT strategy_id FROM strategies "
        " WHERE first_logged_iso IS NOT NULL "
        "   AND DATE(first_logged_iso) BETWEEN ? AND ? "
        " ORDER BY strategy_id",
        (start_iso, end_iso),
    ).fetchall()
    new_list = [r["strategy_id"] for r in new_strats]

    return {
        "window_start": start_iso,
        "window_end": end_iso,
        "fires": fires,
        "outcomes": outcomes_summary,
        "by_strategy": by_strategy,
        "top_performer": top_performer,
        "biggest_loser": biggest_loser,
        "new_strategies": new_list,
    }


def render_markdown(rollup: Dict) -> str:
    fires = rollup.get("fires") or {}
    outcomes = rollup.get("outcomes") or {}
    by_strategy = rollup.get("by_strategy") or []
    top = rollup.get("top_performer")
    worst = rollup.get("biggest_loser")
    new_strats = rollup.get("new_strategies") or []

    total_fires = sum(fires.values())
    fire_breakdown = ", ".join(
        f"{k}={v}" for k, v in sorted(fires.items())
    ) or "(none)"

    lines: List[str] = []
    lines.append(
        f"## Weekly Digest — {rollup['window_start']} → {rollup['window_end']}"
    )
    lines.append("")
    lines.append("### Activity")
    lines.append(f"- **Total fires:** {total_fires} ({fire_breakdown})")
    lines.append(
        f"- **Closed outcomes:** {outcomes['n']}  "
        f"mean **{outcomes['mean']:+.2f}%**, "
        f"win-rate **{outcomes['win_rate']*100:.0f}%**"
    )
    lines.append(
        f"- **Net return contribution:** **{outcomes['sum_ret']:+.2f}%** "
        f"(best trade {outcomes['best']:+.2f}%, "
        f"worst trade {outcomes['worst']:+.2f}%)"
    )
    lines.append("")

    if top is not None:
        lines.append(
            f"### Top performer: `{top['strategy_id']}`"
        )
        lines.append(
            f"- {top['n']} closed trades, "
            f"sum_ret **{top['sum_ret']:+.2f}%**, "
            f"mean {top['mean']:+.2f}%, "
            f"win-rate {top['win_rate']*100:.0f}%"
        )
        lines.append("")

    if worst is not None:
        lines.append(
            f"### Biggest loser: `{worst['strategy_id']}`"
        )
        lines.append(
            f"- {worst['n']} closed trades, "
            f"sum_ret **{worst['sum_ret']:+.2f}%**, "
            f"mean {worst['mean']:+.2f}%, "
            f"win-rate {worst['win_rate']*100:.0f}%"
        )
        lines.append("")

    if by_strategy:
        lines.append("### By strategy")
        lines.append("")
        lines.append("| strategy | n | sum | mean | win rate |")
        lines.append("|---|---|---|---|---|")
        for row in by_strategy:
            lines.append(
                f"| `{row['strategy_id']}` | {row['n']} | "
                f"{row['sum_ret']:+.2f}% | {row['mean']:+.2f}% | "
                f"{row['win_rate']*100:.0f}% |"
            )
        lines.append("")

    if new_strats:
        bullets = ", ".join(f"`{s}`" for s in new_strats)
        lines.append("### New strategies added")
        lines.append(f"- {len(new_strats)}: {bullets}")
        lines.append("")
    else:
        lines.append("### New strategies added")
        lines.append("- (none)")
        lines.append("")

    return "\n".join(lines)


def post_to_notion(
    *, window_start: str, window_end: str, markdown: str,
    database_id: Optional[str] = None,
) -> Dict:
    """Post the digest as a new page in the daily-reports DB with
    `Source = "weekly-digest"` and a `Weekly Digest` tag."""
    from monitoring import notion_writer
    from monitoring.config import NOTION_DAILY_REPORTS_DB_ID
    db_id = database_id or NOTION_DAILY_REPORTS_DB_ID

    title = f"Weekly Digest — {window_start} → {window_end}"
    properties = {
        "Report": {"title": [{"text": {"content": title}}]},
        "Date": {"date": {"start": window_end}},
        "Importance": {"number": 3},
        "Has Notable Pattern": {"checkbox": False},
        "Watchlist Count": {"number": 0},
        "Strategy Fires": {"number": 0},
        "Symbols Watched": {"multi_select": []},
        "Tags": {"multi_select": [{"name": "Weekly Digest"}]},
        "Status": {"select": {"name": "Generated"}},
        "Source": {"select": {"name": "weekly-digest"}},
    }
    body = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji", "emoji": "\U0001f4c5"},
        "properties": properties,
        "children": notion_writer._markdown_to_blocks(markdown)[:100],
    }
    import requests
    r = requests.post(
        f"{notion_writer.NOTION_API}/pages",
        headers=notion_writer._headers(),
        json=body, timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Notion API {r.status_code}: {r.text[:500]}")
    return r.json()


def build_digest(
    *, asof: Optional[date] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    """Compose the digest payload + markdown without posting."""
    asof = asof or date.today()
    own_conn = conn is None
    if own_conn:
        conn = db.init_db()
    try:
        rollup = aggregate_window(conn, asof=asof, window_days=window_days)
    finally:
        if own_conn:
            conn.close()
    markdown = render_markdown(rollup)
    return {"rollup": rollup, "markdown": markdown}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", help="ISO date (default: today)")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the markdown instead of posting to Notion")
    args = parser.parse_args()
    asof = date.fromisoformat(args.asof) if args.asof else date.today()
    digest = build_digest(asof=asof, window_days=args.window_days)
    if args.dry_run:
        print(digest["markdown"])
        return
    try:
        resp = post_to_notion(
            window_start=digest["rollup"]["window_start"],
            window_end=digest["rollup"]["window_end"],
            markdown=digest["markdown"],
        )
        page_id = resp.get("id")
        log(f"Weekly digest posted to Notion as page {page_id}", "SUCCESS")
    except Exception as e:
        log(f"Weekly digest post FAILED: {e}", "ERROR")
        print(digest["markdown"])
        sys.exit(1)


if __name__ == "__main__":
    main()
