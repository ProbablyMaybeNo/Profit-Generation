"""
strategy_forecast.py — Calibrated forward expectations per strategy.

Based on a strategy's actual fire history + closed outcomes, projects:
  - expected fires per month
  - median return per closed trade
  - mean return per closed trade

Used for the strategy edge card: "expected: ~12 fires/month, median
+0.5%/trade". Lets the user set realistic expectations instead of
chasing the highest-Sharpe number.

Fire frequency uses long_entry signals across the strategy's history;
the observation window is the span between the first and last signal
(or the lookback floor — whichever is shorter). Returns use closed
outcomes only.

Confidence is a coarse rollup:
  - high   : >= 30 closed trades AND >= 90 observation days
  - medium : >= 10 closed trades AND >= 30 observation days
  - low    : everything else (still emits numbers — caller decides)
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime
from typing import Dict, Optional


DAYS_PER_MONTH = 365.25 / 12.0  # ≈ 30.44
DEFAULT_FALLBACK_DAYS = 30  # used when only one signal is observed

CONFIDENCE_HIGH_TRADES = 30
CONFIDENCE_HIGH_DAYS = 90
CONFIDENCE_MED_TRADES = 10
CONFIDENCE_MED_DAYS = 30


def _parse_iso(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _confidence(n_trades: int, observation_days: int) -> str:
    if n_trades >= CONFIDENCE_HIGH_TRADES and observation_days >= CONFIDENCE_HIGH_DAYS:
        return "high"
    if n_trades >= CONFIDENCE_MED_TRADES and observation_days >= CONFIDENCE_MED_DAYS:
        return "medium"
    return "low"


def _fmt_pct(value: float) -> str:
    sign = "+" if value > 0 else ("" if value == 0 else "")
    return f"{sign}{value:.2f}%"


def _build_summary(
    fires_per_month: Optional[float],
    median_return_pct: Optional[float],
) -> str:
    """Render the spec's exact phrasing — empty if data is too thin."""
    if fires_per_month is None and median_return_pct is None:
        return "(no historical fires)"
    bits = []
    if fires_per_month is not None:
        if fires_per_month >= 1:
            bits.append(f"~{fires_per_month:.0f} fires/month")
        else:
            bits.append(f"~{fires_per_month:.2f} fires/month")
    if median_return_pct is not None:
        bits.append(f"median {_fmt_pct(median_return_pct)}/trade")
    return "expected: " + ", ".join(bits)


def fetch_signal_dates(conn: sqlite3.Connection, strategy_id: str) -> list:
    """All long_entry signal bar dates for the strategy, deduped + sorted."""
    rows = conn.execute(
        "SELECT DISTINCT bar_ts FROM signals "
        " WHERE strategy_id = ? AND signal_type = 'long_entry' "
        "   AND bar_interval = '1d' "
        " ORDER BY bar_ts ASC",
        (strategy_id,),
    ).fetchall()
    dates: list = []
    for r in rows:
        d = _parse_iso(r["bar_ts"])
        if d is not None:
            dates.append(d)
    return dates


def fetch_closed_returns(conn: sqlite3.Connection, strategy_id: str) -> list:
    rows = conn.execute(
        "SELECT o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' AND o.return_pct IS NOT NULL "
        "   AND s.strategy_id = ? AND s.bar_interval = '1d'",
        (strategy_id,),
    ).fetchall()
    return [float(r["return_pct"]) for r in rows]


def compute_forecast(conn: sqlite3.Connection, strategy_id: str) -> Dict:
    """Per-strategy forecast rollup served by the API + state expansion."""
    signal_dates = fetch_signal_dates(conn, strategy_id)
    returns = fetch_closed_returns(conn, strategy_id)

    n_signals = len(signal_dates)
    n_trades = len(returns)

    fires_per_month: Optional[float] = None
    observation_days = 0
    first_iso = None
    last_iso = None

    if signal_dates:
        first = signal_dates[0]
        last = signal_dates[-1]
        first_iso = first.isoformat()
        last_iso = last.isoformat()
        span = (last - first).days
        if span == 0:
            # Single signal — assume a one-month observation floor so the
            # frequency isn't infinite.
            observation_days = DEFAULT_FALLBACK_DAYS
        else:
            observation_days = span
        if observation_days > 0:
            fires_per_month = round(
                n_signals / observation_days * DAYS_PER_MONTH, 3,
            )

    median_return_pct: Optional[float] = None
    mean_return_pct: Optional[float] = None
    win_rate: Optional[float] = None
    if returns:
        median_return_pct = round(statistics.median(returns), 4)
        mean_return_pct = round(sum(returns) / len(returns), 4)
        win_rate = round(sum(1 for r in returns if r > 0) / len(returns), 4)

    confidence = _confidence(n_trades, observation_days)
    summary = _build_summary(fires_per_month, median_return_pct)

    return {
        "strategy_id": strategy_id,
        "n_signals_observed": n_signals,
        "n_trades": n_trades,
        "first_signal_iso": first_iso,
        "last_signal_iso": last_iso,
        "observation_days": observation_days,
        "fires_per_month": fires_per_month,
        "median_return_pct": median_return_pct,
        "mean_return_pct": mean_return_pct,
        "win_rate": win_rate,
        "confidence": confidence,
        "summary": summary,
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }
