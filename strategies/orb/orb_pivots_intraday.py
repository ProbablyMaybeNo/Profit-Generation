"""
orb_pivots_intraday.py — ORB with classic floor-pivot confirmation,
df-in/df-out adapter for the intraday_fires pipeline (5.3.3).

The stateful strategies/orb/orb_pivots.py uses a separate daily HLCV
frame for pivot computation. This adapter derives prior-day H/L/C from
the supplied intraday bar frame directly — grouping by .date() and
taking the (high.max, low.min, close.last) per session, then shifting
by one session for the pivot inputs. That makes the function usable
within the intraday_fires.check_intraday_fires pipeline which only
hands a single intraday OHLCV frame.

Long-only entry rule (same as orb_pivots.ORBPivotsStrategy):
  bar.high > or_high  AND  R1 > or_high  AND  bar.open < or_high

Exit:
  - long_exit on the first bar where close <= prior_day_low (initial stop)
  - long_exit at EOD (default 15:55)
  - The half-pivot trailing ladder of the stateful strategy is NOT
    replicated here — the compute_fn shape only emits boolean flags and
    leaves position management (including trailing) to the auto_trader's
    own trailing-stop engine (4.6.1) once the entry is on the books.
"""

from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd


def _prior_day_hlc(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame indexed by `df.index` whose rows carry the PRIOR
    session-day's high, low, close (NaN on the first day in the frame).
    """
    dates = pd.Series(df.index.date, index=df.index)
    by_day = df.groupby(dates).agg(
        day_high=("high", "max"),
        day_low=("low", "min"),
        day_close=("close", "last"),
    )
    # Shift to get prior-day values.
    prior = by_day.shift(1)
    prior.columns = ["pdh", "pdl", "pdc"]
    # Broadcast back to intraday index via date join.
    return prior.reindex(dates.values).set_index(df.index)


def compute_orb_pivots_intraday(
    df: pd.DataFrame,
    *,
    or_window_start: time = time(9, 30),
    or_window_end: time = time(9, 45),
    eod_exit: time = time(15, 55),
) -> pd.DataFrame:
    """ORB + classic R1 pivot confirmation, long-only, single entry per
    session-day.

    Returns a copy of `df` with `long_entry`, `long_exit` boolean columns
    and pivot diagnostic columns (`pdh`, `pdl`, `pdc`, `P`, `R1`)
    appended.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("compute_orb_pivots_intraday requires a DatetimeIndex")
    if or_window_start >= or_window_end:
        raise ValueError("or_window_start must be strictly before or_window_end")

    out = df.copy()
    n = len(out)
    if n == 0:
        out["long_entry"] = np.zeros(0, dtype=bool)
        out["long_exit"] = np.zeros(0, dtype=bool)
        for k in ("pdh", "pdl", "pdc", "P", "R1"):
            out[k] = np.zeros(0, dtype=float)
        return out

    prior = _prior_day_hlc(df)
    pdh = prior["pdh"].to_numpy(dtype=float)
    pdl = prior["pdl"].to_numpy(dtype=float)
    pdc = prior["pdc"].to_numpy(dtype=float)
    pivot_p = (pdh + pdl + pdc) / 3.0
    r1 = 2.0 * pivot_p - pdl

    out["pdh"] = pdh
    out["pdl"] = pdl
    out["pdc"] = pdc
    out["P"] = pivot_p
    out["R1"] = r1

    long_entry = np.zeros(n, dtype=bool)
    long_exit = np.zeros(n, dtype=bool)

    dates = out.index.date
    times = out.index.time
    opens = out["open"].to_numpy(dtype=float)
    highs = out["high"].to_numpy(dtype=float)
    closes = out["close"].to_numpy(dtype=float)

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
            or_low = lows_iter_value(out["low"].iloc[i], or_low)
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

        # Stop hit: close <= prior-day low (initial stop).
        pdl_i = pdl[i]
        if has_entered and not np.isnan(pdl_i) and closes[i] <= pdl_i:
            long_exit[i] = True
            continue

        if has_entered:
            continue

        r1_i = r1[i]
        if np.isnan(r1_i) or np.isnan(pdl_i):
            continue

        # Entry: bar.high > or_high AND R1 > or_high AND bar.open < or_high.
        if (
            highs[i] > or_high
            and r1_i > or_high
            and opens[i] < or_high
        ):
            long_entry[i] = True
            has_entered = True

    out["long_entry"] = long_entry
    out["long_exit"] = long_exit
    return out


def lows_iter_value(current_low, running_low):
    """Tiny helper kept out of the hot loop's branch-min for clarity.
    Returns min(current_low, running_low), preferring numeric values.
    """
    if running_low is None:
        return float(current_low)
    return min(float(current_low), running_low)
