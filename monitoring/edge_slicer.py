"""
edge_slicer.py — Slice closed outcomes by conditioning variables to
surface "this strategy only works on Mondays" type insights.

For each active strategy, this module pulls all closed-1d outcomes
from trading.db, joins each trade to:
  - the day-of-week of its entry_ts
  - the daily_reports.market_regime tag on the entry date
  - (where VIX is available) the VIX quartile on the entry date
... then aggregates (n, mean_ret, sharpe-ish) per (strategy, slice).

The result is a flat list of slice rows the dashboard renders as a
table. The empty case (zero closed trades, or zero matches for a
slice) returns an empty list — never raises.

VIX data is optional. When the `macro` table (planned in milestone
2.5.1) has VIX rows, the vix-quartile slice computes against them.
Until then, VIX slicing returns an empty list and emits a
`vix_unavailable=True` field in the summary so the dashboard can
show "waiting on macro overlay".
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_iso_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _safe_stats(returns: Sequence[float]) -> Dict:
    """Mirror validate_strategy._stats's shape, but tolerant of empty input."""
    rets = list(returns)
    n = len(rets)
    if n == 0:
        return {"n": 0, "mean": 0.0, "sharpe_ish": 0.0,
                "win_rate": 0.0, "median": 0.0, "stdev": 0.0,
                "min": 0.0, "max": 0.0}
    mean = sum(rets) / n
    sd = statistics.stdev(rets) if n > 1 else 0.0
    sharpe = (mean / sd) if sd > 0 else 0.0
    wr = sum(1 for r in rets if r > 0) / n
    return {
        "n": n,
        "mean": round(mean, 4),
        "sharpe_ish": round(sharpe, 4),
        "win_rate": round(wr, 4),
        "median": round(statistics.median(rets), 4),
        "stdev": round(sd, 4),
        "min": round(min(rets), 4),
        "max": round(max(rets), 4),
    }


# ---------------------------------------------------------------------------
# Source-of-truth pulls
# ---------------------------------------------------------------------------

def fetch_closed_outcomes(conn: sqlite3.Connection) -> List[Dict]:
    """Closed 1d outcomes joined with the strategy_id for slicing.

    Each row: {strategy_id, entry_ts, exit_ts, return_pct}
    """
    rows = conn.execute(
        "SELECT s.strategy_id, o.entry_ts, o.exit_ts, o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval = '1d'"
    ).fetchall()
    return [
        {"strategy_id": r["strategy_id"], "entry_ts": r["entry_ts"],
         "exit_ts": r["exit_ts"], "return_pct": float(r["return_pct"])}
        for r in rows
    ]


def fetch_regime_by_date(conn: sqlite3.Connection) -> Dict[str, str]:
    """Return {YYYY-MM-DD: market_regime} from daily_reports."""
    rows = conn.execute(
        "SELECT report_date, market_regime FROM daily_reports "
        " WHERE market_regime IS NOT NULL AND market_regime != ''"
    ).fetchall()
    return {r["report_date"]: r["market_regime"] for r in rows}


def fetch_vix_by_date(conn: sqlite3.Connection) -> Dict[str, float]:
    """Return {YYYY-MM-DD: vix_close} from the macro table.

    The macro table is introduced in milestone 2.5.1. If it doesn't
    exist yet, return an empty dict — slicing degrades gracefully.
    """
    try:
        rows = conn.execute(
            "SELECT bar_date, value FROM macro "
            " WHERE series_id IN ('VIXCLS', 'VIX') AND value IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["bar_date"]: float(r["value"]) for r in rows}


# ---------------------------------------------------------------------------
# Quartile labelling
# ---------------------------------------------------------------------------

def quartile_thresholds(values: Sequence[float]) -> Tuple[float, float, float]:
    """Return (q1, q2, q3) cut points. Empty input → all zeros."""
    if not values:
        return (0.0, 0.0, 0.0)
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    def pick(p):
        # Linear-interp percentile, clamped within range.
        if n == 1:
            return sorted_vals[0]
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac
    return (pick(0.25), pick(0.5), pick(0.75))


def vix_quartile_label(value: float, thresholds: Tuple[float, float, float]) -> str:
    q1, q2, q3 = thresholds
    if value <= q1:
        return "Q1 (low vol)"
    if value <= q2:
        return "Q2"
    if value <= q3:
        return "Q3"
    return "Q4 (high vol)"


# ---------------------------------------------------------------------------
# Slicing primitives
# ---------------------------------------------------------------------------

def slice_by_dow(trades: Iterable[Dict]) -> List[Dict]:
    """Slice trades by entry-day weekday. Returns one row per (strategy, dow)
    with stats. Skips rows whose entry_ts can't be parsed."""
    buckets: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for t in trades:
        d = _parse_iso_date(t.get("entry_ts"))
        if d is None:
            continue
        sid = t["strategy_id"]
        label = DOW_LABELS[d.weekday()] if d.weekday() < 7 else "?"
        buckets[(sid, label)].append(t["return_pct"])
    out: List[Dict] = []
    for (sid, label), rets in buckets.items():
        stats = _safe_stats(rets)
        out.append({"strategy_id": sid, "slice": label, **stats})
    # Sort: by strategy, then weekday order, then label.
    dow_order = {l: i for i, l in enumerate(DOW_LABELS)}
    out.sort(key=lambda r: (r["strategy_id"], dow_order.get(r["slice"], 99)))
    return out


def slice_by_regime(
    trades: Iterable[Dict], regime_by_date: Dict[str, str],
) -> List[Dict]:
    """Slice trades by daily_reports.market_regime on the entry date."""
    buckets: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for t in trades:
        d = _parse_iso_date(t.get("entry_ts"))
        if d is None:
            continue
        regime = regime_by_date.get(d.isoformat())
        if not regime:
            regime = "(unknown)"
        sid = t["strategy_id"]
        buckets[(sid, regime)].append(t["return_pct"])
    out: List[Dict] = []
    for (sid, regime), rets in buckets.items():
        stats = _safe_stats(rets)
        out.append({"strategy_id": sid, "slice": regime, **stats})
    out.sort(key=lambda r: (r["strategy_id"], r["slice"]))
    return out


def slice_by_vix(
    trades: Iterable[Dict], vix_by_date: Dict[str, float],
) -> List[Dict]:
    """Slice trades by VIX quartile on the entry date.

    Returns [] if no VIX data is available (caller surfaces this).
    """
    if not vix_by_date:
        return []
    thresholds = quartile_thresholds(list(vix_by_date.values()))
    buckets: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for t in trades:
        d = _parse_iso_date(t.get("entry_ts"))
        if d is None:
            continue
        vix = vix_by_date.get(d.isoformat())
        if vix is None:
            label = "(no vix)"
        else:
            label = vix_quartile_label(vix, thresholds)
        sid = t["strategy_id"]
        buckets[(sid, label)].append(t["return_pct"])
    out: List[Dict] = []
    for (sid, label), rets in buckets.items():
        stats = _safe_stats(rets)
        out.append({"strategy_id": sid, "slice": label, **stats})
    quartile_order = {"Q1 (low vol)": 0, "Q2": 1, "Q3": 2, "Q4 (high vol)": 3,
                       "(no vix)": 4}
    out.sort(key=lambda r: (r["strategy_id"], quartile_order.get(r["slice"], 9)))
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def compute_edge_slices(conn: sqlite3.Connection) -> Dict:
    """Return the full slice rollup the dashboard endpoint serves."""
    trades = fetch_closed_outcomes(conn)
    regime_by_date = fetch_regime_by_date(conn)
    vix_by_date = fetch_vix_by_date(conn)

    return {
        "by_dow": slice_by_dow(trades),
        "by_regime": slice_by_regime(trades, regime_by_date),
        "by_vix": slice_by_vix(trades, vix_by_date),
        "n_trades_total": len(trades),
        "vix_unavailable": not bool(vix_by_date),
        "regime_unavailable": not bool(regime_by_date),
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }
