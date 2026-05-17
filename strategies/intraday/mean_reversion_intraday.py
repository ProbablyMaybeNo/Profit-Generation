"""mean_reversion_intraday.py — Three representative botnet101
mean-reversion strategies ported to intraday bars.

All three rules are bar-count based, NOT calendar-day based, so the
original Pine semantics translate cleanly to any OHLCV frame where the
index is monotonic time and rows are evenly spaced (5m, 15m, 1h, ...).

Each function takes an OHLCV DataFrame and returns the same frame with
`long_entry` and `long_exit` boolean columns appended. They are 100%
shape-compatible with `strategies.mean_reversion.botnet101.SignalStrategy`
so the existing backtest engine and validator work without modification.

Exposed functions:
  compute_n_bar_low_intraday      — long if close < lowest_low(prev N bars)
  compute_3bar_low_intraday       — Botnet 3-Bar Low (N=3, exit on 7-bar high)
  compute_consecutive_bearish_intraday — N consecutive lower-close bars
"""

from typing import Optional

import pandas as pd


def compute_n_bar_low_intraday(
    df: pd.DataFrame, lookback: int = 5, exit_lookback: Optional[int] = None,
) -> pd.DataFrame:
    """Buy-on-N-Bar-Low intraday.

    Long entry: close < min(low) over the prior `lookback` bars.
    Long exit:  close > high of the prior bar (default) or
                close > max(high) over prior `exit_lookback` bars when set.
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    out = df.copy()
    lowest = df["low"].rolling(lookback).min().shift(1)
    if exit_lookback is None:
        prev_high = df["high"].shift(1)
        exit_cond = (df["close"] > prev_high)
    else:
        if exit_lookback < 1:
            raise ValueError("exit_lookback must be >= 1")
        prev_highest = df["high"].rolling(exit_lookback).max().shift(1)
        exit_cond = (df["close"] > prev_highest)
    out["long_entry"] = (df["close"] < lowest).fillna(False)
    out["long_exit"] = exit_cond.fillna(False)
    return out


def compute_3bar_low_intraday(df: pd.DataFrame) -> pd.DataFrame:
    """Botnet 3-Bar Low ported to intraday bars.

    Long if close < lowest_low of prior 3 bars. Exit if close > highest_high
    of prior 7 bars. Identical to the EOD `compute_3bar_low` shape but with
    the no-EMA-filter default (intraday EMA filters change behavior across
    sessions and are out of scope for this milestone).
    """
    return compute_n_bar_low_intraday(df, lookback=3, exit_lookback=7)


def compute_consecutive_bearish_intraday(
    df: pd.DataFrame, lookback: int = 3,
) -> pd.DataFrame:
    """Consecutive-bearish intraday.

    Long if `lookback` consecutive bars each close lower than the previous
    bar's close. Exit when close > the prior bar's high.
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    out = df.copy()
    bearish = df["close"] < df["close"].shift(1)
    cond = bearish.rolling(lookback).sum() == lookback
    out["long_entry"] = cond.fillna(False)
    out["long_exit"] = (df["close"] > df["high"].shift(1)).fillna(False)
    return out


# Registry mirrors strategies.mean_reversion.botnet101.STRATEGIES so the
# runner / validator can iterate uniformly. (label, compute_fn).
INTRADAY_STRATEGIES = [
    ("intraday-5bar-low", compute_n_bar_low_intraday),
    ("intraday-3bar-low", compute_3bar_low_intraday),
    ("intraday-consec-bearish", compute_consecutive_bearish_intraday),
]
