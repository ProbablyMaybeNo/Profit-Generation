"""
intraday_eod_report.py — 7.5.6 end-of-day intraday rollup.

Drinks the day's intraday_bars, intraday signals (signals where
bar_interval != '1d'), intraday_skips, and paper_trades to produce a
markdown summary. Posts to Notion via the existing channel
(notion_writer.post_daily_report).

The LLM-filter wiring shipped in 7.1.1 stays active in this milestone
— `settings.llm_filter.enabled` is still default False; flipping it on
is Ross's manual decision. This report is the analytics layer.

Sections:
  1. Fires by strategy (count + symbols)
  2. Skip breakdown by gate
  3. Paper P&L by strategy (today's closed trades)
  4. Top divergences — intraday signals vs end-of-day outcomes

Public entrypoint: ``generate_intraday_eod_report(conn, asof)`` returns
a markdown string. ``post_to_notion`` wraps that + the Notion upload.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def gather_intraday_fires(
    conn: sqlite3.Connection, asof: date,
) -> List[Dict[str, Any]]:
    """Return today's intraday fires (signals.bar_interval != '1d')."""
    today_prefix = asof.isoformat()
    rows = conn.execute(
        "SELECT id, ts, bar_ts, bar_interval, strategy_id, symbol, "
        "       signal_type, close "
        "  FROM signals "
        " WHERE substr(bar_ts, 1, 10) = ? "
        "   AND COALESCE(bar_interval, '1d') != '1d' "
        " ORDER BY ts ASC, id ASC",
        (today_prefix,),
    ).fetchall()
    return [dict(r) for r in rows]


def gather_intraday_bars_count(
    conn: sqlite3.Connection, asof: date,
) -> Dict[str, int]:
    """Return {symbol: bar_count} for today's intraday_bars rows."""
    today_prefix = asof.isoformat()
    rows = conn.execute(
        "SELECT symbol, COUNT(*) AS n FROM intraday_bars "
        " WHERE substr(ts_utc, 1, 10) = ? "
        " GROUP BY symbol ORDER BY symbol ASC",
        (today_prefix,),
    ).fetchall()
    return {r["symbol"]: int(r["n"]) for r in rows}


def gather_skips_today(
    conn: sqlite3.Connection, asof: date,
) -> List[Dict[str, Any]]:
    """Return all intraday_skips rows recorded today."""
    today_start = datetime.combine(
        asof, datetime.min.time(), tzinfo=timezone.utc,
    ).isoformat(timespec="seconds")
    today_end = datetime.combine(
        asof + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc,
    ).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, recorded_at, strategy_id, symbol, bar_ts, "
        "       signal_type, gate, reason_detail, source "
        "  FROM intraday_skips "
        " WHERE recorded_at >= ? AND recorded_at < ? "
        " ORDER BY id ASC",
        (today_start, today_end),
    ).fetchall()
    return [dict(r) for r in rows]


def gather_paper_pnl_today(
    conn: sqlite3.Connection, asof: date,
) -> List[Dict[str, Any]]:
    """Return today's closed outcomes joined to their originating signals.

    Each row: ``{strategy_id, symbol, bar_interval, return_pct,
    exit_reason}``. Filters on outcomes.exit_ts (closed today).
    """
    today_prefix = asof.isoformat()
    rows = conn.execute(
        "SELECT s.strategy_id, s.symbol, "
        "       COALESCE(s.bar_interval, '1d') AS bar_interval, "
        "       o.return_pct, o.exit_reason "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' "
        "   AND substr(COALESCE(o.exit_ts, ''), 1, 10) = ?",
        (today_prefix,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def fires_by_strategy(fires: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group fires by strategy → ``[{strategy_id, count, symbols}]``."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for f in fires:
        sid = f.get("strategy_id") or ""
        b = buckets.setdefault(sid, {
            "strategy_id": sid, "count": 0, "symbols": set(),
        })
        b["count"] += 1
        if f.get("symbol"):
            b["symbols"].add(f["symbol"])
    out = []
    for b in buckets.values():
        out.append({
            "strategy_id": b["strategy_id"],
            "count": b["count"],
            "symbols": sorted(b["symbols"]),
        })
    out.sort(key=lambda r: (-r["count"], r["strategy_id"]))
    return out


def skips_by_gate(skips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group skips by gate → ``[{gate, count}]`` sorted by count desc."""
    counts: Dict[str, int] = {}
    for s in skips:
        gate = s.get("gate") or "unknown"
        counts[gate] = counts.get(gate, 0) + 1
    out = [{"gate": g, "count": c} for g, c in counts.items()]
    out.sort(key=lambda r: (-r["count"], r["gate"]))
    return out


def pnl_by_strategy(
    outcomes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Group closed outcomes by strategy → totals + wins/losses."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for o in outcomes:
        sid = o.get("strategy_id") or ""
        ret = o.get("return_pct")
        if ret is None:
            continue
        b = buckets.setdefault(sid, {
            "strategy_id": sid, "n": 0, "wins": 0, "losses": 0,
            "total_return_pct": 0.0, "best": None, "worst": None,
        })
        b["n"] += 1
        if ret > 0:
            b["wins"] += 1
        elif ret < 0:
            b["losses"] += 1
        b["total_return_pct"] += ret
        if b["best"] is None or ret > b["best"]:
            b["best"] = ret
        if b["worst"] is None or ret < b["worst"]:
            b["worst"] = ret
    out = []
    for b in buckets.values():
        b["total_return_pct"] = round(b["total_return_pct"], 4)
        b["best"] = round(b["best"], 4) if b["best"] is not None else None
        b["worst"] = round(b["worst"], 4) if b["worst"] is not None else None
        out.append(b)
    out.sort(key=lambda r: (-r["total_return_pct"], r["strategy_id"]))
    return out


def find_divergences(
    fires: List[Dict[str, Any]],
    outcomes: List[Dict[str, Any]],
    *,
    threshold_pct: float = 3.0,
) -> List[Dict[str, Any]]:
    """Surface (strategy, symbol) pairs where the intraday signal direction
    disagreed sharply with the EOD outcome.

    A divergence is a long_entry intraday fire that closed at <= -threshold_pct
    on the same (strategy_id, symbol) by EOD. Returns up to 5 rows.
    """
    # Build a set of (sid, sym) that had intraday long_entry today.
    intraday_longs: set = {
        (f["strategy_id"], f["symbol"]) for f in fires
        if f.get("signal_type") == "long_entry"
    }
    div: List[Dict[str, Any]] = []
    for o in outcomes:
        sid = o.get("strategy_id")
        sym = o.get("symbol")
        ret = o.get("return_pct")
        if ret is None or sid is None or sym is None:
            continue
        if (sid, sym) in intraday_longs and ret <= -abs(threshold_pct):
            div.append({
                "strategy_id": sid, "symbol": sym,
                "return_pct": round(ret, 4),
                "exit_reason": o.get("exit_reason"),
            })
    div.sort(key=lambda r: r["return_pct"])
    return div[:5]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_markdown(
    *,
    asof: date,
    fires: List[Dict[str, Any]],
    bars_by_symbol: Dict[str, int],
    skips: List[Dict[str, Any]],
    outcomes: List[Dict[str, Any]],
) -> str:
    """Render the EOD intraday rollup as markdown."""
    lines: List[str] = []
    lines.append(f"# Intraday EOD Report — {asof.isoformat()}")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("")

    # Section 1 — fires by strategy
    lines.append("## 1. Fires by strategy")
    by_strat = fires_by_strategy(fires)
    if not by_strat:
        lines.append("_No intraday fires today._")
    else:
        lines.append("")
        lines.append("| strategy | count | symbols |")
        lines.append("|---|---:|---|")
        for row in by_strat:
            syms = ", ".join(row["symbols"]) or "—"
            lines.append(
                f"| `{row['strategy_id']}` | {row['count']} | {syms} |"
            )
    lines.append("")

    # Section 2 — skip breakdown
    lines.append("## 2. Skip breakdown by gate")
    by_gate = skips_by_gate(skips)
    if not by_gate:
        lines.append("_No skips recorded today._")
    else:
        lines.append("")
        lines.append("| gate | count |")
        lines.append("|---|---:|")
        for row in by_gate:
            lines.append(f"| `{row['gate']}` | {row['count']} |")
    lines.append("")

    # Section 3 — paper P&L by strategy
    lines.append("## 3. Paper P&L by strategy (closed today)")
    pnl = pnl_by_strategy(outcomes)
    if not pnl:
        lines.append("_No closed outcomes today._")
    else:
        lines.append("")
        lines.append("| strategy | n | wins | losses | total% | best% | worst% |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for row in pnl:
            total = row["total_return_pct"]
            best = row["best"] if row["best"] is not None else 0.0
            worst = row["worst"] if row["worst"] is not None else 0.0
            lines.append(
                f"| `{row['strategy_id']}` | {row['n']} | {row['wins']} | "
                f"{row['losses']} | {total:+.2f} | {best:+.2f} | {worst:+.2f} |"
            )
    lines.append("")

    # Section 4 — top divergences
    lines.append("## 4. Top divergences (intraday signal → EOD outcome)")
    div = find_divergences(fires, outcomes)
    if not div:
        lines.append("_No notable divergences (no intraday long fired and closed ≤ -3% today)._")
    else:
        lines.append("")
        lines.append("| strategy | symbol | return% | exit reason |")
        lines.append("|---|---|---:|---|")
        for row in div:
            reason = row.get("exit_reason") or "—"
            lines.append(
                f"| `{row['strategy_id']}` | {row['symbol']} | "
                f"{row['return_pct']:+.2f} | {reason} |"
            )
    lines.append("")

    # Section 5 — bar counts (for context)
    lines.append("## 5. Intraday bars ingested today")
    if not bars_by_symbol:
        lines.append("_No intraday_bars rows for today — listener offline?_")
    else:
        lines.append("")
        lines.append("| symbol | bars |")
        lines.append("|---|---:|")
        for sym in sorted(bars_by_symbol):
            lines.append(f"| {sym} | {bars_by_symbol[sym]} |")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def generate_intraday_eod_report(
    conn: sqlite3.Connection,
    asof: Optional[date] = None,
) -> str:
    """Generate the markdown EOD intraday report for `asof` (UTC today by default)."""
    asof_d = asof or _utc_today()
    fires = gather_intraday_fires(conn, asof_d)
    bars = gather_intraday_bars_count(conn, asof_d)
    skips = gather_skips_today(conn, asof_d)
    outcomes = gather_paper_pnl_today(conn, asof_d)
    return render_markdown(
        asof=asof_d,
        fires=fires,
        bars_by_symbol=bars,
        skips=skips,
        outcomes=outcomes,
    )


def post_to_notion(
    markdown: str,
    *,
    asof: date,
    database_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Post the rendered markdown to the Notion daily-reports DB.

    Returns the Notion API page response. Raises on any HTTP / auth
    failure (caller — usually the EOD scheduler — wraps).
    """
    from monitoring import notion_writer as nw
    from monitoring.config import NOTION_DAILY_REPORTS_DB_ID
    db_id = database_id or NOTION_DAILY_REPORTS_DB_ID
    report = {
        "report_date": asof.isoformat(),
        "title": f"Intraday EOD Report — {asof.isoformat()}",
        "market_regime": "intraday_eod",
        "importance": 2,
        "has_notable_pattern": False,
        "fires_count": 0,
        "watchlist_count": 0,
        "notable_movers_count": 0,
        "tags": ["intraday", "eod"],
    }
    return nw.post_daily_report(report, markdown, db_id)


def write_to_file(markdown: str, out_path: Path) -> Path:
    """Write the report to disk for local archive / debug."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from data import db as _db

    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", default=None,
                        help="YYYY-MM-DD (UTC). Defaults to today.")
    parser.add_argument("--out", default=None,
                        help="Write the markdown to this path instead of Notion.")
    parser.add_argument("--post", action="store_true",
                        help="Post to Notion in addition to writing to file.")
    args = parser.parse_args()

    asof = (date.fromisoformat(args.asof) if args.asof else _utc_today())
    conn = _db.init_db()
    try:
        md = generate_intraday_eod_report(conn, asof)
    finally:
        conn.close()
    if args.out:
        out = write_to_file(md, Path(args.out))
        print(f"Wrote {out}")
    if args.post:
        try:
            page = post_to_notion(md, asof=asof)
            print(f"Posted to Notion: {page.get('url') or page.get('id')}")
        except Exception as e:
            print(f"Notion post failed: {e}")
            sys.exit(1)
    if not args.out and not args.post:
        sys.stdout.write(md)
