"""
score_live_candidates.py — Rank active strategies by paper-trading track
record and surface candidates that have earned a live promotion.

For every strategy that has at least one closed *live* outcome (= an
outcome whose signal is backed by a `paper_trades` BUY row), compute:

  n             — number of closed live outcomes
  mean_ret_pct  — mean per-trade return %, across the closed live outcomes
  sharpe        — stable Sharpe-ish (mean / stdev) over the same returns
  score         — mean_ret_pct * sqrt(n) * sharpe

A strategy is flagged `READY_FOR_LIVE` when:
  - n            >= 50
  - sharpe       > 0.4
  - mean_ret_pct > 0
  - strategy_id  NOT already in auto_trade.live_strategies

Output:
  - sorted ranking printed to stdout (best score first)
  - exit 0 always (this script SURFACES candidates; it never flips a flag)
  - Notion: optional post (skipped when --no-notion or notion missing)

Usage:
  py -3.13 scripts/score_live_candidates.py
  py -3.13 scripts/score_live_candidates.py --json
  py -3.13 scripts/score_live_candidates.py --no-notion
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import load_settings, log  # noqa: E402
from data import db  # noqa: E402


MIN_OUTCOMES = 50
MIN_SHARPE = 0.4
MIN_MEAN_RET = 0.0

READY_TAG = "READY_FOR_LIVE"
SKIP_ALREADY_LIVE = "ALREADY_LIVE"
SKIP_THIN = "INSUFFICIENT_SAMPLE"
SKIP_NEGATIVE = "NEGATIVE_EDGE"
SKIP_LOW_SHARPE = "LOW_SHARPE"


def _sharpe_ish(rets: List[float]) -> float:
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    sd = statistics.stdev(rets)
    return (mean / sd) if sd > 0 else 0.0


def _live_outcomes_by_strategy(conn: sqlite3.Connection) -> Dict[str, List[float]]:
    """Closed live outcomes grouped by strategy_id.

    `Live` = there's a `paper_trades` BUY row referencing the outcome's
    signal. Pure-validator runs never write paper_trades, so they are
    excluded cleanly — same pattern as strategy_health._live_outcomes.
    """
    rows = conn.execute(
        "SELECT s.strategy_id, o.return_pct "
        "  FROM outcomes o "
        "  JOIN signals s ON s.id = o.signal_id "
        "  JOIN paper_trades pt ON pt.signal_id = o.signal_id "
        "                       AND pt.side = 'buy' "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        " GROUP BY o.signal_id"
    ).fetchall()
    out: Dict[str, List[float]] = {}
    for r in rows:
        sid = r["strategy_id"] or ""
        if not sid:
            continue
        out.setdefault(sid, []).append(float(r["return_pct"]))
    return out


def _already_live(settings: Dict) -> set:
    auto = settings.get("auto_trade") or {}
    listed = auto.get("live_strategies") or []
    return {str(x) for x in listed}


def evaluate_strategy(
    strategy_id: str,
    returns: List[float],
    *,
    already_live: bool,
    min_outcomes: int = MIN_OUTCOMES,
    min_sharpe: float = MIN_SHARPE,
    min_mean_ret: float = MIN_MEAN_RET,
) -> Dict:
    n = len(returns)
    mean = (sum(returns) / n) if n else 0.0
    sharpe = _sharpe_ish(returns)
    # Score = mean × √N × sharpe. Punishes thin samples and noisy ones.
    score = mean * math.sqrt(n) * sharpe if n > 0 else 0.0

    if already_live:
        verdict = SKIP_ALREADY_LIVE
        reason = "already in auto_trade.live_strategies"
    elif n < min_outcomes:
        verdict = SKIP_THIN
        reason = f"only {n} closed live outcomes (need >= {min_outcomes})"
    elif mean <= min_mean_ret:
        verdict = SKIP_NEGATIVE
        reason = f"mean return {mean:+.4f}% is not positive"
    elif sharpe <= min_sharpe:
        verdict = SKIP_LOW_SHARPE
        reason = f"sharpe {sharpe:.3f} <= {min_sharpe:.2f}"
    else:
        verdict = READY_TAG
        reason = (f"n={n}, mean={mean:+.4f}%, sharpe={sharpe:.3f} — "
                  f"all thresholds cleared")

    return {
        "strategy_id": strategy_id,
        "n": n,
        "mean_ret_pct": round(mean, 4),
        "sharpe": round(sharpe, 4),
        "score": round(score, 4),
        "verdict": verdict,
        "reason": reason,
        "already_live": already_live,
    }


def score_candidates(
    conn: sqlite3.Connection,
    *,
    settings: Optional[Dict] = None,
    min_outcomes: int = MIN_OUTCOMES,
    min_sharpe: float = MIN_SHARPE,
    min_mean_ret: float = MIN_MEAN_RET,
) -> List[Dict]:
    """Rank every strategy with closed live outcomes. Returns sorted rows
    (highest score first, then strategy_id alpha for stability)."""
    cfg = settings if settings is not None else load_settings()
    live_set = _already_live(cfg)
    by_strat = _live_outcomes_by_strategy(conn)
    rows: List[Dict] = []
    for sid, rets in by_strat.items():
        rows.append(evaluate_strategy(
            sid, rets,
            already_live=(sid in live_set),
            min_outcomes=min_outcomes,
            min_sharpe=min_sharpe,
            min_mean_ret=min_mean_ret,
        ))
    rows.sort(key=lambda r: (-r["score"], r["strategy_id"]))
    return rows


def format_report(rows: List[Dict]) -> str:
    if not rows:
        return "No closed live outcomes yet — no candidates to score."
    lines: List[str] = []
    width = max(len(r["strategy_id"]) for r in rows) + 2
    n_ready = sum(1 for r in rows if r["verdict"] == READY_TAG)
    lines.append(
        f"Live-promotion candidates — {n_ready} of {len(rows)} ready"
    )
    lines.append("")
    header = (
        f"  {'strategy_id':<{width}} {'n':>5}  {'mean%':>9}  "
        f"{'sharpe':>7}  {'score':>9}  verdict"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for r in rows:
        lines.append(
            f"  {r['strategy_id']:<{width}} {r['n']:>5}  "
            f"{r['mean_ret_pct']:>+9.4f}  {r['sharpe']:>7.3f}  "
            f"{r['score']:>9.4f}  {r['verdict']}"
        )
    lines.append("")
    lines.append(
        f"Thresholds: n >= {MIN_OUTCOMES}, sharpe > {MIN_SHARPE}, "
        f"mean > {MIN_MEAN_RET}. This script surfaces candidates only — "
        f"promotion to auto_trade.live_strategies is a deliberate human flip."
    )
    return "\n".join(lines)


def render_markdown(rows: List[Dict]) -> str:
    """Notion-friendly version of the report."""
    if not rows:
        return ("# Live-Promotion Scoring\n\n"
                "No closed live outcomes yet — no candidates to score.\n")
    n_ready = sum(1 for r in rows if r["verdict"] == READY_TAG)
    lines: List[str] = [
        "# Live-Promotion Scoring",
        "",
        f"**{n_ready}** of **{len(rows)}** strategies cleared every "
        f"`READY_FOR_LIVE` threshold.",
        "",
        f"_Thresholds: n >= {MIN_OUTCOMES}, sharpe > {MIN_SHARPE}, "
        f"mean > {MIN_MEAN_RET}. Score = mean × √n × sharpe._",
        "",
        "| strategy | n | mean % | sharpe | score | verdict |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['strategy_id']}` | {r['n']} | "
            f"{r['mean_ret_pct']:+.4f}% | {r['sharpe']:.3f} | "
            f"{r['score']:.4f} | {r['verdict']} |"
        )
    lines.append("")
    lines.append(
        "_This report SURFACES candidates only — "
        "promotion to `auto_trade.live_strategies` is a deliberate human flip._"
    )
    return "\n".join(lines)


def post_to_notion(
    rows: List[Dict],
    *,
    database_id: Optional[str] = None,
) -> Dict:
    """Post the ranking as a fresh page in the daily-reports DB tagged
    "Live-Promotion"."""
    from monitoring import notion_writer
    from monitoring.config import NOTION_DAILY_REPORTS_DB_ID

    db_id = database_id or NOTION_DAILY_REPORTS_DB_ID
    today = datetime.now(timezone.utc).date().isoformat()
    title = f"Live-Promotion Scoring — {today}"
    markdown = render_markdown(rows)
    n_ready = sum(1 for r in rows if r["verdict"] == READY_TAG)
    properties = {
        "Report": {"title": [{"text": {"content": title}}]},
        "Date": {"date": {"start": today}},
        "Importance": {"number": 3 if n_ready else 2},
        "Has Notable Pattern": {"checkbox": bool(n_ready)},
        "Watchlist Count": {"number": 0},
        "Strategy Fires": {"number": 0},
        "Symbols Watched": {"multi_select": []},
        "Tags": {"multi_select": [{"name": "Live-Promotion"}]},
        "Status": {"select": {"name": "Generated"}},
        "Source": {"select": {"name": "live-promotion-scorer"}},
    }
    body = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji", "emoji": "\U0001f3af"},
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON instead of a table")
    parser.add_argument("--no-notion", action="store_true",
                        help="skip the Notion post (always skipped when "
                             "the notion section is unconfigured)")
    parser.add_argument("--min-outcomes", type=int, default=MIN_OUTCOMES)
    parser.add_argument("--min-sharpe", type=float, default=MIN_SHARPE)
    parser.add_argument("--min-mean-ret", type=float, default=MIN_MEAN_RET)
    args = parser.parse_args()

    conn = db.init_db()
    try:
        rows = score_candidates(
            conn,
            min_outcomes=args.min_outcomes,
            min_sharpe=args.min_sharpe,
            min_mean_ret=args.min_mean_ret,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(format_report(rows))

    if not args.no_notion:
        try:
            resp = post_to_notion(rows)
            log(f"Live-promotion report posted to Notion "
                f"(page {resp.get('id')})", "SUCCESS")
        except Exception as e:
            log(f"Notion post skipped: {e}", "WARNING")

    # Surface-only — never non-zero, even when there are zero candidates.
    sys.exit(0)


if __name__ == "__main__":
    main()
