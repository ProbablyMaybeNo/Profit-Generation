"""
momentum_1m.py — 7.5.5 1-minute momentum strategy.

Long entry: 3 consecutive 1-minute bars each close above a rising
20-period EMA, AND volume on the entry bar > 1.5× the trailing 20-bar
average volume (rvol > 1.5).

Long exit: close drops back below the EMA20.

Per-session-day context: EMA + rolling volume are computed on the full
1m series. No special intraday reset — momentum riding through midday
remains valid; the EOD batch close happens elsewhere via
auto_trader's intraday position cleanup.
"""
from __future__ import annotations

import pandas as pd


EMA_PERIOD = 20
RVOL_LOOKBACK = 20
RVOL_THRESHOLD = 1.5
CONSEC_BARS = 3


def compute_intraday_1m_momentum(
    df: pd.DataFrame,
    *,
    ema_period: int = EMA_PERIOD,
    rvol_lookback: int = RVOL_LOOKBACK,
    rvol_threshold: float = RVOL_THRESHOLD,
    consec_bars: int = CONSEC_BARS,
) -> pd.DataFrame:
    """1-minute momentum entry: N consecutive closes above rising EMA + rvol > T."""
    if ema_period < 2:
        raise ValueError("ema_period must be >= 2")
    if rvol_lookback < 1:
        raise ValueError("rvol_lookback must be >= 1")
    if consec_bars < 1:
        raise ValueError("consec_bars must be >= 1")
    out = df.copy()
    n = len(out)
    if n == 0:
        out["long_entry"] = pd.Series(dtype=bool)
        out["long_exit"] = pd.Series(dtype=bool)
        return out
    ema = out["close"].ewm(span=ema_period, adjust=False).mean()
    ema_rising = (ema.diff() > 0).fillna(False)
    above_ema = out["close"] > ema
    consec_above = above_ema.rolling(consec_bars).sum() == consec_bars
    avg_vol = out["volume"].rolling(rvol_lookback).mean().shift(1)
    rvol = out["volume"] / avg_vol
    rvol_ok = (rvol > rvol_threshold).fillna(False)
    entry = (consec_above & ema_rising & rvol_ok).fillna(False)
    # Exit: close drops back below EMA.
    exit_cond = (out["close"] < ema).fillna(False)
    out["long_entry"] = entry
    out["long_exit"] = exit_cond
    return out
