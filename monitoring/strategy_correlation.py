"""
strategy_correlation.py — Pairwise daily-P&L correlation across active
strategies.

For each strategy with closed 1d outcomes, builds a daily P&L series
keyed on exit_ts date (sum of return_pct on each calendar day a trade
exits). Strategies are aligned on a shared date index using the union
of all exit dates; missing dates are filled with 0.0 (no P&L that day).
Pairwise Pearson correlation is computed between every strategy pair.

The dashboard renders the result as an inline SVG heatmap. Cells with
|corr| > the redundancy threshold (default 0.70) are flagged as
likely-redundant pairs.

Empty / degenerate cases:
  - 0 strategies → empty matrix
  - 1 strategy → 1x1 matrix with corr = 1.0
  - any strategy with a degenerate (zero-variance) series → corr = 0.0
    against every other strategy (cosine math undefined)
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


REDUNDANCY_THRESHOLD = 0.70


def fetch_pl_by_strategy_and_date(conn: sqlite3.Connection) -> Dict[str, Dict[str, float]]:
    """Return {strategy_id: {YYYY-MM-DD: total_return_pct_that_day}}.

    Reads closed 1d outcomes from trading.db. Multiple trades on the same
    exit date for the same strategy are summed.
    """
    rows = conn.execute(
        "SELECT s.strategy_id, o.exit_ts, o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval = '1d'"
    ).fetchall()
    out: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        sid = r["strategy_id"]
        exit_d = (r["exit_ts"] or "")[:10]
        if not exit_d:
            continue
        out[sid][exit_d] += float(r["return_pct"])
    return {sid: dict(d) for sid, d in out.items()}


def aligned_series(
    pl_by_strat: Dict[str, Dict[str, float]],
) -> Tuple[List[str], List[str], Dict[str, List[float]]]:
    """Return (strategies_sorted, dates_sorted, {strategy: [pl per date]}).

    Missing dates for a strategy are filled with 0.0.
    """
    strategies = sorted(pl_by_strat.keys())
    all_dates = set()
    for d in pl_by_strat.values():
        all_dates.update(d.keys())
    dates = sorted(all_dates)
    series: Dict[str, List[float]] = {}
    for sid in strategies:
        per_date = pl_by_strat[sid]
        series[sid] = [per_date.get(d, 0.0) for d in dates]
    return strategies, dates, series


def pearson(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson r over equal-length sequences. Returns 0.0 on degenerate input."""
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = 0.0
    var_a = 0.0
    var_b = 0.0
    for x, y in zip(a, b):
        dx = x - mean_a
        dy = y - mean_b
        num += dx * dy
        var_a += dx * dx
        var_b += dy * dy
    denom = math.sqrt(var_a) * math.sqrt(var_b)
    if denom == 0.0:
        return 0.0
    return num / denom


def build_correlation_matrix(
    pl_by_strat: Dict[str, Dict[str, float]],
) -> Dict:
    """Return the full rollup the dashboard renders."""
    strategies, dates, series = aligned_series(pl_by_strat)
    n = len(strategies)
    matrix: List[List[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[i][j] = 1.0
            elif j < i:
                matrix[i][j] = matrix[j][i]  # symmetric
            else:
                r = pearson(series[strategies[i]], series[strategies[j]])
                # NaN guard.
                if r != r:  # noqa: PLR0124  (NaN check)
                    r = 0.0
                matrix[i][j] = round(r, 4)
    redundant_pairs: List[Tuple[str, str, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            if abs(matrix[i][j]) >= REDUNDANCY_THRESHOLD:
                redundant_pairs.append(
                    (strategies[i], strategies[j], matrix[i][j])
                )
    return {
        "strategies": strategies,
        "matrix": matrix,
        "n_strategies": n,
        "n_dates": len(dates),
        "redundancy_threshold": REDUNDANCY_THRESHOLD,
        "redundant_pairs": [
            {"a": a, "b": b, "corr": c} for (a, b, c) in redundant_pairs
        ],
    }


def compute_strategy_correlation(conn: sqlite3.Connection) -> Dict:
    pl = fetch_pl_by_strategy_and_date(conn)
    return build_correlation_matrix(pl)
