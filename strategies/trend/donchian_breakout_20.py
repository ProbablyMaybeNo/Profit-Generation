"""donchian_breakout_20 — long on close above prev 20-day high; exit on
close below prev 10-day low (classical Turtle channel breakout)."""

import pandas as pd


def compute_donchian_breakout_20(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    high20 = df["high"].rolling(20).max().shift(1)
    low10 = df["low"].rolling(10).min().shift(1)
    out["long_entry"] = (df["close"] > high20).fillna(False)
    out["long_exit"] = (df["close"] < low10).fillna(False)
    return out
