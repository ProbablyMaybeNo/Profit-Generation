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

from typing import Dict, List, Optional, Tuple


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


def _in_window(ts: str, entry_ts: Optional[str], exit_ts: Optional[str]) -> bool:
    if not ts:
        return True
    if entry_ts and ts < entry_ts:
        return False
    if exit_ts and ts > exit_ts:
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
