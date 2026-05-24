"""
llm_filter_ab.py — 7.1.2 LLM filter A/B aggregation.

Mirrors the SAR overlay A/B shape (6.4.2 aggregator) and the Kelly
dashboard card pattern (6.2.3). Joins paper_trades_llm_filter shadow
rows back to the live outcomes (via signals) to answer the question:
**what would PnL have looked like if every "skip" verdict had been
honored?**

Verdict semantics in the A/B computation:
  - allow     → take the trade. Real PnL counts as both shadow and live.
  - skip      → shadow PnL is 0; live PnL is the realized return.
  - downsize  → shadow PnL is real_pnl × 0.5 (the same haircut 7.1.3
                will apply when the filter graduates).

Sample-size gate: until at least 50 closed outcomes pair to a verdict,
the aggregator returns ``verdict_available=False`` so the dashboard
card can render an "insufficient sample" placeholder.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional


DEFAULT_MIN_SAMPLE = 50
DOWNSIZE_FACTOR = 0.5


def _row_to_dict(row) -> Dict[str, Any]:
    return dict(row)


def fetch_paired_outcomes(
    conn: sqlite3.Connection,
    *,
    strategy_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return shadow rows joined to their realized outcome.

    Joins paper_trades_llm_filter → signals → outcomes by
    (strategy_id, symbol, bar_ts, signal_type). Only includes rows with
    a closed outcome (return_pct is not null).
    """
    where = ["o.status = 'closed'", "o.return_pct IS NOT NULL"]
    params: List[Any] = []
    if strategy_id is not None:
        where.append("f.strategy_id = ?")
        params.append(strategy_id)
    sql = (
        "SELECT f.strategy_id, f.symbol, f.bar_ts, f.signal_type, "
        "       f.verdict, f.confidence, f.factors_json, f.failure_mode, "
        "       o.return_pct "
        "  FROM paper_trades_llm_filter f "
        "  JOIN signals s "
        "    ON s.strategy_id = f.strategy_id "
        "   AND s.symbol      = f.symbol "
        "   AND s.bar_ts      = f.bar_ts "
        "   AND s.signal_type = f.signal_type "
        "  JOIN outcomes o ON o.signal_id = s.id "
        " WHERE " + " AND ".join(where)
    )
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def aggregate(
    rows: List[Dict[str, Any]],
    *,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> Dict[str, Any]:
    """Aggregate paired shadow + outcome rows into the A/B verdict shape.

    Returns:
      {
        n: int,                    # paired sample size
        verdict_available: bool,   # True when n >= min_sample
        live_total_pct: float,     # sum of real return_pct
        shadow_total_pct: float,   # what the filter would have netted
        delta_pct: float,          # shadow - live
        verdict_counts: {allow, skip, downsize, fail_open},
        live_win_rate: float,
        shadow_win_rate: float,    # fraction of allow/downsize positive
        sharpe_delta: float | None,
      }
    """
    n = len(rows)
    counts = {"allow": 0, "skip": 0, "downsize": 0, "fail_open": 0}
    live_returns: List[float] = []
    shadow_returns: List[float] = []
    live_wins = 0
    shadow_wins = 0
    shadow_taken_n = 0  # rows the filter would have actually taken
    for r in rows:
        verdict = (r.get("verdict") or "").lower()
        ret = float(r.get("return_pct") or 0.0)
        if r.get("failure_mode"):
            counts["fail_open"] += 1
        if verdict in counts:
            counts[verdict] += 1
        # Live always realizes the full return.
        live_returns.append(ret)
        if ret > 0:
            live_wins += 1
        # Shadow scenario.
        if verdict == "skip":
            shadow_returns.append(0.0)
        elif verdict == "downsize":
            scaled = ret * DOWNSIZE_FACTOR
            shadow_returns.append(scaled)
            shadow_taken_n += 1
            if scaled > 0:
                shadow_wins += 1
        else:  # allow / unknown → take full
            shadow_returns.append(ret)
            shadow_taken_n += 1
            if ret > 0:
                shadow_wins += 1
    live_total = sum(live_returns)
    shadow_total = sum(shadow_returns)
    live_wr = (live_wins / n) if n > 0 else 0.0
    shadow_wr = (shadow_wins / shadow_taken_n) if shadow_taken_n > 0 else 0.0
    sharpe_delta = _sharpe_delta(live_returns, shadow_returns)
    return {
        "n": n,
        "verdict_available": n >= min_sample,
        "live_total_pct": round(live_total, 4),
        "shadow_total_pct": round(shadow_total, 4),
        "delta_pct": round(shadow_total - live_total, 4),
        "verdict_counts": counts,
        "live_win_rate": round(live_wr, 4),
        "shadow_win_rate": round(shadow_wr, 4),
        "shadow_taken_n": shadow_taken_n,
        "sharpe_delta": sharpe_delta,
    }


def _sharpe_delta(
    live_returns: List[float], shadow_returns: List[float],
) -> Optional[float]:
    """Compute the Sharpe of shadow minus Sharpe of live. None when
    either side has < 2 samples or a zero standard deviation."""
    if len(live_returns) < 2 or len(shadow_returns) < 2:
        return None
    live_sharpe = _sharpe(live_returns)
    shadow_sharpe = _sharpe(shadow_returns)
    if live_sharpe is None or shadow_sharpe is None:
        return None
    return round(shadow_sharpe - live_sharpe, 4)


def _sharpe(returns: List[float]) -> Optional[float]:
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        return None
    sd = var ** 0.5
    return mean / sd


def summary(
    conn: sqlite3.Connection,
    *,
    strategy_id: Optional[str] = None,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> Dict[str, Any]:
    """Top-level entry point. Returns the aggregate verdict for one
    strategy or the global aggregate when ``strategy_id`` is None."""
    rows = fetch_paired_outcomes(conn, strategy_id=strategy_id)
    agg = aggregate(rows, min_sample=min_sample)
    agg["strategy_id"] = strategy_id
    return agg


def summary_by_strategy(
    conn: sqlite3.Connection,
    *,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> List[Dict[str, Any]]:
    """Per-strategy aggregate verdicts. Strategies with zero paired rows
    are omitted (no card to render)."""
    rows = fetch_paired_outcomes(conn)
    by_sid: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_sid.setdefault(r["strategy_id"], []).append(r)
    out: List[Dict[str, Any]] = []
    for sid, sub in by_sid.items():
        agg = aggregate(sub, min_sample=min_sample)
        agg["strategy_id"] = sid
        out.append(agg)
    out.sort(key=lambda r: (-r.get("delta_pct", 0.0), r["strategy_id"]))
    return out
