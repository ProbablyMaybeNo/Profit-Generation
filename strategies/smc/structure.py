"""
structure.py — Market structure: Break of Structure (BOS), liquidity sweeps,
and bias inference. Operates over swing points produced by primitives.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional

import pandas as pd

from strategies.smc.primitives import SwingPoint, swing_points


@dataclass(frozen=True)
class BreakOfStructure:
    timestamp: datetime
    price: float
    direction: Literal["bull", "bear"]
    broken_swing_ts: datetime
    broken_swing_price: float


@dataclass(frozen=True)
class LiquiditySweep:
    timestamp: datetime
    extreme_price: float
    close_price: float
    direction: Literal["bull", "bear"]
    swept_swing_ts: datetime


def detect_bos(
    df: pd.DataFrame,
    swings: Optional[List[SwingPoint]] = None,
) -> List[BreakOfStructure]:
    """
    A bullish BOS is a bar that closes above the most recent confirmed
    swing high. A bearish BOS closes below the most recent swing low.

    We track the running 'most recent confirmed swing of each kind' as we
    walk forward, and emit one BOS per crossing event.
    """
    if swings is None:
        swings = swing_points(df)
    if not swings:
        return []

    closes = df["close"].values
    idx = df.index
    out: List[BreakOfStructure] = []

    sw_iter = iter(swings)
    last_high: Optional[SwingPoint] = None
    last_low: Optional[SwingPoint] = None
    pending = next(sw_iter, None)

    for t in range(len(df)):
        while pending is not None and pending.bar_index <= t:
            if pending.kind == "high":
                last_high = pending
            else:
                last_low = pending
            pending = next(sw_iter, None)

        c = closes[t]
        if last_high is not None and c > last_high.price:
            out.append(BreakOfStructure(
                timestamp=idx[t], price=float(c), direction="bull",
                broken_swing_ts=last_high.timestamp,
                broken_swing_price=last_high.price,
            ))
            last_high = None
        if last_low is not None and c < last_low.price:
            out.append(BreakOfStructure(
                timestamp=idx[t], price=float(c), direction="bear",
                broken_swing_ts=last_low.timestamp,
                broken_swing_price=last_low.price,
            ))
            last_low = None

    return out


def detect_liquidity_sweep(
    df: pd.DataFrame,
    swings: Optional[List[SwingPoint]] = None,
    max_age_bars: int = 60,
) -> List[LiquiditySweep]:
    """
    A bullish (long-side) sweep: price wicks below a recent swing low and
    closes back above it within the same bar. Bearish: wicks above a
    swing high and closes back below.

    max_age_bars caps how far back we look for the swept swing — a year-old
    swing being 'taken out' is not the same kind of liquidity event as
    yesterday's low.
    """
    if swings is None:
        swings = swing_points(df)
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    idx = df.index
    out: List[LiquiditySweep] = []

    swing_highs = [s for s in swings if s.kind == "high"]
    swing_lows = [s for s in swings if s.kind == "low"]

    for t in range(len(df)):
        for sh in reversed(swing_highs):
            if sh.bar_index >= t:
                continue
            if t - sh.bar_index > max_age_bars:
                break
            if highs[t] > sh.price and closes[t] < sh.price:
                out.append(LiquiditySweep(
                    timestamp=idx[t], extreme_price=float(highs[t]),
                    close_price=float(closes[t]), direction="bear",
                    swept_swing_ts=sh.timestamp,
                ))
                break
        for sl in reversed(swing_lows):
            if sl.bar_index >= t:
                continue
            if t - sl.bar_index > max_age_bars:
                break
            if lows[t] < sl.price and closes[t] > sl.price:
                out.append(LiquiditySweep(
                    timestamp=idx[t], extreme_price=float(lows[t]),
                    close_price=float(closes[t]), direction="bull",
                    swept_swing_ts=sl.timestamp,
                ))
                break
    return out


def bias_from_swings(
    swings: List[SwingPoint],
    as_of_index: int,
) -> Literal["bull", "bear", "neutral"]:
    """
    Determine bias from the last 4 confirmed swings up to as_of_index.
    Higher highs + higher lows → bull. Lower highs + lower lows → bear.
    Anything else → neutral.
    """
    visible = [s for s in swings if s.bar_index < as_of_index]
    if len(visible) < 4:
        return "neutral"
    last4 = visible[-4:]
    highs = [s for s in last4 if s.kind == "high"]
    lows = [s for s in last4 if s.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "neutral"
    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price
    if hh and hl:
        return "bull"
    if lh and ll:
        return "bear"
    return "neutral"


if __name__ == "__main__":
    from backtest.data import load_bars
    data = load_bars(["SPY"], "2024-01-02", "2024-01-31", "5m")["SPY"]
    swings = swing_points(data, lookback=2)
    bos = detect_bos(data, swings)
    sweeps = detect_liquidity_sweep(data, swings)
    print(f"5m bars: {len(data)}")
    print(f"swings: {len(swings)}")
    print(f"BOS events: {len(bos)} (sample: {bos[:2]})")
    print(f"liquidity sweeps: {len(sweeps)} (sample: {sweeps[:2]})")
    final_bias = bias_from_swings(swings, as_of_index=len(data))
    print(f"final bias: {final_bias}")
