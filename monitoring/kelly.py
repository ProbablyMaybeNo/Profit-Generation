"""
kelly.py — Phase 6.2.1 per-strategy Kelly fraction calculator.

The canonical entry point for Phase 6.2 fractional-Kelly sizing. Reads
the strategy's closed paper-traded outcomes from the `outcomes` table
(joined to `signals` for strategy_id), then computes the Kelly fraction
that maximizes long-run logarithmic growth.

Formula:
    p = wins / total                  # win rate
    b = mean_winner / abs(mean_loser) # payoff ratio (>0 when there's edge)
    f* = (p × (b + 1) - 1) / b        # full Kelly fraction

Phase 6 wraps `f*` with three safety rails:

  1. Minimum sample-size guard (default 50). Below that, return None —
     the caller must fall back to whatever sizing tier they were using
     before. Kelly on tiny samples is dangerous because b is unstable.

  2. Negative-edge handling. If `f* <= 0` the strategy doesn't have an
     edge — return 0 rather than negative-sizing (we don't size shorts
     based on a long strategy's negative Kelly).

  3. Hard cap at 0.25 (a quarter of the portfolio). No single strategy
     ever sizes above this even if the math says it should. This is the
     "full Kelly" cap; the fractional-Kelly fraction (1/4 etc.) is
     applied separately in 6.2.2's sizing tier.

The numbers themselves are NOT fractional-Kelly here — this returns
the raw, capped Kelly fraction. 6.2.2 multiplies by 0.25 / 0.50 to get
the actual sizing fraction.
"""
from __future__ import annotations

import sqlite3
from typing import Dict, Optional


DEFAULT_KELLY_MIN_SAMPLES = 50
KELLY_CAP = 0.25  # mirror of sizing.KELLY_CAP — kept here so callers
                  # can import from one place without crossing modules.


def fetch_closed_outcomes(
    conn: sqlite3.Connection,
    strategy_id: str,
) -> list:
    """Return the closed paper-traded outcomes' return_pct list for the
    given strategy.

    Joins outcomes → signals so we filter on strategy_id (which lives
    on the signal row, not the outcome row). bar_interval is left
    unrestricted so intraday and EOD strategies both qualify.
    """
    rows = conn.execute(
        "SELECT o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' AND o.return_pct IS NOT NULL "
        "   AND s.strategy_id = ?",
        (strategy_id,),
    ).fetchall()
    return [float(r["return_pct"]) for r in rows]


def kelly_stats(returns: list) -> Dict:
    """Compute the win-rate and payoff stats Kelly needs.

    Shape:
      {n, wins, losses, win_rate, mean_winner, mean_loser, b}

    `mean_loser` is stored as a positive magnitude. `b` is the payoff
    ratio, or 0.0 when either side is empty (degenerate edge — no
    Kelly fraction is computable).
    """
    n = len(returns)
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    n_wins = len(wins)
    n_losses = len(losses)
    mean_winner = (sum(wins) / n_wins) if n_wins else 0.0
    mean_loser = (abs(sum(losses) / n_losses)) if n_losses else 0.0
    b = (mean_winner / mean_loser) if mean_loser > 0 else 0.0
    return {
        "n": n,
        "wins": n_wins,
        "losses": n_losses,
        "win_rate": round(n_wins / n, 4) if n else 0.0,
        "mean_winner": round(mean_winner, 4),
        "mean_loser": round(mean_loser, 4),
        "b": round(b, 4),
    }


def profit_factor(returns: list) -> Optional[float]:
    """Gross profit / gross loss over the returns. None when there are no
    losses (undefined / infinite PF) or no returns at all."""
    if not returns:
        return None
    gross_profit = sum(r for r in returns if r > 0)
    gross_loss = abs(sum(r for r in returns if r < 0))
    if gross_loss <= 0:
        return None
    return round(gross_profit / gross_loss, 4)


def _kelly_raw(p: float, b: float) -> float:
    """The textbook Kelly formula: f* = (p × (b + 1) - 1) / b.

    Equivalent to (bp - q) / b where q = 1 - p. Returns 0.0 when b <= 0
    (degenerate input — caller must guard before calling)."""
    if b <= 0:
        return 0.0
    return (p * (b + 1) - 1) / b


def calc_kelly_fraction(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    min_samples: int = DEFAULT_KELLY_MIN_SAMPLES,
    cap: float = KELLY_CAP,
) -> Optional[float]:
    """Phase 6.2.1 — capped Kelly fraction for the strategy, or None.

    Returns:
      None  — fewer than `min_samples` closed outcomes (sample-size guard)
      0.0   — Kelly is negative (no edge) OR degenerate b (one-sided
              outcomes — caller must wait for both sides to populate)
      f∈(0, cap] — the strategy's Kelly fraction, clamped to `cap`
                   (default 0.25 — no strategy claims > a quarter of
                   the portfolio under full Kelly).

    The Phase 6.2.2 fractional-Kelly sizing tier multiplies this by
    0.25 (¼ Kelly) before using as a portfolio fraction. Ross can
    promote a strategy to ½ Kelly per-strategy after 200+ closed
    outcomes — but never full. This calculator returns the raw capped
    fraction; the fractional discount happens in 6.2.2.
    """
    rets = fetch_closed_outcomes(conn, strategy_id)
    stats = kelly_stats(rets)
    if stats["n"] < int(min_samples):
        return None
    if stats["b"] <= 0:
        # One-sided outcomes (all wins or all losses) — can't measure
        # payoff ratio. Treat as no edge until both sides populate.
        return 0.0
    raw = _kelly_raw(stats["win_rate"], stats["b"])
    if raw <= 0:
        return 0.0
    if raw > float(cap):
        return round(float(cap), 4)
    return round(raw, 4)


def kelly_diagnostic(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    min_samples: int = DEFAULT_KELLY_MIN_SAMPLES,
    cap: float = KELLY_CAP,
) -> Dict:
    """Return Kelly fraction + the full stats payload + guard status.

    Shape:
      {fraction: float|None, stats: {...}, guard: "qualifying" |
       "need_more_samples" | "no_edge" | "capped",
       min_samples: int, samples_needed: int}

    Used by the 6.2.3 dashboard card to show per-strategy guard status.
    """
    rets = fetch_closed_outcomes(conn, strategy_id)
    stats = kelly_stats(rets)
    if stats["n"] < int(min_samples):
        return {
            "fraction": None,
            "stats": stats,
            "guard": "need_more_samples",
            "min_samples": int(min_samples),
            "samples_needed": int(min_samples) - stats["n"],
        }
    if stats["b"] <= 0:
        return {
            "fraction": 0.0, "stats": stats,
            "guard": "no_edge", "min_samples": int(min_samples),
            "samples_needed": 0,
        }
    raw = _kelly_raw(stats["win_rate"], stats["b"])
    if raw <= 0:
        return {
            "fraction": 0.0, "stats": stats,
            "guard": "no_edge", "min_samples": int(min_samples),
            "samples_needed": 0,
        }
    capped = raw > float(cap)
    return {
        "fraction": round(min(raw, float(cap)), 4),
        "stats": stats,
        "guard": "capped" if capped else "qualifying",
        "min_samples": int(min_samples),
        "samples_needed": 0,
        "raw_fraction": round(raw, 4),
    }
