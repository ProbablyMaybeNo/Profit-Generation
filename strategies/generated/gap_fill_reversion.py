"""compute_fn for candidate strategy `gap-fill-reversion` (P2 prototype).

EOD mean-reversion in the proven botnet101 family: buy when today GAPS DOWN
below the prior close by >= GAP_PCT and then RECLAIMS (today's close finishes
back above today's open, i.e. intraday buyers stepped in). This is the
overnight-gap analogue of the 3-bar-low / consec-below-EMA reversion edge.

Entry (long): (open <= prior_close * (1 - GAP_PCT)) AND (close > open)
              AND (close > 200-SMA)   # regime filter, same as RSI variants
Exit (long):  close >= prior_close   # gap filled / reverted to pre-gap level
              OR close >= entry-bar's high-water proxy via SMA(5) cross-up

Signals act on the bar they fire; the engine fills next-bar-open, so this is
a "signal today, enter tomorrow's open" EOD strategy consistent with botnet101.

CANDIDATE ONLY — not wired into live routing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

GAP_PCT = 0.01   # 1% gap-down threshold
USE_TREND_FILTER = True


def compute_gap_fill_reversion(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    prior_close = df["close"].shift(1)
    gap_down = df["open"] <= prior_close * (1.0 - GAP_PCT)
    reclaim = df["close"] > df["open"]
    sma200 = df["close"].rolling(200).mean()

    entry = gap_down & reclaim
    if USE_TREND_FILTER:
        entry = entry & (df["close"] > sma200)

    # Exit when the gap is filled (price reverts to / above the pre-gap close)
    # OR the bounce stalls (close back above SMA(5)). Gap-fill target is the
    # natural mean-reversion objective; SMA(5) overshoot is the give-up signal.
    sma5 = df["close"].rolling(5).mean()
    exit_ = (df["close"] >= prior_close) | (df["close"] > sma5)

    out["long_entry"] = entry.fillna(False)
    out["long_exit"] = exit_.fillna(False)
    return out
