"""LLM-generated compute_fn for strategy `inside-day-breakout`.

Source: local://inside-day-breakout
Generated: 2026-05-15

DO NOT hand-edit unless you also update records.jsonl. Re-run
codegen_strategy.py to regenerate.
"""

import pandas as pd
import numpy as np


def compute_inside_day_breakout(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    
    # Calculate previous day's high and low
    prev_high = df["high"].shift(1)
    prev_low = df["low"].shift(1)
    
    # Calculate the day before yesterday's high and low
    day_before_high = df["high"].shift(2)
    day_before_low = df["low"].shift(2)
    
    # Check if yesterday was an inside day
    is_inside_day = (prev_high < day_before_high) & (prev_low > day_before_low)
    
    # Long entry condition: today's close > yesterday's high AND yesterday was an inside day
    out["long_entry"] = ((df["close"] > prev_high) & is_inside_day).fillna(False)
    
    # Calculate 5-day low for exit condition
    lowest_5 = df["low"].rolling(5).min().shift(1)
    
    # Long exit condition: close drops below 5-day low
    out["long_exit"] = (df["close"] < lowest_5).fillna(False)
    
    return out
