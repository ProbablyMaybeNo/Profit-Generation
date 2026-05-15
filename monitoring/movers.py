"""
movers.py — Single-day movement snapshot for tracked stocks + crypto.

Pulls the most recent daily bar for each tracked symbol, computes:
  - 1-day return %
  - 5-day return %
  - 20-day return %
  - relative volume (today vs 20-day average)
  - distance from 20-day SMA (in %)

Used in the daily report to show what's hot/cold today.
"""

from datetime import date, timedelta
from typing import Dict, List

import numpy as np
import pandas as pd

from backtest.data import load_bars
from monitoring.config import TRACKED_STOCKS, TRACKED_SECTORS, TRACKED_CRYPTO


def snapshot(as_of: date) -> List[Dict]:
    end = (as_of + timedelta(days=1)).isoformat()
    start = (as_of - timedelta(days=60)).isoformat()
    rows: List[Dict] = []

    for symbol in TRACKED_STOCKS + TRACKED_SECTORS + TRACKED_CRYPTO:
        try:
            data = load_bars([symbol], start=start, end=end, interval="1d", source="yf")
        except Exception as e:
            rows.append({"symbol": symbol, "error": str(e)[:200]})
            continue
        if symbol not in data or data[symbol].empty:
            continue
        df = data[symbol]
        if len(df) < 21:
            continue

        last = df.iloc[-1]
        prev = df.iloc[-2]
        d5 = df.iloc[-6] if len(df) >= 6 else df.iloc[0]
        d20 = df.iloc[-21] if len(df) >= 21 else df.iloc[0]

        ret_1d = (last["close"] - prev["close"]) / prev["close"] * 100 if prev["close"] > 0 else 0
        ret_5d = (last["close"] - d5["close"]) / d5["close"] * 100 if d5["close"] > 0 else 0
        ret_20d = (last["close"] - d20["close"]) / d20["close"] * 100 if d20["close"] > 0 else 0

        vol_20d_avg = df["volume"].tail(20).mean()
        rvol = (last["volume"] / vol_20d_avg) if vol_20d_avg > 0 else None

        sma20 = df["close"].tail(20).mean()
        dist_sma20_pct = (last["close"] - sma20) / sma20 * 100 if sma20 > 0 else None

        asset_class = (
            "crypto" if symbol in TRACKED_CRYPTO else
            "sector_etf" if symbol in TRACKED_SECTORS else "major_index"
        )

        rows.append({
            "symbol": symbol,
            "asset_class": asset_class,
            "bar_date": str(df.index[-1].date()),
            "close": float(last["close"]),
            "ret_1d_pct": round(float(ret_1d), 2),
            "ret_5d_pct": round(float(ret_5d), 2),
            "ret_20d_pct": round(float(ret_20d), 2),
            "rvol_vs_20d": round(float(rvol), 2) if rvol is not None else None,
            "dist_sma20_pct": round(float(dist_sma20_pct), 2) if dist_sma20_pct is not None else None,
        })

    return rows


def classify_market_regime(rows: List[Dict]) -> str:
    """
    Crude regime classifier based on SPY/QQQ/IWM 20-day return + volatility.
    """
    indices = [r for r in rows if r.get("asset_class") == "major_index"]
    if not indices:
        return "mixed"
    avg_20d = np.mean([r["ret_20d_pct"] for r in indices if r.get("ret_20d_pct") is not None])
    if avg_20d > 3:
        return "trending_up"
    if avg_20d < -3:
        return "trending_down"
    if abs(avg_20d) < 1:
        return "low_vol"
    return "choppy"


if __name__ == "__main__":
    from datetime import date as _date
    rows = snapshot(_date.today())
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))
    print(f"\nClassified regime: {classify_market_regime(rows)}")
