"""new_high_volume — long on close making a new 252-bar (~52-week) high
accompanied by volume >= 150% of the 50-day average. Exit on close below
the 20-day low (looser trail than donchian, looser than the entry signal).

A high without confirming volume is treated as a low-conviction breakout
and is rejected — that's the well-documented Wyckoff / O'Neil rule."""

import pandas as pd


def compute_new_high_volume(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    high_252 = df["high"].rolling(252).max().shift(1)
    vol_avg_50 = df["volume"].rolling(50).mean().shift(1)
    low_20 = df["low"].rolling(20).min().shift(1)
    out["long_entry"] = (
        (df["close"] > high_252)
        & (df["volume"] >= 1.5 * vol_avg_50)
    ).fillna(False)
    out["long_exit"] = (df["close"] < low_20).fillna(False)
    return out
