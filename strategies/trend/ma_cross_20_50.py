"""ma_cross_20_50 — long when 20-EMA crosses above 50-EMA; exit on
opposite cross. EMA (exponential), not SMA — responds faster to trend
inception."""

import pandas as pd


def compute_ma_cross_20_50(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    ema50 = df["close"].ewm(span=50, adjust=False).mean()
    # Compare PRIOR bar vs the bar before it — pure shift(1) so no
    # look-ahead.
    fast_prev = ema20.shift(1)
    fast_prev_prev = ema20.shift(2)
    slow_prev = ema50.shift(1)
    slow_prev_prev = ema50.shift(2)
    cross_up = (fast_prev_prev <= slow_prev_prev) & (fast_prev > slow_prev)
    cross_down = (fast_prev_prev >= slow_prev_prev) & (fast_prev < slow_prev)
    out["long_entry"] = cross_up.fillna(False)
    out["long_exit"] = cross_down.fillna(False)
    return out
