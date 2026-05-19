"""
orbo_intraday.py — Opening-Range Breakout, df-to-df adapter for the
intraday_fires pipeline (5.3.2).

The existing strategies/orb/orbo.py is a stateful per-bar strategy used
by the backtest engine. This module exposes the same trading logic in
the df-in/df-out shape that monitoring.intraday_fires (and the existing
EOD compute_fns) expect:

    out = compute_orbo_intraday(df_5m_bars)
    out["long_entry"]  # boolean per bar
    out["long_exit"]   # boolean per bar

Rules (long-only, since auto_trader has allow_shorts=false):
  - Per session-day, define the opening range from bars whose timestamp
    falls in [or_window_start, or_window_end) — default 09:30-09:50 ET.
  - After the window closes: long_entry fires on the FIRST bar of the
    day where close > OR_high. Subsequent bars do not re-fire even if
    they also break the high (single-shot per day).
  - long_exit fires at the EOD bar (default 15:55 ET) AND on any bar
    where close <= OR_low (stop hit).
  - Bars before the window completes get long_entry=False, long_exit=False.

The index must be a DatetimeIndex (any timezone or naive both work — we
group by .date()). Session-day boundaries are computed from the index
.date() per bar.
"""

from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd


def compute_orbo_intraday(
    df: pd.DataFrame,
    *,
    or_window_start: time = time(9, 30),
    or_window_end: time = time(9, 50),
    eod_exit: time = time(15, 55),
) -> pd.DataFrame:
    """Opening-Range Breakout, long-only, single entry per session-day.

    Returns a copy of `df` with `long_entry` and `long_exit` boolean
    columns appended. Order: window-build → wait → breakout → exit.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("compute_orbo_intraday requires a DatetimeIndex")
    if or_window_start >= or_window_end:
        raise ValueError("or_window_start must be strictly before or_window_end")

    out = df.copy()
    n = len(out)
    long_entry = np.zeros(n, dtype=bool)
    long_exit = np.zeros(n, dtype=bool)

    if n == 0:
        out["long_entry"] = long_entry
        out["long_exit"] = long_exit
        return out

    # Group by trading date.
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

        # Build the opening range during the configured window.
        if or_window_start <= t < or_window_end:
            or_high = highs[i] if or_high is None else max(or_high, highs[i])
            or_low = lows[i] if or_low is None else min(or_low, lows[i])
            continue

        # First bar at or after window end with a valid range → mark complete.
        if not window_complete and t >= or_window_end:
            if or_high is not None and or_low is not None:
                window_complete = True

        # EOD: force exit any open position.
        if t >= eod_exit:
            if has_entered:
                long_exit[i] = True
            continue

        if not window_complete:
            continue

        # Stop hit: close <= OR_low → exit.
        if has_entered and closes[i] <= or_low:
            long_exit[i] = True
            continue

        # Single-shot long breakout: close > OR_high, before EOD.
        if not has_entered and closes[i] > or_high:
            long_entry[i] = True
            has_entered = True

    out["long_entry"] = long_entry
    out["long_exit"] = long_exit
    return out
