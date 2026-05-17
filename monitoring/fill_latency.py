"""
fill_latency.py — Per-strategy fill-time latency rollup (milestone 3.6.3).

For every filled paper_trades row with non-null submitted_at AND filled_at,
compute the wall-clock delta in seconds. Group by strategy_id and surface:

  median_s    = median latency in seconds
  p95_s       = 95th-percentile latency (long-tail signal)
  n           = sample count
  outliers    = count of rows whose latency exceeds OUTLIER_THRESHOLD_S
                (default 300s = 5min, per spec)

Rows where either timestamp is missing OR filled_at < submitted_at (clock
skew / data error) are excluded.

Sort: strategies with the worst median latency first, then by p95
descending as the tiebreaker.

Spec: dashboard "FILL LATENCY" card shows median fill-time delta per
strategy with outliers (> 5min) flagged.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Dict, List, Optional


OUTLIER_THRESHOLD_S = 5 * 60   # 5 minutes — anything beyond is "outlier"


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse our ISO-ish timestamps. Tolerates missing tz and 'Z' suffix.
    Returns None on any parse failure or empty input."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    n = len(sv)
    mid = n // 2
    if n % 2 == 1:
        return float(sv[mid])
    return float((sv[mid - 1] + sv[mid]) / 2.0)


def _percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile (sufficient for small n — operational stat,
    not a statistical claim)."""
    if not values:
        return 0.0
    sv = sorted(values)
    n = len(sv)
    if n == 1:
        return float(sv[0])
    # Clamp 0 <= pct <= 100
    p = max(0.0, min(100.0, pct))
    idx = int(round((p / 100.0) * (n - 1)))
    return float(sv[idx])


def fetch_latencies(conn: sqlite3.Connection) -> Dict[str, List[float]]:
    """{strategy_id: [latency_seconds, ...]} from every filled paper trade.

    Excludes:
      - rows with NULL submitted_at or NULL filled_at
      - rows whose parsed timestamps yield a negative latency (clock skew)
    """
    rows = conn.execute(
        "SELECT strategy_id, submitted_at, filled_at "
        "  FROM paper_trades "
        " WHERE submitted_at IS NOT NULL AND filled_at IS NOT NULL "
        "   AND strategy_id IS NOT NULL AND strategy_id != '' "
    ).fetchall()
    out: Dict[str, List[float]] = {}
    for r in rows:
        sub = _parse_iso(r["submitted_at"])
        fil = _parse_iso(r["filled_at"])
        if sub is None or fil is None:
            continue
        delta = (fil - sub).total_seconds()
        if delta < 0:
            continue
        out.setdefault(r["strategy_id"], []).append(float(delta))
    return out


def compute_fill_latency(conn: sqlite3.Connection) -> Dict:
    """Per-strategy median + p95 + outlier-count rollup.

    Shape::

      {
        "rows": [
          {"strategy_id": ..., "n": int, "median_s": float, "p95_s": float,
           "outliers": int, "outlier_pct": float},
          ...
        ],
        "n_strategies": int,
        "n_trades_total": int,
        "outlier_threshold_s": 300,
        "overall_median_s": float | None,
      }
    """
    by_strat = fetch_latencies(conn)
    rows: List[Dict] = []
    all_latencies: List[float] = []
    for sid in sorted(by_strat.keys()):
        lats = by_strat[sid]
        if not lats:
            continue
        n_outliers = sum(1 for x in lats if x > OUTLIER_THRESHOLD_S)
        rows.append({
            "strategy_id": sid,
            "n": len(lats),
            "median_s": round(_median(lats), 2),
            "p95_s": round(_percentile(lats, 95.0), 2),
            "outliers": n_outliers,
            "outlier_pct": round(n_outliers / len(lats) * 100.0, 1),
        })
        all_latencies.extend(lats)

    rows.sort(key=lambda r: (-r["median_s"], -r["p95_s"], r["strategy_id"]))

    overall_median = round(_median(all_latencies), 2) if all_latencies else None

    return {
        "rows": rows,
        "n_strategies": len(rows),
        "n_trades_total": len(all_latencies),
        "outlier_threshold_s": OUTLIER_THRESHOLD_S,
        "overall_median_s": overall_median,
    }
