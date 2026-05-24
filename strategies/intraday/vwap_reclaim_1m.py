"""
vwap_reclaim_1m.py — 7.5.5 1-minute VWAP-reclaim strategy.

Long entry: price has dipped BELOW the session VWAP and then crossed
back ABOVE it. Volume confirmation: rvol > 1.0 on the reclaim bar.

Long exit: price closes back below VWAP after the reclaim.

VWAP resets at the start of each session-day (date boundary in the
index). The cumulative typical-price × volume series is built per-day,
not across days — common practice.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


RVOL_LOOKBACK = 20
RVOL_THRESHOLD = 1.0


def _session_vwap(df: pd.DataFrame) -> pd.Series:
    """Compute per-session-day VWAP. Resets each new date in the index."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("VWAP requires a DatetimeIndex")
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    dates = pd.Series(df.index.date, index=df.index)
    # Cumulate within each date group.
    cum_pv = pv.groupby(dates).cumsum()
    cum_vol = df["volume"].groupby(dates).cumsum()
    # Avoid div-by-zero for the first bar of each session (volume=0 case).
    vwap = cum_pv / cum_vol.replace(0, np.nan)
    return vwap


def compute_intraday_1m_vwap_reclaim(
    df: pd.DataFrame,
    *,
    rvol_lookback: int = RVOL_LOOKBACK,
    rvol_threshold: float = RVOL_THRESHOLD,
) -> pd.DataFrame:
    """1-minute VWAP-reclaim: dip below VWAP then close back above with rvol."""
    if rvol_lookback < 1:
        raise ValueError("rvol_lookback must be >= 1")
    out = df.copy()
    n = len(out)
    if n == 0:
        out["long_entry"] = pd.Series(dtype=bool)
        out["long_exit"] = pd.Series(dtype=bool)
        out["vwap"] = pd.Series(dtype=float)
        return out
    vwap = _session_vwap(out)
    out["vwap"] = vwap
    above = out["close"] > vwap
    below = out["close"] < vwap
    # Reclaim = above now AND below at any point earlier in the session.
    dates = pd.Series(out.index.date, index=out.index)
    # Per-session cumulative below count up to (but excluding) current bar.
    below_int = below.astype(int)
    cum_below = below_int.groupby(dates).cumsum().shift(1)
    cum_below = cum_below.groupby(dates).ffill().fillna(0)
    has_dipped = (cum_below > 0)
    avg_vol = out["volume"].rolling(rvol_lookback).mean().shift(1)
    rvol = out["volume"] / avg_vol
    rvol_ok = (rvol > rvol_threshold).fillna(False)
    # Trigger only on the bar that CROSSES (above now, below previous).
    crossed = above & below.shift(1, fill_value=False)
    entry = (crossed & has_dipped & rvol_ok).fillna(False).astype(bool)
    exit_cond = (out["close"] < vwap).fillna(False).astype(bool)
    out["long_entry"] = entry
    out["long_exit"] = exit_cond
    return out
