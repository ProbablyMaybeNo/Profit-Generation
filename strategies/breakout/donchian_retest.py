"""donchian_retest — 6.3.1 Donchian breakout-and-retest (long side).

Signal logic:
  1. `level = highest_high_20.shift(1)` — prior 20-bar high.
  2. A bar's `close > level` marks a "breakout watch" — the strategy now
     looks for a retest entry on any of the NEXT 5 bars (inclusive).
  3. On each of those bars, if the bar's price range straddles
     `level ± 0.5 × ATR_14` (i.e., low ≤ level + 0.5 × ATR AND
     high ≥ level - 0.5 × ATR), enter long.
  4. After 5 bars with no retest, the pending entry is cancelled —
     no chase.
  5. Exit: standard Donchian channel reverse — close below the prior
     10-bar low. Cuts losers without holding through the next cycle.

Vectorized implementation walks bar-by-bar, but the cost is O(N × 5)
which is negligible for typical 5-year backtests.

Contract: takes a DataFrame with columns open/high/low/close (any
case). Returns the same frame with added boolean columns:
  - `long_entry`  — fires on the retest bar
  - `long_exit`   — fires on close below the trailing 10-bar low
  - `breakout`    — diagnostic: True on each breakout-watch bar
  - `pending`     — diagnostic: True on each bar where a retest entry
                     is still active (within 5 bars of a breakout)
"""
from __future__ import annotations

import pandas as pd


DONCHIAN_PERIOD = 20
ATR_PERIOD = 14
RETEST_WINDOW = 5
RETEST_TOLERANCE_K = 0.5


def _columns(df: pd.DataFrame) -> dict:
    return {c.lower(): c for c in df.columns}


def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Simple-mean ATR matching monitoring.stops.compute_atr's
    convention (no Wilder smoothing). Returns a series aligned to
    df.index; the first `period` rows are NaN."""
    cols = _columns(df)
    high = df[cols["high"]].astype(float)
    low = df[cols["low"]].astype(float)
    close = df[cols["close"]].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_donchian_retest(df: pd.DataFrame) -> pd.DataFrame:
    """Compute long_entry / long_exit for the Donchian breakout-retest.

    See module docstring for the signal definition.
    """
    cols = _columns(df)
    out = df.copy()
    if not all(k in cols for k in ("high", "low", "close")):
        out["long_entry"] = False
        out["long_exit"] = False
        out["breakout"] = False
        out["pending"] = False
        return out
    high = df[cols["high"]].astype(float)
    low = df[cols["low"]].astype(float)
    close = df[cols["close"]].astype(float)
    level = high.rolling(DONCHIAN_PERIOD).max().shift(1)
    low_channel = low.rolling(10).min().shift(1)
    atr = _atr(df, period=ATR_PERIOD)
    breakout = (close > level).fillna(False)
    # For each bar, track whether ANY of the prior RETEST_WINDOW bars
    # (excluding the current bar — the breakout bar itself isn't a
    # retest) was a breakout AND we haven't already retested since.
    n = len(df)
    long_entry = pd.Series(False, index=df.index)
    pending = pd.Series(False, index=df.index)
    # `pending_breakout_level`: the level we're waiting to retest, or NaN.
    # `pending_bars_left`: how many more bars (incl current) the pending
    #   entry stays valid.
    waiting_level = None
    waiting_left = 0
    breakout_arr = breakout.to_numpy()
    level_arr = level.to_numpy()
    low_arr = low.to_numpy()
    high_arr = high.to_numpy()
    atr_arr = atr.to_numpy()
    entry_arr = [False] * n
    pending_arr = [False] * n
    for i in range(n):
        # Check whether the current bar retests an active pending breakout
        # BEFORE we consider this bar's own breakout (we don't enter on
        # the breakout bar itself — only on retests of prior breakouts).
        if waiting_left > 0 and waiting_level is not None:
            tol = atr_arr[i]
            if pd.notna(tol) and pd.notna(waiting_level):
                lower = waiting_level - RETEST_TOLERANCE_K * tol
                # Retest = the bar pulled back DOWN to (or below) the
                # broken level. Specifically: the bar's low touched or
                # broke the level, AND stayed within the tolerance band
                # below (so a flash crash 10×ATR below the level isn't a
                # retest — that's the breakout failing).
                #
                # Equivalent: low ≤ level (actual touch) AND
                #             low ≥ level - 0.5×ATR (within tolerance).
                # Bars that stay entirely above the level are continuations,
                # not retests, and the strategy must NOT chase.
                if low_arr[i] <= waiting_level and low_arr[i] >= lower:
                    entry_arr[i] = True
                    waiting_level = None
                    waiting_left = 0
            if waiting_left > 0:
                # Bar consumes one slot of the retest window.
                pending_arr[i] = True
                waiting_left -= 1
                if waiting_left == 0:
                    # Window expired without a retest — clear it.
                    waiting_level = None
        # If this bar IS a breakout, arm a new pending entry for the
        # next RETEST_WINDOW bars. (Replaces any active wait — the
        # most recent breakout is the relevant one.)
        if breakout_arr[i] and pd.notna(level_arr[i]):
            waiting_level = level_arr[i]
            waiting_left = RETEST_WINDOW
    long_entry = pd.Series(entry_arr, index=df.index)
    pending = pd.Series(pending_arr, index=df.index)
    long_exit = (close < low_channel).fillna(False)
    out["long_entry"] = long_entry
    out["long_exit"] = long_exit
    out["breakout"] = breakout
    out["pending"] = pending
    return out
