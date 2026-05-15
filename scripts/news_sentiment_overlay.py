"""
news_sentiment_overlay.py — Slice each strategy's closed outcomes by
the sentiment of news on the traded symbol within ±1 day of entry.

Surfaces "trades into negative-sentiment days return +X% vs +Y% on
neutral days" style insights per strategy. Reads news.sentiment from
trading.db (Polygon insights JSON), parses the per-ticker sentiment
records that match the trade's symbol, and aggregates closed-outcome
returns by dominant sentiment label.

Writes a snapshot to logs/news_sentiment_overlay_YYYY-MM-DD.json on
every run. Idempotent.

CLI:
  py -3.13 scripts/news_sentiment_overlay.py
  py -3.13 scripts/news_sentiment_overlay.py --window-days 2
  py -3.13 scripts/news_sentiment_overlay.py --no-write
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402
from monitoring import news_sentiment_overlay as nso  # noqa: E402

DEFAULT_LOG_DIR = ROOT / "logs"


def default_out_path() -> Path:
    return DEFAULT_LOG_DIR / f"news_sentiment_overlay_{date.today().isoformat()}.json"


def write_report(report: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def render_table(rows: list) -> str:
    if not rows:
        return "(no closed outcomes with parseable news yet)"
    # Group by strategy for a compact display.
    by_strat: dict = defaultdict(list)
    for r in rows:
        by_strat[r["strategy_id"]].append(r)
    lines: list = []
    for sid in sorted(by_strat.keys()):
        lines.append(f"\n== {sid} ==")
        lines.append(
            f"  {'sentiment':<11} {'n':>4}  {'mean':>9}  {'win%':>6}  "
            f"{'best':>8}  {'worst':>8}"
        )
        for r in by_strat[sid]:
            lines.append(
                f"  {r['sentiment']:<11} {r['n']:>4}  "
                f"{r['mean']:+8.2f}%  {r['win_rate'] * 100:>5.1f}%  "
                f"{r['max']:+7.2f}%  {r['min']:+7.2f}%"
            )
    return "\n".join(lines).lstrip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None,
                        help="output snapshot JSON path "
                             "(default: logs/news_sentiment_overlay_<today>.json)")
    parser.add_argument("--window-days", type=int, default=nso.WINDOW_DAYS,
                        help="days +/- entry_ts to scan news (default 1)")
    parser.add_argument("--no-write", action="store_true",
                        help="don't persist the snapshot — print only")
    args = parser.parse_args()

    conn = db.init_db()
    try:
        report = nso.compute_overlay(conn, window_days=args.window_days)
    finally:
        conn.close()

    report["generated_at"] = date.today().isoformat()
    out_path = Path(args.out) if args.out else default_out_path()

    print(render_table(report["rows"]))
    print()
    n_rows = len(report["rows"])
    log(
        f"news_sentiment_overlay: {n_rows} (strategy, sentiment) buckets "
        f"from {report['n_trades_total']} trades + "
        f"{report['n_news_total']} news rows",
        "SUCCESS" if n_rows else "WARNING",
    )

    if not args.no_write:
        write_report(report, out_path)
        log(f"wrote snapshot → {out_path}", "INFO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
