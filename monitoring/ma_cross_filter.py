"""
ma_cross_filter.py — Sprint 2 / M7 regime/trend-strength confirmation for the
trend-ma-cross-20-50 strategy.

Evidence: trend-ma-cross-20-50 was -2.06% on the day (ZS -23% worst loser) and
has a mixed recent aggregate — it catches weak continuations and takes large
drawdowns. Rather than pause it (it does produce genuine trend signals), we GATE
it: an MA-cross long entry must clear a trend-strength confirmation, else it's
vetoed as a weak-continuation entry.

Pure + config-driven so it's testable and tunable. Two confirmations, both must
hold for a STRONG cross:

  1. Slow-EMA slope is positive — the established trend is actually rising, not
     a flat chop that an EMA cross will whipsaw on.
  2. EMA spread (fast - slow) as a percent of price clears a minimum — a cross
     with a razor-thin spread is noise; a strong cross has meaningful separation.

Optionally (3) price is above both EMAs — confirms the entry isn't into a
pullback below trend. Tunable via settings.ma_cross_filter.*.
"""

from __future__ import annotations

from typing import Dict, List, Optional

DEFAULT_MIN_SPREAD_PCT = 0.25       # fast-slow spread >= 0.25% of price
DEFAULT_MIN_SLOPE_PCT = 0.0         # slow EMA must be rising (slope > 0)
DEFAULT_SLOPE_LOOKBACK = 5          # bars over which slow-EMA slope is measured
DEFAULT_REQUIRE_PRICE_ABOVE = True  # price must be above both EMAs


def _ema(values: List[float], span: int) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def _closes(bars) -> List[float]:
    out: List[float] = []
    for b in bars or []:
        try:
            if isinstance(b, dict):
                out.append(float(b.get("close")))
            else:
                out.append(float(b))
        except (TypeError, ValueError):
            continue
    return out


def _cfg(settings: Optional[dict], key: str, default):
    if not isinstance(settings, dict):
        return default
    src = settings.get("ma_cross_filter")
    src = src if isinstance(src, dict) else settings
    v = src.get(key)
    return default if v is None else v


def evaluate_ma_cross_strength(
    bars,
    *,
    settings: Optional[dict] = None,
    fast_span: int = 20,
    slow_span: int = 50,
) -> Dict:
    """Confirm trend strength for an MA-cross long entry from recent daily bars.

    Returns {confirmed (bool), reason, spread_pct, slope_pct, price_above}.
    With too few bars to judge, confirmed defaults to True (don't block on
    insufficient data — the strategy already fired its own cross logic).
    """
    min_spread = float(_cfg(settings, "min_spread_pct", DEFAULT_MIN_SPREAD_PCT))
    min_slope = float(_cfg(settings, "min_slope_pct", DEFAULT_MIN_SLOPE_PCT))
    lookback = int(_cfg(settings, "slope_lookback", DEFAULT_SLOPE_LOOKBACK))
    require_above = bool(_cfg(settings, "require_price_above",
                             DEFAULT_REQUIRE_PRICE_ABOVE))

    closes = _closes(bars)
    out = {
        "confirmed": True,
        "reason": "",
        "spread_pct": None,
        "slope_pct": None,
        "price_above": None,
    }
    if len(closes) < slow_span + lookback:
        out["reason"] = "insufficient bars to judge trend strength; not blocked"
        return out

    fast = _ema(closes, fast_span)
    slow = _ema(closes, slow_span)
    price = closes[-1]
    if price <= 0:
        out["reason"] = "non-positive price; not blocked"
        return out

    spread_pct = (fast[-1] - slow[-1]) / price * 100.0
    slope_pct = (slow[-1] - slow[-1 - lookback]) / price * 100.0
    price_above = price >= fast[-1] and price >= slow[-1]
    out["spread_pct"] = round(spread_pct, 4)
    out["slope_pct"] = round(slope_pct, 4)
    out["price_above"] = price_above

    fails = []
    if spread_pct < min_spread:
        fails.append(f"EMA spread {spread_pct:.3f}% < {min_spread:.3f}%")
    if slope_pct <= min_slope:
        fails.append(f"slow-EMA slope {slope_pct:.3f}% <= {min_slope:.3f}%")
    if require_above and not price_above:
        fails.append("price not above both EMAs")

    if fails:
        out["confirmed"] = False
        out["reason"] = "weak continuation: " + "; ".join(fails)
    else:
        out["reason"] = (
            f"strong cross: spread {spread_pct:.3f}%, slope {slope_pct:.3f}%"
        )
    return out
