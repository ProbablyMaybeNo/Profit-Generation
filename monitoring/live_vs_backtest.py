"""
live_vs_backtest.py — Weekly live-vs-backtest divergence report (milestone 3.6.2).

For every strategy with at least one closed live outcome in the trailing
window (default 7 days), compute:

  live_mean_pct      = mean per-trade return across the closed live
                        outcomes in the window
  backtest_mean_pct  = mean per-trade return from the strategy's
                        validator test_runs (same formula strategy_health
                        already uses for auto-pause)
  ratio              = live_mean_pct / backtest_mean_pct   when bt > 0
  flag               = "warn"  iff bt > 0 AND ratio < 0.5   (< 50% of bt)
                       "watch" iff bt > 0 AND 0.5 <= ratio < 0.8
                       "ok"    iff bt > 0 AND ratio >= 0.8
                       "info"  iff bt <= 0 or bt is None

Renders as a markdown table sorted by ratio ascending (worst first) and
posts to the daily-reports Notion DB as a fresh page tagged
"Live-vs-Backtest". Runs Sunday 18:00 PT alongside the weekly digest.

CLI:
  py -3.13 -m monitoring.live_vs_backtest              # window = last 7 days
  py -3.13 -m monitoring.live_vs_backtest --asof 2026-05-17
  py -3.13 -m monitoring.live_vs_backtest --window-days 14
  py -3.13 -m monitoring.live_vs_backtest --dry-run    # print, don't post
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402
from monitoring.strategy_health import backtest_mean_return_pct  # noqa: E402

DEFAULT_WINDOW_DAYS = 7
WARN_RATIO = 0.5   # < 50% of backtest → ⚠️ flag per spec
WATCH_RATIO = 0.8  # 50% – 80% → soft "watch" flag


def _live_outcomes_in_window(
    conn: sqlite3.Connection, *, start_iso: str, end_iso: str,
) -> Dict[str, List[float]]:
    """Closed live outcomes per strategy whose exit_ts falls in
    [start_iso, end_iso). Live = backed by a paper_trades BUY row.

    Returns {strategy_id: [return_pct, ...]} sorted-not-guaranteed.
    """
    rows = conn.execute(
        "SELECT s.strategy_id, o.return_pct "
        "  FROM outcomes o "
        "  JOIN signals s ON s.id = o.signal_id "
        "  JOIN paper_trades pt ON pt.signal_id = o.signal_id "
        "                       AND pt.side = 'buy' "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        "   AND o.exit_ts >= ? AND o.exit_ts < ? "
        " GROUP BY o.signal_id ",
        (start_iso, end_iso),
    ).fetchall()
    out: Dict[str, List[float]] = {}
    for r in rows:
        sid = r["strategy_id"] or ""
        if not sid:
            continue
        out.setdefault(sid, []).append(float(r["return_pct"]))
    return out


def _classify(bt: Optional[float], ratio: Optional[float]) -> str:
    if bt is None or bt <= 0 or ratio is None:
        return "info"
    if ratio < WARN_RATIO:
        return "warn"
    if ratio < WATCH_RATIO:
        return "watch"
    return "ok"


def compute_divergence(
    conn: sqlite3.Connection,
    *,
    asof: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Dict:
    """Roll up live-vs-backtest divergence for the trailing window.

    Window: [asof - window_days, asof], evaluated on outcome `exit_ts`.
    Shape:

      {
        "window_start": "YYYY-MM-DD",
        "window_end":   "YYYY-MM-DD",
        "asof_iso":     "...",
        "rows": [
          {"strategy_id": ..., "n_live": int, "live_mean_pct": float,
           "backtest_mean_pct": float | None, "ratio": float | None,
           "flag": "warn"|"watch"|"ok"|"info"},
          ...
        ],
        "n_strategies": int,
        "n_warn": int,
        "n_trades_total": int,
      }
    """
    window_start = (asof - timedelta(days=window_days)).isoformat()
    window_end = (asof + timedelta(days=1)).isoformat()
    by_strat = _live_outcomes_in_window(
        conn, start_iso=window_start, end_iso=window_end,
    )

    rows: List[Dict] = []
    n_warn = 0
    n_trades_total = 0
    for sid in sorted(by_strat.keys()):
        rets = by_strat[sid]
        n = len(rets)
        if n == 0:
            continue
        live_mean = sum(rets) / n
        bt = backtest_mean_return_pct(conn, sid)
        if bt is not None and bt > 0:
            ratio: Optional[float] = live_mean / bt
        else:
            ratio = None
        flag = _classify(bt, ratio)
        if flag == "warn":
            n_warn += 1
        rows.append({
            "strategy_id": sid,
            "n_live": n,
            "live_mean_pct": round(live_mean, 4),
            "backtest_mean_pct": round(bt, 4) if bt is not None else None,
            "ratio": round(ratio, 4) if ratio is not None else None,
            "flag": flag,
        })
        n_trades_total += n

    # Sort: warn first (lowest ratio first inside warn), then watch, then
    # ok, then info (no ratio). Within each bucket, lowest ratio first.
    flag_order = {"warn": 0, "watch": 1, "ok": 2, "info": 3}

    def _sort_key(r):
        f = flag_order.get(r["flag"], 9)
        ratio = r["ratio"] if r["ratio"] is not None else 1e9
        return (f, ratio, r["strategy_id"])
    rows.sort(key=_sort_key)

    return {
        "window_start": (asof - timedelta(days=window_days)).isoformat(),
        "window_end": asof.isoformat(),
        "asof_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
        "n_strategies": len(rows),
        "n_warn": n_warn,
        "n_trades_total": n_trades_total,
    }


def render_markdown(rollup: Dict) -> str:
    rows = rollup["rows"]
    lines: List[str] = []
    lines.append(f"# Live-vs-Backtest Divergence — "
                 f"{rollup['window_start']} → {rollup['window_end']}")
    lines.append("")
    if not rows:
        lines.append("No closed live outcomes in this window.")
        lines.append("")
        return "\n".join(lines)

    if rollup["n_warn"] > 0:
        lines.append(
            f"## ⚠️ {rollup['n_warn']} strategy(ies) running below 50% of "
            f"backtest expectation"
        )
    else:
        lines.append("## All strategies within tolerance")
    lines.append("")
    lines.append(
        f"Scope: {rollup['n_trades_total']} live outcomes across "
        f"{rollup['n_strategies']} strategy(ies)."
    )
    lines.append("")
    lines.append("| strategy | n | live mean | backtest mean | ratio | flag |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        bt = r["backtest_mean_pct"]
        ratio = r["ratio"]
        live = r["live_mean_pct"]
        flag = r["flag"]
        icon = {"warn": "⚠️", "watch": "🟠", "ok": "✅", "info": "ℹ️"}.get(flag, "")
        bt_str = "—" if bt is None else f"{bt:+.2f}%"
        ratio_str = "—" if ratio is None else f"{ratio:.2f}"
        lines.append(
            f"| `{r['strategy_id']}` | {r['n_live']} | "
            f"{live:+.2f}% | {bt_str} | {ratio_str} | {icon} {flag} |"
        )
    lines.append("")
    lines.append(
        f"_Thresholds: warn < {WARN_RATIO:.2f} · watch < {WATCH_RATIO:.2f} · "
        f"ok >= {WATCH_RATIO:.2f}. Backtest mean <= 0 → info (no positive "
        f"edge to diverge from)._"
    )
    lines.append("")
    return "\n".join(lines)


def post_to_notion(
    *, window_start: str, window_end: str, markdown: str,
    database_id: Optional[str] = None,
) -> Dict:
    """Post the divergence report as a new page in the daily-reports DB
    tagged "Live-vs-Backtest"."""
    from monitoring import notion_writer
    from monitoring.config import NOTION_DAILY_REPORTS_DB_ID
    db_id = database_id or NOTION_DAILY_REPORTS_DB_ID

    title = f"Live-vs-Backtest — {window_start} → {window_end}"
    properties = {
        "Report": {"title": [{"text": {"content": title}}]},
        "Date": {"date": {"start": window_end}},
        "Importance": {"number": 3},
        "Has Notable Pattern": {"checkbox": False},
        "Watchlist Count": {"number": 0},
        "Strategy Fires": {"number": 0},
        "Symbols Watched": {"multi_select": []},
        "Tags": {"multi_select": [{"name": "Live-vs-Backtest"}]},
        "Status": {"select": {"name": "Generated"}},
        "Source": {"select": {"name": "live-vs-backtest"}},
    }
    body = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji", "emoji": "\U0001f4ca"},
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


def build_report(
    *, asof: Optional[date] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    asof = asof or date.today()
    own_conn = conn is None
    if own_conn:
        conn = db.init_db()
    try:
        rollup = compute_divergence(conn, asof=asof, window_days=window_days)
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
    report = build_report(asof=asof, window_days=args.window_days)
    if args.dry_run:
        print(report["markdown"])
        return
    try:
        resp = post_to_notion(
            window_start=report["rollup"]["window_start"],
            window_end=report["rollup"]["window_end"],
            markdown=report["markdown"],
        )
        page_id = resp.get("id")
        log(f"Live-vs-backtest report posted to Notion as page {page_id}",
            "SUCCESS")
    except Exception as e:
        log(f"Live-vs-backtest post FAILED: {e}", "ERROR")
        print(report["markdown"])
        sys.exit(1)


if __name__ == "__main__":
    main()
