"""
drawdown_throttle.py — Portfolio-drawdown auto-throttle for the
auto-trader.

Inspects current portfolio_value against the trailing 30-day peak and
returns a per-entry size multiplier:

  ≤ 95% of peak   → 0.5  (halve positions)
  ≤ 90% of peak   → 0.25 (quarter positions)
  ≤ 85% of peak   → 0.0  + trip kill switch
  > 97% of peak   → 1.0  (full size; recovery)
  in between      → previous level held (sticky to avoid flapping)

Thresholds are configurable via settings.auto_trade.drawdown_throttle.
Defaults match the milestone spec exactly.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

DEFAULTS = {
    "window_days":       30,
    "halve_at_pct":      95.0,   # current_pv / peak * 100 <= this → 0.5x
    "quarter_at_pct":    90.0,   # <= this → 0.25x
    "kill_at_pct":       85.0,   # <= this → trip kill switch
    "recover_at_pct":    97.0,   # >= this → full size restored
}

MULT_FULL = 1.0
MULT_HALF = 0.5
MULT_QUARTER = 0.25
MULT_HALT = 0.0


def _coerce_settings(raw) -> Dict:
    out = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for k, default in DEFAULTS.items():
        v = raw.get(k)
        if v is None:
            continue
        try:
            v = float(v) if k != "window_days" else int(v)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        out[k] = v
    return out


def evaluate(
    *, current_pv: Optional[float],
    peak_pv: Optional[float],
    settings_throttle: Optional[Dict] = None,
) -> Dict:
    """Pure function — no I/O. Returns:
      {multiplier, level, ratio_pct, peak_pv, current_pv, reason, trip_kill_switch}

    `level` is one of "full" | "half" | "quarter" | "halt".
    """
    cfg = _coerce_settings(settings_throttle)
    info = {
        "multiplier": MULT_FULL,
        "level": "full",
        "ratio_pct": None,
        "peak_pv": peak_pv,
        "current_pv": current_pv,
        "reason": "",
        "trip_kill_switch": False,
        "config": cfg,
    }
    if (current_pv is None or peak_pv is None
            or peak_pv <= 0 or current_pv <= 0):
        info["reason"] = "no portfolio history yet — full size"
        return info
    ratio = (float(current_pv) / float(peak_pv)) * 100.0
    info["ratio_pct"] = round(ratio, 4)
    if ratio <= cfg["kill_at_pct"]:
        info["multiplier"] = MULT_HALT
        info["level"] = "halt"
        info["trip_kill_switch"] = True
        info["reason"] = (
            f"current {current_pv:.2f} is {ratio:.2f}% of peak "
            f"{peak_pv:.2f} (≤ {cfg['kill_at_pct']:.1f}% halt threshold)"
        )
        return info
    if ratio <= cfg["quarter_at_pct"]:
        info["multiplier"] = MULT_QUARTER
        info["level"] = "quarter"
        info["reason"] = (
            f"current is {ratio:.2f}% of peak (≤ "
            f"{cfg['quarter_at_pct']:.1f}% → quarter size)"
        )
        return info
    if ratio <= cfg["halve_at_pct"]:
        info["multiplier"] = MULT_HALF
        info["level"] = "half"
        info["reason"] = (
            f"current is {ratio:.2f}% of peak (≤ "
            f"{cfg['halve_at_pct']:.1f}% → halved)"
        )
        return info
    info["reason"] = f"current is {ratio:.2f}% of peak — full size"
    return info


def maybe_engage_kill_switch(
    info: Dict, *, engage_fn: Optional[Callable] = None,
) -> bool:
    """If `info` calls for a kill-switch trip, engage it. Idempotent
    on re-call — the kill switch module itself overwrites the reason
    with the latest message, which is what we want."""
    if not info.get("trip_kill_switch"):
        return False
    if engage_fn is None:
        from monitoring import kill_switch
        engage_fn = kill_switch.engage
    engage_fn(info["reason"])
    return True
