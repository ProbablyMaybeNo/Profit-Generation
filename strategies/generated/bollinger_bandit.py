"""LLM-generated compute_fn for strategy `bollinger-bandit`.

Source: local://bollinger-bandit
Generated: 2026-05-15

DO NOT hand-edit unless you also update records.jsonl. Re-run
codegen_strategy.py to regenerate.
"""

import pandas as pd
import numpy as np


import pandas as pd
import numpy as np
def compute_bollinger_bandit(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    
    # Calculate 20-period SMA
    sma_20 = df["close"].rolling(20).mean().shift(1)
    
    # Calculate 20-period standard deviation
    std_20 = df["close"].rolling(20).std().shift(1)
    
    # Calculate upper and lower Bollinger Bands
    upper_band = sma_20 + (2 * std_20)
    lower_band = sma_20 - (2 * std_20)
    
    # Calculate 14-period RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_14 = 100 - (100 / (1 + rs))
    
    # Entry rule: Long when close < lower Bollinger Band AND 14-period RSI < 40
    out["long_entry"] = ((df["close"] < lower_band) & (rsi_14 < 40)).fillna(False)
    
    # Exit rule: Exit when close > middle Bollinger Band (20-period SMA)
    out["long_exit"] = (df["close"] > sma_20).fillna(False)
    
    return out
