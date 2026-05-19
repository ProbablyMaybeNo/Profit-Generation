"""donchian_retest_short — 6.3.2 Donchian breakdown-and-retest (short).

Mirror of 6.3.1 for the short side. Signal logic:

  1. `level = lowest_low_20.shift(1)` — prior 20-bar low.
  2. A bar's `close < level` marks a "breakdown watch" — the strategy
     now looks for a retest entry on any of the NEXT 5 bars.
  3. On each of those bars, if the bar's HIGH pushes back up to (or
     through) the broken level within tolerance: specifically
     `high ≥ level` AND `high ≤ level + 0.5 × ATR_14`, enter short.
  4. After 5 bars with no retest, the pending entry cancels — no chase.
  5. Exit: standard Donchian channel reverse for shorts — close above
     the prior 10-bar high.

Risk caveats baked into the declaration (see __init__.py):
  - pyramidable: False — single-shot entries, no add-on tiers.
  - max position size 50% of the long-side breakout-retest equivalent
    (configured via `auto_trade.short_max_position_usd_multiplier`
    or directly via per-strategy max_position_usd override). Borrow
    costs + unlimited-loss exposure make over-sizing dangerous.
  - active_in_regimes maps the milestone's "bear / trend" intent to
    the project's `trending_down` vocabulary plus `mixed`.

Contract: same as the long-side module. Takes OHLC DataFrame, returns
a DataFrame with added boolean columns:
  - `short_entry` — fires on the retest bar
  - `short_exit`  — fires on close above the trailing 10-bar high
  - `breakdown`   — diagnostic: True on each breakdown-watch bar
  - `pending`     — diagnostic: True on each bar where a retest entry
                     is still active
"""
from __future__ import annotations

import pandas as pd

from strategies.breakout.donchian_retest import (
    DONCHIAN_PERIOD, ATR_PERIOD, RETEST_WINDOW, RETEST_TOLERANCE_K, _atr,
    _columns,
)


def compute_donchian_retest_short(df: pd.DataFrame) -> pd.DataFrame:
    """Compute short_entry / short_exit for the Donchian breakdown-retest."""
    cols = _columns(df)
    out = df.copy()
    if not all(k in cols for k in ("high", "low", "close")):
        out["short_entry"] = False
        out["short_exit"] = False
        out["breakdown"] = False
        out["pending"] = False
        return out
    high = df[cols["high"]].astype(float)
    low = df[cols["low"]].astype(float)
    close = df[cols["close"]].astype(float)
    level = low.rolling(DONCHIAN_PERIOD).min().shift(1)
    high_channel = high.rolling(10).max().shift(1)
    atr = _atr(df, period=ATR_PERIOD)
    breakdown = (close < level).fillna(False)
    n = len(df)
    breakdown_arr = breakdown.to_numpy()
    level_arr = level.to_numpy()
    high_arr = high.to_numpy()
    atr_arr = atr.to_numpy()
    entry_arr = [False] * n
    pending_arr = [False] * n
    waiting_level = None
    waiting_left = 0
    for i in range(n):
        if waiting_left > 0 and waiting_level is not None:
            tol = atr_arr[i]
            if pd.notna(tol) and pd.notna(waiting_level):
                upper = waiting_level + RETEST_TOLERANCE_K * tol
                # Retest = the bar's HIGH pushed back up to or through
                # the broken level, but didn't blow through it more than
                # 0.5×ATR. A bar staying entirely below the level is a
                # breakdown continuation, not a retest — strategy must
                # NOT chase on continuations.
                if high_arr[i] >= waiting_level and high_arr[i] <= upper:
                    entry_arr[i] = True
                    waiting_level = None
                    waiting_left = 0
            if waiting_left > 0:
                pending_arr[i] = True
                waiting_left -= 1
                if waiting_left == 0:
                    waiting_level = None
        if breakdown_arr[i] and pd.notna(level_arr[i]):
            waiting_level = level_arr[i]
            waiting_left = RETEST_WINDOW
    short_exit = (close > high_channel).fillna(False)
    out["short_entry"] = pd.Series(entry_arr, index=df.index)
    out["short_exit"] = short_exit
    out["breakdown"] = breakdown
    out["pending"] = pd.Series(pending_arr, index=df.index)
    return out
