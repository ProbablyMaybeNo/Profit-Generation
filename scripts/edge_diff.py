"""
edge_diff.py — Realized-vs-theoretical edge analysis for paper-traded
strategies.

For each strategy with at least one closed paper-trade pair, compares the
backtest-expected mean return per signal (weighted across instruments
from records.jsonl test_runs) to the actual mean fill-to-fill return of
paper trades. Surfaces the slippage gap so we can see how much of the
modelled edge is being eaten by live execution.

Writes a snapshot to logs/edge_diff_YYYY-MM-DD.json on every run.
Prints a per-strategy table to stdout. Idempotent — re-running on the
same day overwrites the snapshot.

CLI:
  py -3.13 scripts/edge_diff.py
  py -3.13 scripts/edge_diff.py --out logs/edge_diff_custom.json
  py -3.13 scripts/edge_diff.py --no-write
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402
from monitoring import edge_diff as ed  # noqa: E402

DEFAULT_LOG_DIR = ROOT / "logs"


def default_out_path() -> Path:
    return DEFAULT_LOG_DIR / f"edge_diff_{date.today().isoformat()}.json"


def write_report(report: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def render_table(rows: list) -> str:
    if not rows:
        return "(no strategies with paper trades + backtest baseline)"
    header = (
        f"{'strategy':<34} {'status':<22} {'theo':>8}  {'real':>8}  "
        f"{'slip':>7}  {'cap%':>6}  {'n':>4}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        theo = r["theoretical_per_signal_pct"]
        real = r["realized"]["mean_pct"] if r["realized"]["n"] else None
        slip = r["slippage_pct"]
        cap = r["capture_ratio_pct"]
        lines.append(
            f"{r['strategy_id']:<34} {r['status']:<22} "
            f"{(f'{theo:+.2f}%' if theo is not None else '—'):>8}  "
            f"{(f'{real:+.2f}%' if real is not None else '—'):>8}  "
            f"{(f'{slip:+.2f}' if slip is not None else '—'):>7}  "
            f"{(f'{cap:.0f}' if cap is not None else '—'):>6}  "
            f"{r['realized']['n']:>4}"
        )
        if r["narrative"]:
            lines.append(f"    └─ {r['narrative']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=None,
        help="output path for the snapshot JSON "
             "(default: logs/edge_diff_<today>.json)",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="don't persist the snapshot — just print to stdout",
    )
    args = parser.parse_args()

    conn = db.init_db()
    try:
        report = ed.compute_edge_diff(conn)
    finally:
        conn.close()

    report["generated_at"] = date.today().isoformat()
    out_path = Path(args.out) if args.out else default_out_path()

    print(render_table(report["rows"]))
    print()
    log(
        f"edge_diff: {report['n_rows']} strategies analysed "
        f"({report['n_ok']} ok)",
        "SUCCESS" if report["n_ok"] else "WARNING",
    )

    if not args.no_write:
        write_report(report, out_path)
        log(f"wrote snapshot → {out_path}", "INFO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
