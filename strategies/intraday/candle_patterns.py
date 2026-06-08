"""
candle_patterns.py — pure candlestick-pattern detectors (Stage 1 of
docs/INTRADAY_TREND_BUILD_PLAN.md).

Each detector takes an OHLCV DataFrame (columns: open/high/low/close,
volume optional) and returns a boolean pd.Series aligned to df.index, True
on the bar where the pattern COMPLETES. Detection uses only the completing
bar and prior bars (shift(1)/shift(2)) — never a future bar — so a signal at
index i is invariant to anything after i (no lookahead). The caller is
responsible for shifting entries to the next bar before acting.

Detectors are triggers, not edges (see docs/INTRADAY_RESEARCH_FINDINGS.md):
they are meant to be combined with trend / VWAP / volume / level / time
confirmation by the strategy layer, not traded standalone.

Entry-side (bullish): hammer, bullish_engulfing, piercing, morning_star,
three_white_soldiers.
Exit-side (bearish): bearish_engulfing, evening_star, shooting_star
(shooting_star is near-random — never an exit on its own).

Thresholds are module constants so the strategy/backtest can tune them.
"""

from __future__ import annotations

import pandas as pd

# --- tunable thresholds ---------------------------------------------------
DOJI_BODY_FRAC = 0.10        # body <= 10% of range → doji (small-body star)
LONG_WICK_MULT = 2.0         # hammer/shooting-star shadow >= 2× body
SHORT_WICK_FRAC = 0.30       # opposing wick <= 30% of range
SMALL_BODY_FRAC = 0.30       # "small body" (star middle) <= 30% of range
SOLDIER_WICK_FRAC = 0.30     # three-white-soldiers: upper wick <= 30% of range


def _parts(df: pd.DataFrame):
    """Return (body, rng, upper_wick, lower_wick, bull, bear) Series.

    body/upper/lower are absolute price distances; rng is high-low. bull/bear
    are booleans for an up/down candle. Zero-range bars yield zero ratios
    rather than NaN/inf downstream (callers guard with rng > 0 where needed).
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - l)
    upper = h - o.where(o > c, c)        # high - max(open, close)
    lower = o.where(o < c, c) - l        # min(open, close) - low
    bull = c > o
    bear = c < o
    return body, rng, upper, lower, bull, bear


def hammer(df: pd.DataFrame) -> pd.Series:
    """Single-bar hammer: long lower shadow, small upper shadow, body near top."""
    body, rng, upper, lower, _, _ = _parts(df)
    ok = (rng > 0) & (body > 0) \
        & (lower >= LONG_WICK_MULT * body) \
        & (upper <= SHORT_WICK_FRAC * rng)
    return ok.fillna(False)


def shooting_star(df: pd.DataFrame) -> pd.Series:
    """Single-bar shooting star (mirror of hammer). Near-random — exit aid only."""
    body, rng, upper, lower, _, _ = _parts(df)
    ok = (rng > 0) & (body > 0) \
        & (upper >= LONG_WICK_MULT * body) \
        & (lower <= SHORT_WICK_FRAC * rng)
    return ok.fillna(False)


def doji(df: pd.DataFrame) -> pd.Series:
    """Small-body bar (indecision); used as the star middle in 3-bar patterns."""
    body, rng, _, _, _, _ = _parts(df)
    ok = (rng > 0) & (body <= DOJI_BODY_FRAC * rng)
    return ok.fillna(False)


def bullish_engulfing(df: pd.DataFrame) -> pd.Series:
    """Prior down candle whose body is engulfed by the current up candle."""
    o, c = df["open"], df["close"]
    po, pc = o.shift(1), c.shift(1)
    body = (c - o).abs()
    pbody = (pc - po).abs()
    ok = (pc < po) & (c > o) & (o <= pc) & (c >= po) & (body > pbody)
    return ok.fillna(False)


def bearish_engulfing(df: pd.DataFrame) -> pd.Series:
    """Prior up candle whose body is engulfed by the current down candle."""
    o, c = df["open"], df["close"]
    po, pc = o.shift(1), c.shift(1)
    body = (c - o).abs()
    pbody = (pc - po).abs()
    ok = (pc > po) & (c < o) & (o >= pc) & (c <= po) & (body > pbody)
    return ok.fillna(False)


def piercing(df: pd.DataFrame) -> pd.Series:
    """Prior down candle; current opens below prior low, closes back above the
    prior body midpoint but below the prior open."""
    o, c, l = df["open"], df["close"], df["low"]
    po, pc, pl = o.shift(1), c.shift(1), l.shift(1)
    pmid = (po + pc) / 2.0
    ok = (pc < po) & (c > o) & (o < pl) & (c > pmid) & (c < po)
    return ok.fillna(False)


def morning_star(df: pd.DataFrame) -> pd.Series:
    """3-bar bullish reversal: big down bar, small-body star, big up bar that
    closes above the midpoint of the first bar's body."""
    o, c = df["open"], df["close"]
    o1, c1 = o.shift(2), c.shift(2)   # first bar
    o3, c3 = o, c                     # third (current) bar
    star = doji(df).shift(1, fill_value=False) | _small_body(df).shift(1, fill_value=False)
    first_bear = c1 < o1
    third_bull = c3 > o3
    mid1 = (o1 + c1) / 2.0
    ok = first_bear & star & third_bull & (c3 > mid1)
    return ok.fillna(False)


def evening_star(df: pd.DataFrame) -> pd.Series:
    """3-bar bearish reversal (mirror of morning_star). Exit trigger."""
    o, c = df["open"], df["close"]
    o1, c1 = o.shift(2), c.shift(2)
    o3, c3 = o, c
    star = doji(df).shift(1, fill_value=False) | _small_body(df).shift(1, fill_value=False)
    first_bull = c1 > o1
    third_bear = c3 < o3
    mid1 = (o1 + c1) / 2.0
    ok = first_bull & star & third_bear & (c3 < mid1)
    return ok.fillna(False)


def three_white_soldiers(df: pd.DataFrame) -> pd.Series:
    """Three consecutive up bars, each closing higher, each opening within the
    prior body, each with a small upper wick (steady advance, not blow-off)."""
    o, c = df["open"], df["close"]
    _, rng, upper, _, bull, _ = _parts(df)
    b0, b1, b2 = bull, bull.shift(1), bull.shift(2)
    rising = (c > c.shift(1)) & (c.shift(1) > c.shift(2))
    # each opens within the prior candle's body (no big gaps)
    open_in_prev = (o <= c.shift(1)) & (o >= o.shift(1))
    open_in_prev_1 = (o.shift(1) <= c.shift(2)) & (o.shift(1) >= o.shift(2))
    small_upper = (rng > 0) & (upper <= SOLDIER_WICK_FRAC * rng)
    ok = (b0 & b1 & b2).fillna(False) & rising & open_in_prev & open_in_prev_1 \
        & small_upper
    return ok.fillna(False)


def _small_body(df: pd.DataFrame) -> pd.Series:
    body, rng, _, _, _, _ = _parts(df)
    ok = (rng > 0) & (body <= SMALL_BODY_FRAC * rng)
    return ok.fillna(False)


# Registry for the strategy layer: name → (detector, side).
BULLISH_PATTERNS = {
    "hammer": hammer,
    "bullish_engulfing": bullish_engulfing,
    "piercing": piercing,
    "morning_star": morning_star,
    "three_white_soldiers": three_white_soldiers,
}
BEARISH_PATTERNS = {
    "bearish_engulfing": bearish_engulfing,
    "evening_star": evening_star,
    "shooting_star": shooting_star,
}
