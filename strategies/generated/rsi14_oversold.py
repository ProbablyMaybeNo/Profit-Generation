"""LLM-generated compute_fn for strategy `rsi14-oversold`.

Source: local://rsi14-oversold
Generated: 2026-05-15

DO NOT hand-edit unless you also update records.jsonl. Re-run
codegen_strategy.py to regenerate.
"""

import pandas as pd
import numpy as np


import pandas as pd
import numpy as np
def compute_rsi14_oversold(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi14 = 100 - (100 / (1 + rs))
    sma200 = df["close"].rolling(200).mean()
    out["long_entry"] = ((rsi14 < 30) & (df["close"] > sma200)).fillna(False)
    out["long_exit"] = (rsi14 > 50).fillna(False)
    return out
