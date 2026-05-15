"""
botnet101.py — The Botnet101 mean-reversion cluster from TradingView.

Nine simple long-only mean-reversion rules, all sharing structure:
  - Daily bar mechanics
  - Some "we've gone down enough" entry signal
  - Most exit on close > prior bar high
  - Designed for ETFs and indices

Each strategy is a vectorized signal-computation function: given a price
DataFrame, return the same DataFrame with `long_entry` and `long_exit`
boolean columns added. The SignalStrategy adapter then runs them through
the Phase A backtest engine.

Source: TradingView strategies by user Botnet101, scraped 2026-04-26.
"""

from typing import Optional

import numpy as np
import pandas as pd

from backtest.engine import Bar, Order
from backtest.portfolio import Portfolio


def ibs(df: pd.DataFrame) -> pd.Series:
    """Internal Bar Strength = (close - low) / (high - low). 0.5 on doji."""
    rng = df["high"] - df["low"]
    return ((df["close"] - df["low"]) / rng.where(rng > 0, np.nan)).fillna(0.5)


def _wilder_rsi(close: pd.Series, period: int = 2) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_5day_low(df: pd.DataFrame) -> pd.DataFrame:
    """Buy on 5-day Low: long if close < lowest_low(prev 5 bars). Exit close > prev_high."""
    out = df.copy()
    lowest_5 = df["low"].rolling(5).min().shift(1)
    prev_high = df["high"].shift(1)
    out["long_entry"] = (df["close"] < lowest_5).fillna(False)
    out["long_exit"] = (df["close"] > prev_high).fillna(False)
    return out


def compute_3bar_low(df: pd.DataFrame, use_ema_filter: bool = False, ma_period: int = 200) -> pd.DataFrame:
    """3-Bar Low: long if close < lowest_low(prev 3). Exit close > highest_high(prev 7)."""
    out = df.copy()
    lowest_3 = df["low"].rolling(3).min().shift(1)
    highest_7 = df["high"].rolling(7).max().shift(1)
    cond = df["close"] < lowest_3
    if use_ema_filter:
        ema = df["close"].ewm(span=ma_period, adjust=False).mean()
        cond = cond & (df["close"] > ema)
    out["long_entry"] = cond.fillna(False)
    out["long_exit"] = (df["close"] > highest_7).fillna(False)
    return out


def compute_bb_ibs(df: pd.DataFrame, length: int = 20, mult: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands Reversal + IBS: long if IBS < 0.2 AND close < lower BB. Exit IBS > 0.8."""
    out = df.copy()
    sma = df["close"].rolling(length).mean()
    std = df["close"].rolling(length).std()
    lower = sma - mult * std
    bar_ibs = ibs(df)
    out["long_entry"] = ((bar_ibs < 0.2) & (df["close"] < lower)).fillna(False)
    out["long_exit"] = (bar_ibs > 0.8).fillna(False)
    return out


def compute_avg_hl_range_ibs(
    df: pd.DataFrame,
    length: int = 20,
    bars_below: int = 2,
    ibs_thresh: float = 0.2,
) -> pd.DataFrame:
    """
    Avg High-Low Range + IBS Reversal. Threshold = SMA(close, length) - 2.5 * SMA(H-L, length).
    Long if close has been below threshold for `bars_below` consecutive bars AND IBS < 0.2.
    Exit close > prev bar high.

    Note: original Pine source uses 'upper' as the reference; without source we
    interpret it as SMA(close) for the centerline. Document this assumption.
    """
    out = df.copy()
    hl_avg = (df["high"] - df["low"]).rolling(length).mean()
    upper = df["close"].rolling(length).mean()
    threshold = upper - 2.5 * hl_avg
    below = (df["close"] < threshold).rolling(bars_below).sum() == bars_below
    out["long_entry"] = (below & (ibs(df) < ibs_thresh)).fillna(False)
    out["long_exit"] = (df["close"] > df["high"].shift(1)).fillna(False)
    return out


def compute_turn_of_month(df: pd.DataFrame, day_threshold: int = 25) -> pd.DataFrame:
    """
    Turn of the Month on Steroids:
    Long if day_of_month >= 25 AND close < close[1] AND close[1] < close[2].
    Exit when 2-period RSI > 65.
    """
    out = df.copy()
    dom = pd.Series(df.index.day, index=df.index)
    cond = (
        (dom >= day_threshold)
        & (df["close"] < df["close"].shift(1))
        & (df["close"].shift(1) < df["close"].shift(2))
    )
    out["long_entry"] = cond.fillna(False)
    out["long_exit"] = (_wilder_rsi(df["close"], 2) > 65).fillna(False)
    return out


def compute_consecutive_below_ema(
    df: pd.DataFrame,
    threshold: int = 3,
    ma_type: str = "SMA",
    ma_length: int = 5,
) -> pd.DataFrame:
    """Consecutive bars below MA buy-the-dip. Default: 3 consecutive bars below SMA(5)."""
    out = df.copy()
    if ma_type.upper() == "EMA":
        ma = df["close"].ewm(span=ma_length, adjust=False).mean()
    else:
        ma = df["close"].rolling(ma_length).mean()
    below = (df["close"] < ma).rolling(threshold).sum() == threshold
    out["long_entry"] = below.fillna(False)
    out["long_exit"] = (df["close"] > df["high"].shift(1)).fillna(False)
    return out


def compute_turn_around_tuesday(
    df: pd.DataFrame,
    starting_day: int = 0,
    use_ma_filter: bool = False,
    ma_period: int = 200,
) -> pd.DataFrame:
    """
    Turn-around Tuesday on Steroids.
    starting_day: 0=Monday (recommended for ETFs/stocks per the doc), 6=Sunday.
    Long if dow == starting_day AND close < close[1] AND close[1] < close[2].
    Optional 200-SMA filter.
    """
    out = df.copy()
    dow = pd.Series(df.index.dayofweek, index=df.index)
    cond = (
        (dow == starting_day)
        & (df["close"] < df["close"].shift(1))
        & (df["close"].shift(1) < df["close"].shift(2))
    )
    if use_ma_filter:
        sma = df["close"].rolling(ma_period).mean()
        cond = cond & (df["close"] > sma)
    out["long_entry"] = cond.fillna(False)
    out["long_exit"] = (df["close"] > df["high"].shift(1)).fillna(False)
    return out


def compute_consecutive_bearish(df: pd.DataFrame, lookback: int = 3) -> pd.DataFrame:
    """Long if N consecutive bars closed lower than the previous close. Exit close > prev high."""
    out = df.copy()
    bearish = df["close"] < df["close"].shift(1)
    cond = bearish.rolling(lookback).sum() == lookback
    out["long_entry"] = cond.fillna(False)
    out["long_exit"] = (df["close"] > df["high"].shift(1)).fillna(False)
    return out


def compute_4bar_momentum_reversal(
    df: pd.DataFrame, buy_threshold: int = 4, lookback: int = 4
) -> pd.DataFrame:
    """4 Bar Momentum Reversal: long if close < close[lookback] for N consecutive bars. Exit close > prev high."""
    out = df.copy()
    ref = df["close"].shift(lookback)
    below_ref = df["close"] < ref
    cond = below_ref.rolling(buy_threshold).sum() == buy_threshold
    out["long_entry"] = cond.fillna(False)
    out["long_exit"] = (df["close"] > df["high"].shift(1)).fillna(False)
    return out


class SignalStrategy:
    """
    Adapter: turn a precomputed signals DataFrame into a backtest Strategy.

    Long-only, single-position. Signal generated on bar t (using bar t close
    and prior history) — order submitted, fills at bar t+1 open with slippage.
    Position size = 95% of available cash on entry; full exit on signal.
    """

    def __init__(self, symbol: str, signals: pd.DataFrame, name: str):
        self.symbol = symbol
        self.signals = signals
        self.name = name
        self._signal_lookup = {
            ts: (bool(row["long_entry"]), bool(row["long_exit"]))
            for ts, row in signals[["long_entry", "long_exit"]].iterrows()
        }
        self.in_position = False

    def on_bar(self, ts, bars, portfolio: Portfolio):
        if self.symbol not in bars:
            return []
        sig = self._signal_lookup.get(ts)
        if sig is None:
            return []
        entry_sig, exit_sig = sig
        bar = bars[self.symbol]

        if self.in_position:
            if exit_sig:
                qty = portfolio.qty(self.symbol)
                if qty > 0:
                    self.in_position = False
                    return [Order(symbol=self.symbol, qty=qty, side="sell", type="market")]
        else:
            if entry_sig and bar.close > 0:
                qty = int(portfolio.cash * 0.95 / bar.close)
                if qty > 0:
                    self.in_position = True
                    return [Order(symbol=self.symbol, qty=qty, side="buy", type="market")]
        return []
