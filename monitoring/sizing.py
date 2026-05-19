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
SIZING_METHOD_KELLY_QUARTER = "kelly_quarter"  # 6.2.2
SUPPORTED_SIZING_METHODS = {
    SIZING_METHOD_FIXED, SIZING_METHOD_KELLY, SIZING_METHOD_TIERED,
    SIZING_METHOD_KELLY_QUARTER,
}

# 6.2.2 — Fractional-Kelly sizing tier defaults.
# fraction_of_kelly: how much of the raw Kelly fraction we actually
#   stake (0.25 = ¼ Kelly is the default — full Kelly is famously
#   brutal on noisy estimates). Code hard-caps this to 0.5 (½ Kelly)
#   so a typo can't accidentally route a strategy through full Kelly.
# max_position_fraction: hard ceiling on any single position as a
#   fraction of portfolio (0.05 = 5%).
# min_samples: number of closed outcomes required before Kelly fires;
#   below that, fall back to the previous sizing tier.
DEFAULT_FRACTION_OF_KELLY = 0.25
HARD_CAP_FRACTION_OF_KELLY = 0.50  # never above ½ Kelly
DEFAULT_MAX_POSITION_FRACTION = 0.05
DEFAULT_KELLY_MIN_SAMPLES = 50

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


DEFAULT_INTRADAY_SIZE_MULTIPLIER = 0.5  # 5.5.1


def resolve_intraday_multiplier(
    *,
    bar_interval: Optional[str],
    declaration: Optional[Dict] = None,
    settings_auto_trade: Optional[Dict] = None,
    default: float = DEFAULT_INTRADAY_SIZE_MULTIPLIER,
) -> Optional[float]:
    """Return the intraday capital multiplier or None when not applicable.

    Returns None for EOD (`bar_interval == "1d"` or missing) so callers
    can detect non-applicability and skip the discount entirely.

    Override precedence:
      1. declaration["intraday_size_multiplier"] when set on the TRACKED
         strategy entry — per-strategy override.
      2. settings_auto_trade["intraday_size_multiplier"] — global default
         from settings.json.
      3. The function `default` argument.

    Non-numeric / negative overrides fall through to the next source.
    """
    interval = (bar_interval or "1d")
    if interval == "1d":
        return None
    candidates = []
    if isinstance(declaration, dict):
        candidates.append(declaration.get("intraday_size_multiplier"))
    if isinstance(settings_auto_trade, dict):
        candidates.append(settings_auto_trade.get("intraday_size_multiplier"))
    for c in candidates:
        if c is None:
            continue
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        if v < 0:
            continue
        return v
    return float(default)


def _coerce_kelly_settings(raw: Optional[Dict]) -> Dict:
    """Merge `settings.sizing.kelly` (or `settings.kelly`) over Phase 6.2.2
    defaults. Any non-numeric / out-of-range override falls back to the
    default so a typo never accidentally routes through full Kelly.

    Defaults:
      fraction_of_kelly = 0.25 (¼ Kelly)
      max_position_fraction = 0.05 (5% of portfolio per position)
      min_samples = 50
    """
    out = {
        "fraction_of_kelly": DEFAULT_FRACTION_OF_KELLY,
        "max_position_fraction": DEFAULT_MAX_POSITION_FRACTION,
        "min_samples": DEFAULT_KELLY_MIN_SAMPLES,
    }
    if not isinstance(raw, dict):
        return out
    # fraction_of_kelly — clamped at hard cap so the 6.2.2 acceptance
    # test ("never above ½ Kelly") is enforced in code.
    v = raw.get("fraction_of_kelly")
    if v is not None:
        try:
            f = float(v)
            if 0 < f <= HARD_CAP_FRACTION_OF_KELLY:
                out["fraction_of_kelly"] = f
            elif f > HARD_CAP_FRACTION_OF_KELLY:
                # Hard-clamp to ½ Kelly so a typo can't push to full.
                out["fraction_of_kelly"] = HARD_CAP_FRACTION_OF_KELLY
        except (TypeError, ValueError):
            pass
    # max_position_fraction — non-numeric / non-positive falls back.
    v = raw.get("max_position_fraction")
    if v is not None:
        try:
            f = float(v)
            if 0 < f <= 1.0:
                out["max_position_fraction"] = f
        except (TypeError, ValueError):
            pass
    # min_samples — non-int / negative falls back.
    v = raw.get("min_samples")
    if v is not None:
        try:
            n = int(v)
            if n >= 0:
                out["min_samples"] = n
        except (TypeError, ValueError):
            pass
    return out


def kelly_quarter_notional(
    conn: sqlite3.Connection,
    strategy_id: str,
    portfolio_value: Optional[float],
    *,
    max_position_usd: float,
    settings_kelly: Optional[Dict] = None,
    cap: float = KELLY_CAP,
) -> Dict:
    """Phase 6.2.2 fractional-Kelly sizing tier.

    Routes through `monitoring.kelly.calc_kelly_fraction`. When the
    Kelly guard fails (None — fewer than min_samples closed outcomes),
    returns `{notional: None, ...}` so the auto-trader's compute_notional
    can fall back to the previous tier. Otherwise:

        notional = portfolio_value × min(
            fraction_of_kelly × kelly_fraction,
            max_position_fraction,
        )
        notional = min(notional, max_position_usd)

    `fraction_of_kelly` defaults to 0.25 (¼ Kelly) and is hard-capped
    in code at 0.5 (½ Kelly). `max_position_fraction` defaults to 0.05
    (5% of portfolio per position).

    Returns the shape:
      {notional: float | None,
       fraction: float,            # the raw Kelly (0.25-capped)
       sized_fraction: float,      # fraction_of_kelly × fraction
       fraction_of_kelly: float,
       max_position_fraction: float,
       stats: dict,                # kelly_stats payload
       guard_status: str,          # qualifying / need_more_samples / no_edge / capped
       fallback: bool}             # True when caller should drop to next tier
    """
    from monitoring import kelly as kelly_mod
    settings = _coerce_kelly_settings(settings_kelly)
    diag = kelly_mod.kelly_diagnostic(
        conn, strategy_id,
        min_samples=settings["min_samples"], cap=cap,
    )
    raw_fraction = diag.get("fraction")
    if raw_fraction is None:
        # Guard failed → caller falls back to previous tier.
        return {
            "notional": None,
            "fraction": None,
            "sized_fraction": None,
            "fraction_of_kelly": settings["fraction_of_kelly"],
            "max_position_fraction": settings["max_position_fraction"],
            "stats": diag.get("stats"),
            "guard_status": diag.get("guard"),
            "fallback": True,
        }
    if raw_fraction <= 0:
        # Negative edge → no entry sized at all. Return 0 (not None) so
        # the caller knows we evaluated Kelly and decided to skip.
        return {
            "notional": 0.0,
            "fraction": 0.0,
            "sized_fraction": 0.0,
            "fraction_of_kelly": settings["fraction_of_kelly"],
            "max_position_fraction": settings["max_position_fraction"],
            "stats": diag.get("stats"),
            "guard_status": diag.get("guard"),
            "fallback": False,
        }
    sized_fraction = min(
        settings["fraction_of_kelly"] * raw_fraction,
        settings["max_position_fraction"],
    )
    if portfolio_value is None or portfolio_value <= 0:
        return {
            "notional": 0.0,
            "fraction": raw_fraction,
            "sized_fraction": round(sized_fraction, 6),
            "fraction_of_kelly": settings["fraction_of_kelly"],
            "max_position_fraction": settings["max_position_fraction"],
            "stats": diag.get("stats"),
            "guard_status": diag.get("guard"),
            "fallback": False,
        }
    target = sized_fraction * float(portfolio_value)
    if max_position_usd and max_position_usd > 0:
        target = min(target, float(max_position_usd))
    return {
        "notional": round(target, 2),
        "fraction": raw_fraction,
        "sized_fraction": round(sized_fraction, 6),
        "fraction_of_kelly": settings["fraction_of_kelly"],
        "max_position_fraction": settings["max_position_fraction"],
        "stats": diag.get("stats"),
        "guard_status": diag.get("guard"),
        "fallback": False,
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
    settings_kelly: Optional[Dict] = None,
    regime_multiplier: Optional[float] = None,
    strategy_class: Optional[str] = None,
    min_position_usd: float = 0.0,
    intraday_multiplier: Optional[float] = None,
    fallback_method: str = SIZING_METHOD_TIERED,
) -> Dict:
    """Single entry point for the auto-trader.

    Returns {notional, sizing_method, fraction?, stats?, tier?,
              regime_multiplier?, base_notional?, intraday_multiplier?}.
    For "fixed", notional is just max_position_usd (the existing
    behavior); for "kelly" it routes through kelly_notional; for
    "tiered" it routes through tiered_notional with max_position_usd
    as a hard ceiling; for "kelly_quarter" it routes through
    `kelly_quarter_notional` with auto-fallback to `fallback_method`
    when the Kelly guard fails.

    When `regime_multiplier` is supplied (milestone 4.7.3), the base
    notional from the chosen method is multiplied by it. The product
    is floored at `min_position_usd` to keep the position above the
    broker minimum even when the regime is unfriendly.

    When `intraday_multiplier` is supplied (5.5.1), the notional is
    further reduced — typically to 0.5 of the EOD-equivalent — to
    compensate for higher turnover / slippage exposure. Applied AFTER
    any regime multiplier, then re-floored to `min_position_usd`.
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
    elif method == SIZING_METHOD_KELLY_QUARTER:
        kq = kelly_quarter_notional(
            conn, strategy_id, portfolio_value,
            max_position_usd=max_position_usd,
            settings_kelly=settings_kelly, cap=cap,
        )
        if kq.get("fallback"):
            # Drop to fallback tier (default tiered). Preserve the Kelly
            # diagnostic on the result so downstream consumers can log
            # why Kelly didn't fire.
            fb_method = normalize_sizing_method(fallback_method)
            if fb_method == SIZING_METHOD_TIERED:
                out = tiered_notional(
                    conn, strategy_id,
                    settings_tiered=settings_tiered,
                    max_position_usd=max_position_usd,
                )
            elif fb_method == SIZING_METHOD_KELLY:
                out = kelly_notional(
                    conn, strategy_id, portfolio_value,
                    max_position_usd=max_position_usd, cap=cap,
                )
            else:
                out = {
                    "notional": float(max_position_usd),
                    "fraction": None,
                    "stats": None,
                }
            out["sizing_method"] = fb_method
            out["kelly_quarter"] = {
                "guard_status": kq.get("guard_status"),
                "stats": kq.get("stats"),
                "fallback": True,
                "fraction_of_kelly": kq.get("fraction_of_kelly"),
                "max_position_fraction": kq.get("max_position_fraction"),
            }
        else:
            out = {
                "notional": kq["notional"],
                "fraction": kq["fraction"],
                "stats": kq["stats"],
                "sizing_method": SIZING_METHOD_KELLY_QUARTER,
                "kelly_quarter": kq,
            }
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
    if intraday_multiplier is not None:
        pre = float(out["notional"])
        if "base_notional" not in out:
            out["base_notional"] = pre
        adjusted = pre * float(intraday_multiplier)
        if min_position_usd and 0 < adjusted < min_position_usd:
            adjusted = float(min_position_usd)
        out["intraday_multiplier"] = round(float(intraday_multiplier), 4)
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


# ---------------------------------------------------------------------------
# 6.1.1 — ATR-based initial stops (generalized across all strategies)
# ---------------------------------------------------------------------------
#
# Phase 4.6.1 wired ATR into trend strategies via stops.compute_atr +
# `auto_trade.stop_loss_atr_multiple`. Phase 6.1.1 generalizes that so
# every strategy can opt in via per-strategy multiplier overrides, and
# adds a fixed-percent fallback for the case where ATR can't be computed
# (e.g. <14 bars of history on a fresh symbol).
#
# Settings shape (config/settings.json):
#
#   "stops": {
#     "atr_period": 14,
#     "atr_multiplier": 2.5,
#     "fixed_percent_fallback": 0.05,
#     "per_strategy": {
#       "intraday-orbo-5m": {"atr_multiplier": 1.5},
#       "botnet101-3-bar-low": {"atr_multiplier": 2.0}
#     }
#   }
#
# The legacy `auto_trade.stop_loss_atr_multiple` is still honored — if
# it's set and non-zero, it overrides `stops.atr_multiplier` (so we
# don't break Phase 4.6 trend strategies that already shipped with it).

DEFAULT_ATR_INITIAL_PERIOD = 14
DEFAULT_ATR_INITIAL_MULTIPLIER = 2.5
STOP_METHOD_ATR_INITIAL = "atr_initial"
STOP_METHOD_FIXED_PERCENT = "fixed_percent"


def _coerce_positive(raw, default: float) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if v <= 0:
        return float(default)
    return v


def resolve_atr_multiplier(
    *,
    strategy_id: Optional[str],
    settings_stops: Optional[Dict] = None,
    legacy_multiple: Optional[float] = None,
    strategy_class: Optional[str] = None,
    default: float = DEFAULT_ATR_INITIAL_MULTIPLIER,
) -> float:
    """Return the ATR multiplier to use for this strategy.

    Precedence (highest wins):
      1. `legacy_multiple` (the existing `auto_trade.stop_loss_atr_multiple`
         setting), when truthy — preserves Phase 4.6 behavior.
      2. `settings_stops["per_strategy"][strategy_id]["atr_multiplier"]`
      3. `settings_stops["by_class"][strategy_class]["atr_multiplier"]`
         — 6.1.2 added this so all mean-reversion strategies inherit
         `k=2.0` without listing every id explicitly.
      4. `settings_stops["atr_multiplier"]`
      5. `default` (2.5).

    Non-numeric / non-positive values at any level fall through to the
    next source so a typo never silently zeros out the stop.
    """
    if legacy_multiple is not None:
        try:
            lm = float(legacy_multiple)
        except (TypeError, ValueError):
            lm = 0.0
        if lm > 0:
            return lm
    if isinstance(settings_stops, dict):
        per = settings_stops.get("per_strategy")
        if isinstance(per, dict) and strategy_id:
            entry = per.get(strategy_id)
            if isinstance(entry, dict):
                v = entry.get("atr_multiplier")
                if v is not None:
                    try:
                        f = float(v)
                        if f > 0:
                            return f
                    except (TypeError, ValueError):
                        pass
        by_class = settings_stops.get("by_class")
        if isinstance(by_class, dict) and strategy_class:
            entry = by_class.get(strategy_class)
            if isinstance(entry, dict):
                v = entry.get("atr_multiplier")
                if v is not None:
                    try:
                        f = float(v)
                        if f > 0:
                            return f
                    except (TypeError, ValueError):
                        pass
        v = settings_stops.get("atr_multiplier")
        if v is not None:
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                pass
    return float(default)


def atr_initial_stop(
    *,
    entry_price: float,
    atr: Optional[float],
    multiplier: float,
    side: str = "long",
) -> Optional[float]:
    """Compute the initial stop level from entry + ATR + multiplier.

    For longs:  stop = entry_price - (multiplier × ATR).
    For shorts: stop = entry_price + (multiplier × ATR) — mirror.

    Returns None when inputs are degenerate (missing ATR, non-positive
    multiplier, or the resulting stop wouldn't sit on the correct side
    of entry).
    """
    if entry_price is None or atr is None or atr <= 0 or multiplier <= 0:
        return None
    if entry_price <= 0:
        return None
    side_lc = (side or "long").lower()
    if side_lc not in ("long", "short"):
        return None
    delta = float(multiplier) * float(atr)
    if side_lc == "long":
        stop = float(entry_price) - delta
        if stop >= entry_price or stop <= 0:
            return None
    else:
        stop = float(entry_price) + delta
        if stop <= entry_price:
            return None
    return round(stop, 4)


def fixed_percent_stop(
    *,
    entry_price: float,
    percent: float,
    side: str = "long",
) -> Optional[float]:
    """Fallback stop at entry_price × (1 ∓ percent).

    `percent` is a fraction (0.05 = 5%). Negative or zero percent
    disables the fallback (returns None).
    """
    if entry_price is None or entry_price <= 0:
        return None
    try:
        pct = float(percent)
    except (TypeError, ValueError):
        return None
    if pct <= 0:
        return None
    side_lc = (side or "long").lower()
    if side_lc not in ("long", "short"):
        return None
    if side_lc == "long":
        stop = float(entry_price) * (1.0 - pct)
        if stop <= 0 or stop >= entry_price:
            return None
    else:
        stop = float(entry_price) * (1.0 + pct)
        if stop <= entry_price:
            return None
    return round(stop, 4)


def resolve_initial_stop(
    *,
    entry_price: float,
    atr: Optional[float],
    strategy_id: Optional[str],
    settings_stops: Optional[Dict] = None,
    legacy_multiple: Optional[float] = None,
    side: str = "long",
    strategy_class: Optional[str] = None,
    regime: Optional[str] = None,
    regime_confidence: Optional[float] = None,
    default_multiplier: float = DEFAULT_ATR_INITIAL_MULTIPLIER,
) -> Dict:
    """One-shot resolver used by the auto-trader.

    Tries the ATR initial stop first. If that returns None (no ATR
    available, or the math doesn't yield a valid level), falls back to
    a fixed-percent stop using `settings_stops["fixed_percent_fallback"]`
    when present. Returns:

        {"stop_price": float | None,
         "method":    "atr_initial" | "fixed_percent" | None,
         "multiplier": float | None,             # k_effective
         "base_multiplier": float | None,        # k_base
         "regime_multiplier": float | None,
         "regime": str | None,
         "fallback_percent": float | None}

    `method is None` only when both ATR and the fixed-percent fallback
    failed to produce a valid stop — caller should treat that as "no
    stop attached" and downgrade the entry accordingly.

    6.1.3 — When `regime` is supplied, k_effective = k_base × regime_mult
    where the multiplier comes from
    `regime_router.stop_regime_multiplier`. The result is clamped to
    [0.7, 1.5] inside that helper so a misconfigured override can't
    explode the stop band.
    """
    base_multiplier = resolve_atr_multiplier(
        strategy_id=strategy_id,
        settings_stops=settings_stops,
        legacy_multiple=legacy_multiple,
        strategy_class=strategy_class,
        default=default_multiplier,
    )
    regime_mult = 1.0
    if regime is not None:
        from monitoring.regime_router import stop_regime_multiplier
        overrides = None
        if isinstance(settings_stops, dict):
            overrides = settings_stops.get("regime_multipliers")
        regime_mult = stop_regime_multiplier(
            regime,
            confidence=regime_confidence,
            overrides=overrides,
        )
    multiplier = round(base_multiplier * regime_mult, 4)
    out: Dict = {
        "stop_price": None,
        "method": None,
        "multiplier": multiplier,
        "base_multiplier": base_multiplier,
        "regime_multiplier": round(regime_mult, 4),
        "regime": regime,
        "fallback_percent": None,
    }
    stop = atr_initial_stop(
        entry_price=entry_price, atr=atr,
        multiplier=multiplier, side=side,
    )
    if stop is not None:
        out["stop_price"] = stop
        out["method"] = STOP_METHOD_ATR_INITIAL
        return out
    fallback_pct = None
    if isinstance(settings_stops, dict):
        fallback_pct = settings_stops.get("fixed_percent_fallback")
    if fallback_pct is None:
        return out
    try:
        pct = float(fallback_pct)
    except (TypeError, ValueError):
        return out
    if pct <= 0:
        return out
    fallback_stop = fixed_percent_stop(
        entry_price=entry_price, percent=pct, side=side,
    )
    if fallback_stop is None:
        return out
    out["stop_price"] = fallback_stop
    out["method"] = STOP_METHOD_FIXED_PERCENT
    out["fallback_percent"] = round(pct, 4)
    return out
