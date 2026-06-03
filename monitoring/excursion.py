"""
excursion.py — Maximum favorable / adverse excursion (MFE / MAE).

A long position's MFE is the largest unrealized gain it reached between
entry and exit; MAE is the largest unrealized loss. Both are expressed as
a percentage of the entry price:

    long:  mfe = (max(high) - entry) / entry
           mae = (min(low)  - entry) / entry   (<= 0 in the usual case)

We measure over the bars whose timestamp lies in [entry_ts, exit_ts].
Bars are accepted as a list of dicts (high/low keys, optional ts) or a
pandas-like object with iterrows(); timestamps may be omitted, in which
case every supplied bar is used.

Returns None for either value when no usable bars are available, so a
caller can persist NULL rather than a misleading 0.0.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Convention used across the codebase (monitoring.intraday_fires.MARKET_TZ):
# naive timestamps are America/New_York wall-clock. Bars persisted from the
# broker carry offset-aware UTC (e.g. '...T20:46:00+00:00'); signal entry/exit
# timestamps are naive ET (e.g. '...T15:57:00'). Compare both as aware UTC.
_MARKET_TZ = ZoneInfo("America/New_York")


def _to_rows(bars) -> List[Dict]:
    if bars is None:
        return []
    if hasattr(bars, "iterrows"):
        cols = {c.lower(): c for c in bars.columns}
        if "high" not in cols or "low" not in cols:
            return []
        ts_col = cols.get("ts") or cols.get("ts_utc") or cols.get("timestamp")
        out: List[Dict] = []
        for idx, row in bars.iterrows():
            ts = str(row[ts_col]) if ts_col else (
                str(idx) if idx is not None else None
            )
            try:
                out.append({
                    "high": float(row[cols["high"]]),
                    "low": float(row[cols["low"]]),
                    "ts": ts,
                })
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(bars, list):
        out = []
        for b in bars:
            if not isinstance(b, dict):
                continue
            try:
                out.append({
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "ts": str(b.get("ts") or b.get("ts_utc")
                             or b.get("timestamp") or ""),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out
    return []


def _to_utc(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string to an aware UTC datetime.

    Naive strings (no offset) are interpreted as America/New_York wall-clock
    — the same convention used by intraday_fires / the bar pipeline on this
    non-UTC box. Returns None if the value is empty or unparseable, in which
    case the caller falls back to lexical-free inclusion.
    """
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None
    # Normalize a trailing 'Z' which fromisoformat rejects on older builds.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MARKET_TZ)
    return dt.astimezone(timezone.utc)


def _in_window(ts: str, entry_ts: Optional[str], exit_ts: Optional[str]) -> bool:
    if not ts:
        return True
    bar_dt = _to_utc(ts)
    entry_dt = _to_utc(entry_ts)
    exit_dt = _to_utc(exit_ts)
    # If the bar ts can't be parsed, fall back to lexical compare so we never
    # crash on malformed data (preserves prior behavior for that edge).
    if bar_dt is None:
        if entry_ts and ts < entry_ts:
            return False
        if exit_ts and ts > exit_ts:
            return False
        return True
    if entry_dt is not None and bar_dt < entry_dt:
        return False
    if exit_dt is not None and bar_dt > exit_dt:
        return False
    return True


def compute_mfe_mae(
    bars,
    *,
    entry_price: float,
    entry_ts: Optional[str] = None,
    exit_ts: Optional[str] = None,
    side: str = "long",
) -> Tuple[Optional[float], Optional[float]]:
    """Return (mfe_pct, mae_pct) as fractions of entry_price.

    Long:  mfe uses the highest high, mae uses the lowest low.
    Short: mirrored — mfe uses the lowest low, mae uses the highest high.

    Both are None when no usable bars fall in the window or the entry
    price is degenerate.
    """
    if entry_price in (None, 0):
        return None, None
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return None, None
    if entry <= 0:
        return None, None
    rows = [
        r for r in _to_rows(bars)
        if _in_window(r["ts"], entry_ts, exit_ts)
    ]
    if not rows:
        return None, None
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    max_high = max(highs)
    min_low = min(lows)
    side_lc = (side or "long").lower()
    if side_lc == "short":
        mfe = (entry - min_low) / entry
        mae = (entry - max_high) / entry
    else:
        mfe = (max_high - entry) / entry
        mae = (min_low - entry) / entry
    return round(mfe, 6), round(mae, 6)
