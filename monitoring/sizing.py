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
SIZING_METHOD_TIERED = "tiered"
SUPPORTED_SIZING_METHODS = {
    SIZING_METHOD_FIXED, SIZING_METHOD_KELLY, SIZING_METHOD_TIERED,
}

# Tiered sizing defaults (per-tier capital in USD). All overridable via
# settings.auto_trade.tiered.{tier_0_usd, tier_1_usd, tier_2_usd,
# tier_3_usd, tier_3_min_sharpe}.
TIERED_DEFAULTS = {
    "tier_0_usd": 200.0,    # < 5 closed outcomes
    "tier_1_usd": 500.0,    # 5-19
    "tier_2_usd": 1000.0,   # 20-49
    "tier_3_usd": 2000.0,   # 50+ with Sharpe > tier_3_min_sharpe
    "tier_3_min_sharpe": 0.3,
}


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


def _coerce_tiered_settings(raw) -> Dict:
    """Merge `settings.auto_trade.tiered` over TIERED_DEFAULTS. Any
    non-numeric / negative override falls back to the default for that
    key so a typo never silently zeros out a tier."""
    out = dict(TIERED_DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for k, default in TIERED_DEFAULTS.items():
        v = raw.get(k)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v < 0:
            continue
        out[k] = v
    return out


def _tier_for(n: int, sharpe: float, caps: Dict) -> int:
    """Decide tier from outcome count + sharpe."""
    if n < 5:
        return 0
    if n < 20:
        return 1
    if n < 50:
        return 2
    if sharpe > caps["tier_3_min_sharpe"]:
        return 3
    return 2  # 50+ outcomes but not enough edge → stay at tier 2


def tiered_notional(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    settings_tiered: Optional[Dict] = None,
    max_position_usd: Optional[float] = None,
) -> Dict:
    """Return {notional, tier, sharpe, stats} for a tier-sized entry.

    `max_position_usd` is applied as a hard ceiling on top of the tier
    notional — if the user wants tier 3 but their max_position_usd is
    $800, the entry is capped at $800. Pass None to disable the ceiling.
    """
    caps = _coerce_tiered_settings(settings_tiered)
    rets = fetch_returns(conn, strategy_id)
    n = len(rets)
    if n == 0:
        sharpe = 0.0
    else:
        mean = sum(rets) / n
        sd = statistics.stdev(rets) if n > 1 else 0.0
        sharpe = (mean / sd) if sd > 0 else 0.0
    tier = _tier_for(n, sharpe, caps)
    tier_amount = caps[f"tier_{tier}_usd"]
    notional = float(tier_amount)
    if max_position_usd is not None and max_position_usd > 0:
        notional = min(notional, float(max_position_usd))
    return {
        "notional": round(notional, 2),
        "tier": tier,
        "sharpe": round(sharpe, 4),
        "stats": {"n": n},
        "caps": caps,
    }


def compute_notional(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    sizing_method: str,
    portfolio_value: Optional[float],
    max_position_usd: float,
    cap: float = KELLY_CAP,
    settings_tiered: Optional[Dict] = None,
    regime_multiplier: Optional[float] = None,
    strategy_class: Optional[str] = None,
    min_position_usd: float = 0.0,
) -> Dict:
    """Single entry point for the auto-trader.

    Returns {notional, sizing_method, fraction?, stats?, tier?,
              regime_multiplier?, base_notional?}.
    For "fixed", notional is just max_position_usd (the existing
    behavior); for "kelly" it routes through kelly_notional; for
    "tiered" it routes through tiered_notional with max_position_usd
    as a hard ceiling.

    When `regime_multiplier` is supplied (milestone 4.7.3), the base
    notional from the chosen method is multiplied by it. The product
    is floored at `min_position_usd` to keep the position above the
    broker minimum even when the regime is unfriendly.
    """
    method = normalize_sizing_method(sizing_method)
    if method == SIZING_METHOD_KELLY:
        out = kelly_notional(
            conn, strategy_id, portfolio_value,
            max_position_usd=max_position_usd, cap=cap,
        )
        out["sizing_method"] = method
    elif method == SIZING_METHOD_TIERED:
        out = tiered_notional(
            conn, strategy_id,
            settings_tiered=settings_tiered,
            max_position_usd=max_position_usd,
        )
        out["sizing_method"] = method
    else:
        out = {
            "notional": float(max_position_usd),
            "sizing_method": SIZING_METHOD_FIXED,
            "fraction": None,
            "stats": None,
        }
    if regime_multiplier is not None:
        base = float(out["notional"])
        adjusted = base * float(regime_multiplier)
        if min_position_usd and 0 < adjusted < min_position_usd:
            adjusted = float(min_position_usd)
        out["base_notional"] = round(base, 2)
        out["regime_multiplier"] = round(float(regime_multiplier), 4)
        if strategy_class is not None:
            out["strategy_class"] = strategy_class
        out["notional"] = round(adjusted, 2)
    return out


def resolve_regime_multiplier(
    *,
    strategy_class: Optional[str],
    regime: Optional[str],
    confidence: Optional[float] = None,
    confidence_floor: float = 0.6,
) -> float:
    """Return the per-mode capital multiplier for the strategy class given
    the current market regime. Trend strategies in a friendly regime get
    > 0.5, mean-reversion strategies in choppy / low-vol regimes likewise.

    Strategies with an unknown / missing class get 1.0 (uneffected).
    """
    from monitoring.regime_router import (
        allocation_for_regime, size_multiplier,
    )
    alloc = allocation_for_regime(
        regime or "mixed",
        confidence=confidence,
        confidence_floor=confidence_floor,
    )
    return size_multiplier(strategy_class or "", allocation=alloc)
