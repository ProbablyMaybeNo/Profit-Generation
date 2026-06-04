"""
intraday_edge_gate.py — Sprint 2 / M6 intraday cost/slippage edge gate.

Several intraday strategies show negative average return DESPITE a decent win
rate — costs eat the edge. A trade whose modeled expected move doesn't clear the
estimated round-trip friction (spread + slippage) is negative-EV before it even
fills. This gate vetoes such intraday entries.

Pure + config-driven so it's trivially testable and tunable; defaults are
conservative. EOD (1d) entries are out of scope — this is intraday only.

Model (all in percent of price):
    expected_move_pct  : the strategy's modeled edge for the trade. Derived by
                         the caller from ATR/price (an ATR move is the natural
                         intraday expected-move proxy) or a strategy-declared
                         target.
    friction_pct       : round-trip cost estimate =
                         spread_pct + 2 * slippage_pct  (cross the spread once,
                         slip on entry and exit).
    min_edge_buffer_pct: required margin of edge OVER friction before we trade.

Veto when:  expected_move_pct < friction_pct + min_edge_buffer_pct.
"""

from __future__ import annotations

from typing import Dict, Optional

# Conservative defaults (percent of price). Liquid mega-cap intraday spreads are
# ~1-3 bps; slippage on a market order a few bps more. A strategy needs a clear
# multiple of that to be worth firing.
DEFAULT_SPREAD_PCT = 0.02          # 2 bps half-spread estimate
DEFAULT_SLIPPAGE_PCT = 0.03        # 3 bps per side
DEFAULT_MIN_EDGE_BUFFER_PCT = 0.05  # require 5 bps of edge beyond friction


def _cfg(settings: Optional[dict], key: str, default: float) -> float:
    if not isinstance(settings, dict):
        return default
    intraday = settings.get("intraday")
    src = intraday if isinstance(intraday, dict) else settings
    try:
        v = src.get(key)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def estimate_friction_pct(settings: Optional[dict] = None) -> float:
    """Round-trip friction estimate in percent of price."""
    spread = _cfg(settings, "spread_pct", DEFAULT_SPREAD_PCT)
    slippage = _cfg(settings, "slippage_pct", DEFAULT_SLIPPAGE_PCT)
    return spread + 2.0 * slippage


def expected_move_pct_from_atr(atr: Optional[float],
                               price: Optional[float]) -> Optional[float]:
    """ATR as a percent of price — the intraday expected-move proxy. None when
    inputs are missing/invalid so the caller can fall back (and NOT veto on a
    missing estimate)."""
    try:
        a = float(atr)
        p = float(price)
    except (TypeError, ValueError):
        return None
    if a <= 0 or p <= 0:
        return None
    return a / p * 100.0


def evaluate_edge_gate(
    *,
    expected_move_pct: Optional[float],
    settings: Optional[dict] = None,
) -> Dict:
    """Decide whether an intraday entry clears the friction gate.

    Returns {veto (bool), expected_move_pct, friction_pct, threshold_pct, reason}.

    A None / unknown expected_move_pct does NOT veto (we don't block a trade on a
    missing estimate) — the gate only fires on a positively-modeled-too-thin edge.
    """
    friction = estimate_friction_pct(settings)
    buffer = _cfg(settings, "min_edge_buffer_pct", DEFAULT_MIN_EDGE_BUFFER_PCT)
    threshold = friction + buffer
    out = {
        "veto": False,
        "expected_move_pct": (round(expected_move_pct, 4)
                              if expected_move_pct is not None else None),
        "friction_pct": round(friction, 4),
        "threshold_pct": round(threshold, 4),
        "reason": "",
    }
    if expected_move_pct is None:
        out["reason"] = "no expected-move estimate; gate not applied"
        return out
    if expected_move_pct < threshold:
        out["veto"] = True
        out["reason"] = (
            f"expected move {expected_move_pct:.3f}% < friction+buffer "
            f"{threshold:.3f}% (friction {friction:.3f}%) — edge eaten by cost"
        )
    else:
        out["reason"] = (
            f"expected move {expected_move_pct:.3f}% clears "
            f"{threshold:.3f}% threshold"
        )
    return out
