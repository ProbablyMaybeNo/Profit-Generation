"""
sizing.py — Position-sizing strategies for the auto-trader.

Currently exposes two methods:

  fixed  — the legacy behavior: notional = min(price * floor(max_usd / price),
           max_usd). Returns the same int qty the auto-trader already
           computes via _calc_qty.

  kelly  — Kelly fraction on the strategy's historical edge:
              f* = (bp - q) / b
           where p = win_rate, q = 1 - p, b = avg_win / avg_loss.
           f* is clamped to [0, KELLY_CAP] (default 25%) for safety.
           Notional cap = min(max_position_usd, f* * portfolio_value).

The auto-trader treats both methods as USD notional → integer share
count via _calc_qty, so the chosen sizing is always a whole number
of shares.

Empty / degenerate edge histories (no closed trades, all wins or all
losses, avg_loss=0) return 0 notional under Kelly — caller should
treat 0 as "skip" the same way SKIP_PRICE does today.
"""

from __future__ import annotations

import sqlite3
import statistics
from typing import Dict, Optional


KELLY_CAP = 0.25
SIZING_METHOD_FIXED = "fixed"
SIZING_METHOD_KELLY = "kelly"
SUPPORTED_SIZING_METHODS = {SIZING_METHOD_FIXED, SIZING_METHOD_KELLY}


def fetch_returns(conn: sqlite3.Connection, strategy_id: str) -> list:
    rows = conn.execute(
        "SELECT o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval = '1d' AND s.strategy_id = ?",
        (strategy_id,),
    ).fetchall()
    return [float(r["return_pct"]) for r in rows]


def edge_stats(returns: list) -> Dict:
    """Compute the win_rate, avg_win, avg_loss the Kelly formula needs.

    Empty / all-wins / all-losses returns produce numeric fields where
    the missing side is 0.0. Callers check b > 0 before computing f*.
    """
    n = len(returns)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    return {
        "n": n,
        "win_rate": round(len(wins) / n, 4),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        # avg_loss is stored as a positive magnitude.
        "avg_loss": round(abs(sum(losses) / len(losses)), 4) if losses else 0.0,
    }


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float,
                    *, cap: float = KELLY_CAP) -> float:
    """f* = (b*p - q) / b, clamped to [0, cap].

    Returns 0.0 on degenerate inputs (avg_loss=0, avg_win=0, p<=0, p>=1
    extremes when no data on the other side).
    """
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    p = max(0.0, min(1.0, float(win_rate)))
    q = 1.0 - p
    b = avg_win / avg_loss
    if b <= 0:
        return 0.0
    f = (b * p - q) / b
    if f <= 0:
        return 0.0
    return round(min(f, cap), 4)


def normalize_sizing_method(raw) -> str:
    if not raw:
        return SIZING_METHOD_FIXED
    v = str(raw).lower().strip()
    if v in SUPPORTED_SIZING_METHODS:
        return v
    return SIZING_METHOD_FIXED


def kelly_notional(
    conn: sqlite3.Connection,
    strategy_id: str,
    portfolio_value: Optional[float],
    *,
    max_position_usd: float,
    cap: float = KELLY_CAP,
) -> Dict:
    """Return {notional, fraction, stats} for a Kelly-sized entry.

    notional is the smaller of (max_position_usd, fraction*portfolio_value).
    notional 0 means the caller should skip (no edge, no portfolio, etc.).
    """
    rets = fetch_returns(conn, strategy_id)
    stats = edge_stats(rets)
    f = kelly_fraction(
        stats["win_rate"], stats["avg_win"], stats["avg_loss"], cap=cap,
    )
    if f <= 0 or portfolio_value is None or portfolio_value <= 0:
        return {"notional": 0.0, "fraction": f, "stats": stats}
    target = f * float(portfolio_value)
    notional = min(float(max_position_usd), target)
    return {"notional": round(notional, 2), "fraction": f, "stats": stats}


def compute_notional(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    sizing_method: str,
    portfolio_value: Optional[float],
    max_position_usd: float,
    cap: float = KELLY_CAP,
) -> Dict:
    """Single entry point for the auto-trader.

    Returns {notional, sizing_method, fraction?, stats?}. For "fixed",
    notional is just max_position_usd (the existing behavior); for
    "kelly", it routes through kelly_notional.
    """
    method = normalize_sizing_method(sizing_method)
    if method == SIZING_METHOD_KELLY:
        out = kelly_notional(
            conn, strategy_id, portfolio_value,
            max_position_usd=max_position_usd, cap=cap,
        )
        out["sizing_method"] = method
        return out
    return {
        "notional": float(max_position_usd),
        "sizing_method": SIZING_METHOD_FIXED,
        "fraction": None,
        "stats": None,
    }
