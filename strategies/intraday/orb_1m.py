"""
orb_1m.py — 7.5.5 1-minute opening-range breakout, df-to-df adapter.

Same shape as strategies/orb/orbo_intraday.py (5m ORBO) but tuned for
1-minute bars and a tighter 5-minute opening range.

Rules (long-only):
  - Per session-day, define the opening range from bars whose timestamp
    falls in [or_window_start, or_window_end) — default 09:30-09:35 ET
    (the first 5 minutes).
  - After the window closes: long_entry fires on the FIRST bar of the
    day where close > OR_high. Single-shot per day.
  - long_exit fires at EOD (default 15:55 ET) AND on any bar where
    close <= OR_low (stop hit).

Designed to consume the `intraday_bars` table from 7.5.1 once the
runner adapter shapes that data into a DatetimeIndex DataFrame.
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd


def compute_intraday_1m_orb(
    df: pd.DataFrame,
    *,
    or_window_start: time = time(9, 30),
    or_window_end: time = time(9, 35),
    eod_exit: time = time(15, 55),
) -> pd.DataFrame:
    """1-minute opening-range breakout. Long-only, single entry per day."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("compute_intraday_1m_orb requires a DatetimeIndex")
    if or_window_start >= or_window_end:
        raise ValueError(
            "or_window_start must be strictly before or_window_end"
        )
    out = df.copy()
    n = len(out)
    long_entry = np.zeros(n, dtype=bool)
    long_exit = np.zeros(n, dtype=bool)
    if n == 0:
        out["long_entry"] = long_entry
        out["long_exit"] = long_exit
        return out
    dates = out.index.date
    times = out.index.time
    highs = out["high"].to_numpy()
    lows = out["low"].to_numpy()
    closes = out["close"].to_numpy()
    current_date = None
    or_high = None
    or_low = None
    window_complete = False
    has_entered = False
    for i in range(n):
        d = dates[i]
        t = times[i]
        if d != current_date:
            current_date = d
            or_high = None
            or_low = None
            window_complete = False
            has_entered = False
        if or_window_start <= t < or_window_end:
            or_high = highs[i] if or_high is None else max(or_high, highs[i])
            or_low = lows[i] if or_low is None else min(or_low, lows[i])
            continue
        if not window_complete and t >= or_window_end:
            if or_high is not None and or_low is not None:
                window_complete = True
        if t >= eod_exit:
            if has_entered:
                long_exit[i] = True
            continue
        if not window_complete:
            continue
        if has_entered and closes[i] <= or_low:
            long_exit[i] = True
            continue
        if not has_entered and closes[i] > or_high:
            long_entry[i] = True
            has_entered = True
    out["long_entry"] = long_entry
    out["long_exit"] = long_exit
    return out
