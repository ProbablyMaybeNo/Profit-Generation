"""day_trade_cohort_test — Phase 7.6 multi-strategy day-trading viability test.

Five strategies run in parallel for ~3 weeks of paper trading. The cohort
mix is chosen to separate "framework failure" from "individual strategy
failure":

  - intraday-1m-orb         — 1m reflexive breakout
  - intraday-1m-momentum    — 1m trend continuation
  - intraday-1m-vwap-reclaim — 1m mean reversion
  - intraday-orbo-5m         — 5m sibling of ORB (controls for "1m bars broken")
  - intraday-mr-3bar-low-15m — already-firing 15m baseline ("rest of system fine")

Per-strategy verdict at min_outcomes: Graduate / Park / Kill.
Cohort verdict: framework sound vs. broken, based on how many graduate.

Usage:
    py -3.13 -m monitoring.day_trade_cohort_test           # text report
    py -3.13 -m monitoring.day_trade_cohort_test --json    # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data import db as _db  # noqa: E402


COHORT: List[str] = [
    "intraday-1m-orb",
    "intraday-1m-momentum",
    "intraday-1m-vwap-reclaim",
    "intraday-orbo-5m",
    "intraday-mr-3bar-low-15m",
]

MIN_OUTCOMES = 30
EXPECTANCY_THRESHOLD = 0.0
WIN_RATE_TOLERANCE_PCT = 10.0
MAX_DRAWDOWN_PCT_OF_ALLOCATED = -10.0
ALLOCATED_USD_PER_STRATEGY = 200.0

COHORT_GRADUATE_PARK_THRESHOLD = 3


@dataclass
class StrategyVerdict:
    strategy_id: str
    n_closed: int
    n_open: int
    win_rate_pct: Optional[float]
    mean_return_pct: Optional[float]
    sum_return_pct: Optional[float]
    max_drawdown_pct: Optional[float]
    verdict: str
    reasons: List[str]


def _closed_returns(conn: sqlite3.Connection, strategy_id: str) -> List[float]:
    rows = conn.execute(
        "SELECT o.return_pct FROM outcomes o "
        "  JOIN signals s ON s.id = o.signal_id "
        " WHERE s.strategy_id = ? "
        "   AND o.status = 'closed' "
        "   AND o.return_pct IS NOT NULL "
        "   AND o.entry_ts >= '2026-05-26' "
        " ORDER BY o.entry_ts ASC",
        (strategy_id,),
    ).fetchall()
    return [float(r[0]) for r in rows]


def _open_count(conn: sqlite3.Connection, strategy_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM outcomes o "
        "  JOIN signals s ON s.id = o.signal_id "
        " WHERE s.strategy_id = ? AND o.status = 'open' "
        "   AND o.entry_ts >= '2026-05-26'",
        (strategy_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _max_drawdown_pct(returns: List[float]) -> Optional[float]:
    if not returns:
        return None
    equity = 100.0
    peak = equity
    max_dd_pct = 0.0
    for r in returns:
        equity *= 1.0 + (r / 100.0)
        if equity > peak:
            peak = equity
        dd_pct = ((equity - peak) / peak) * 100.0
        if dd_pct < max_dd_pct:
            max_dd_pct = dd_pct
    return max_dd_pct


def evaluate_strategy(
    conn: sqlite3.Connection,
    strategy_id: str,
) -> StrategyVerdict:
    returns = _closed_returns(conn, strategy_id)
    n_closed = len(returns)
    n_open = _open_count(conn, strategy_id)

    if n_closed == 0:
        return StrategyVerdict(
            strategy_id=strategy_id,
            n_closed=0,
            n_open=n_open,
            win_rate_pct=None,
            mean_return_pct=None,
            sum_return_pct=None,
            max_drawdown_pct=None,
            verdict="no_data",
            reasons=["zero closed outcomes since cohort test start (2026-05-26)"],
        )

    wins = sum(1 for r in returns if r > 0)
    win_rate_pct = (wins / n_closed) * 100.0
    mean_ret = sum(returns) / n_closed
    sum_ret = sum(returns)
    max_dd = _max_drawdown_pct(returns)

    reasons: List[str] = []

    if n_closed < MIN_OUTCOMES:
        reasons.append(
            f"sample too small (n={n_closed} < {MIN_OUTCOMES}) — verdict deferred"
        )
        verdict = "park"
    elif mean_ret <= EXPECTANCY_THRESHOLD:
        reasons.append(
            f"negative expectancy (mean={mean_ret:.3f}% ≤ {EXPECTANCY_THRESHOLD}%)"
        )
        verdict = "kill"
    elif max_dd is not None and max_dd < MAX_DRAWDOWN_PCT_OF_ALLOCATED:
        reasons.append(
            f"drawdown exceeded ({max_dd:.2f}% < {MAX_DRAWDOWN_PCT_OF_ALLOCATED}%)"
        )
        verdict = "kill"
    else:
        reasons.append(
            f"positive expectancy ({mean_ret:.3f}% over n={n_closed}), "
            f"max_dd {max_dd:.2f}%, win_rate {win_rate_pct:.1f}%"
        )
        verdict = "graduate"

    return StrategyVerdict(
        strategy_id=strategy_id,
        n_closed=n_closed,
        n_open=n_open,
        win_rate_pct=round(win_rate_pct, 1),
        mean_return_pct=round(mean_ret, 3),
        sum_return_pct=round(sum_ret, 3),
        max_drawdown_pct=round(max_dd, 2) if max_dd is not None else None,
        verdict=verdict,
        reasons=reasons,
    )


def cohort_verdict(per_strategy: List[StrategyVerdict]) -> Dict[str, object]:
    decided = [v for v in per_strategy if v.verdict in ("graduate", "park", "kill")]
    graduate_or_park = [v for v in decided if v.verdict in ("graduate", "park")]
    n_grad_park = len(graduate_or_park)

    if len(decided) < len(COHORT):
        framework_call = "pending"
        rationale = (
            f"{len(COHORT) - len(decided)} strategies still in no_data state — "
            "cohort decision deferred until more closed outcomes accumulate"
        )
    elif n_grad_park >= COHORT_GRADUATE_PARK_THRESHOLD:
        framework_call = "framework_sound"
        rationale = (
            f"{n_grad_park}/{len(COHORT)} strategies graduated or parked — "
            "day trading is viable for our system"
        )
    elif n_grad_park <= 1:
        framework_call = "framework_broken"
        rationale = (
            f"only {n_grad_park}/{len(COHORT)} strategies survived — "
            "framework likely broken, redesign before retrying"
        )
    else:
        framework_call = "mixed"
        rationale = (
            f"{n_grad_park}/{len(COHORT)} survived — strategy-specific noise, "
            "keep the survivors and iterate"
        )

    return {
        "framework_call": framework_call,
        "rationale": rationale,
        "n_graduate_or_park": n_grad_park,
        "n_cohort": len(COHORT),
    }


def build_report(conn: sqlite3.Connection) -> Dict[str, object]:
    per_strategy = [evaluate_strategy(conn, sid) for sid in COHORT]
    verdict = cohort_verdict(per_strategy)
    return {
        "cohort": COHORT,
        "thresholds": {
            "min_outcomes": MIN_OUTCOMES,
            "expectancy_threshold_pct": EXPECTANCY_THRESHOLD,
            "win_rate_tolerance_pct": WIN_RATE_TOLERANCE_PCT,
            "max_drawdown_pct_of_allocated": MAX_DRAWDOWN_PCT_OF_ALLOCATED,
            "allocated_usd_per_strategy": ALLOCATED_USD_PER_STRATEGY,
            "cohort_graduate_park_threshold": COHORT_GRADUATE_PARK_THRESHOLD,
        },
        "per_strategy": [asdict(v) for v in per_strategy],
        "cohort_verdict": verdict,
    }


def format_text(report: Dict[str, object]) -> str:
    out: List[str] = []
    out.append("=" * 72)
    out.append("Day-trade cohort test — Phase 7.6")
    out.append("=" * 72)
    t = report["thresholds"]
    out.append(
        f"Thresholds: n≥{t['min_outcomes']}, "
        f"expectancy>{t['expectancy_threshold_pct']}%, "
        f"max_dd>{t['max_drawdown_pct_of_allocated']}%, "
        f"${t['allocated_usd_per_strategy']:.0f}/strategy"
    )
    out.append("")
    for s in report["per_strategy"]:
        out.append(f"  {s['strategy_id']:<32} [{s['verdict']:<10}]")
        if s["n_closed"] > 0:
            out.append(
                f"    n_closed={s['n_closed']:<3} n_open={s['n_open']:<3} "
                f"win={s['win_rate_pct']}%  "
                f"mean={s['mean_return_pct']}%  "
                f"sum={s['sum_return_pct']}%  "
                f"max_dd={s['max_drawdown_pct']}%"
            )
        else:
            out.append(f"    n_closed=0 n_open={s['n_open']}")
        for reason in s["reasons"]:
            out.append(f"    -> {reason}")
        out.append("")
    cv = report["cohort_verdict"]
    out.append("-" * 72)
    out.append(f"COHORT VERDICT: {cv['framework_call'].upper()}")
    out.append(f"  {cv['rationale']}")
    out.append("=" * 72)
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    conn = _db.init_db()
    try:
        report = build_report(conn)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
