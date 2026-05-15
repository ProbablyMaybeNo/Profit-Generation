"""
primitives.py — Smart Money Concept building blocks (TJR / ICT methodology).

All detectors are pure functions over OHLCV pandas DataFrames with a
DatetimeIndex. They return immutable dataclasses or arrays so the strategy
state machine can compose them without side effects.

Definitions follow TJR's published rules:
  - Swing high  : a bar whose high is greater than N bars on each side
  - Swing low   : symmetric
  - FVG (fair value gap) : 3-bar imbalance where bar[t-2].high < bar[t].low
                            (bullish) or bar[t-2].low > bar[t].high (bearish)
  - Order block : the last opposing-color candle before a displacement move
                  that broke structure
  - Equilibrium : 50% retrace level of an impulse leg
  - Breaker     : an order block that has been violated; polarity flips
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SwingPoint:
    timestamp: datetime
    price: float
    kind: Literal["high", "low"]
    bar_index: int


@dataclass(frozen=True)
class FVG:
    start_ts: datetime
    end_ts: datetime
    top: float
    bottom: float
    direction: Literal["bull", "bear"]
    size_atr: float

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class OrderBlock:
    timestamp: datetime
    top: float
    bottom: float
    direction: Literal["bull", "bear"]

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — used as the displacement / size yardstick."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(),
         (high - prev_close).abs(),
         (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def swing_points(df: pd.DataFrame, lookback: int = 2) -> List[SwingPoint]:
    """
    Return all confirmed swing highs and lows in chronological order.

    A swing high at bar i requires bars [i-lookback, i+lookback] to all have
    highs <= df.high[i] (and strictly less than i within the window).
    Symmetric for lows. lookback=2 corresponds to TJR's "two candles either
    side" pivot definition; lookback=1 is the loose form he uses in fast charts.
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")

    highs = df["high"].values
    lows = df["low"].values
    out: List[SwingPoint] = []
    n = len(df)
    for i in range(lookback, n - lookback):
        window_high = highs[i - lookback:i + lookback + 1]
        window_low = lows[i - lookback:i + lookback + 1]
        if highs[i] == window_high.max() and (window_high[:lookback] < highs[i]).all():
            out.append(SwingPoint(df.index[i], float(highs[i]), "high", i))
        elif lows[i] == window_low.min() and (window_low[:lookback] > lows[i]).all():
            out.append(SwingPoint(df.index[i], float(lows[i]), "low", i))
    return out


def fair_value_gaps(
    df: pd.DataFrame,
    min_size_atr: float = 0.25,
    atr_period: int = 14,
) -> List[FVG]:
    """
    Detect 3-bar imbalances. min_size_atr filters tiny gaps that aren't
    meaningful (TJR's "pubic hair wicks" exclusion).
    """
    if len(df) < 3:
        return []

    a = atr(df, atr_period)
    out: List[FVG] = []
    highs = df["high"].values
    lows = df["low"].values
    idx = df.index

    for t in range(2, len(df)):
        c1_high, c1_low = highs[t - 2], lows[t - 2]
        c3_high, c3_low = highs[t], lows[t]
        cur_atr = float(a.iloc[t])
        if cur_atr <= 0:
            continue

        if c1_high < c3_low:
            size = c3_low - c1_high
            if size >= min_size_atr * cur_atr:
                out.append(FVG(
                    start_ts=idx[t - 2], end_ts=idx[t],
                    top=float(c3_low), bottom=float(c1_high),
                    direction="bull", size_atr=size / cur_atr,
                ))
        elif c1_low > c3_high:
            size = c1_low - c3_high
            if size >= min_size_atr * cur_atr:
                out.append(FVG(
                    start_ts=idx[t - 2], end_ts=idx[t],
                    top=float(c1_low), bottom=float(c3_high),
                    direction="bear", size_atr=size / cur_atr,
                ))
    return out


def order_blocks(
    df: pd.DataFrame,
    swings: Optional[List[SwingPoint]] = None,
    displacement_atr: float = 1.5,
    atr_period: int = 14,
    lookback_bars: int = 10,
) -> List[OrderBlock]:
    """
    Identify order blocks: the last opposite-color candle before a displacement
    move that broke a recent swing.

    A bullish OB is the last bearish candle within the prior `lookback_bars`
    before a displacement-up move that exceeds `displacement_atr * ATR`.
    Symmetric for bearish OBs.
    """
    if swings is None:
        swings = swing_points(df)

    a = atr(df, atr_period)
    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    idx = df.index
    out: List[OrderBlock] = []

    for t in range(1, len(df)):
        cur_atr = float(a.iloc[t])
        if cur_atr <= 0:
            continue
        body = closes[t] - opens[t]
        if abs(body) < displacement_atr * cur_atr:
            continue

        direction: Literal["bull", "bear"] = "bull" if body > 0 else "bear"
        start = max(0, t - lookback_bars)
        for j in range(t - 1, start - 1, -1):
            is_bearish_candle = closes[j] < opens[j]
            is_bullish_candle = closes[j] > opens[j]
            if direction == "bull" and is_bearish_candle:
                out.append(OrderBlock(
                    timestamp=idx[j],
                    top=float(highs[j]), bottom=float(lows[j]),
                    direction="bull",
                ))
                break
            if direction == "bear" and is_bullish_candle:
                out.append(OrderBlock(
                    timestamp=idx[j],
                    top=float(highs[j]), bottom=float(lows[j]),
                    direction="bear",
                ))
                break
    return out


def equilibrium(impulse_high: float, impulse_low: float) -> float:
    """50% retrace of an impulse leg — TJR's 'equilibrium' confluence."""
    return (impulse_high + impulse_low) / 2


if __name__ == "__main__":
    from backtest.data import load_bars
    data = load_bars(["SPY"], "2024-01-02", "2024-01-15", "5m")["SPY"]
    print(f"Loaded {len(data)} 5m bars")

    swings = swing_points(data, lookback=2)
    print(f"\nSwings: {len(swings)} (first 5: {swings[:5]})")

    fvgs = fair_value_gaps(data, min_size_atr=0.25)
    print(f"\nFVGs: {len(fvgs)}")
    for f in fvgs[:3]:
        print(f"  {f.direction} {f.start_ts} bottom={f.bottom:.2f} top={f.top:.2f} size={f.size_atr:.2f}atr")

    obs = order_blocks(data, swings=swings, displacement_atr=1.5)
    print(f"\nOrder blocks: {len(obs)}")
    for o in obs[:3]:
        print(f"  {o.direction} {o.timestamp} bottom={o.bottom:.2f} top={o.top:.2f}")
