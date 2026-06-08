import pandas as pd

from strategies.intraday import candle_patterns as cp


def _df(bars):
    """bars: list of (open, high, low, close)."""
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"])


def test_hammer_detects_and_rejects():
    df = _df([
        (10.0, 10.3, 9.0, 10.2),    # hammer: long lower wick, tiny upper, body top
        (10.0, 11.1, 9.9, 11.0),    # big body, no wick → not a hammer
    ])
    s = cp.hammer(df)
    assert bool(s.iloc[0]) is True
    assert bool(s.iloc[1]) is False


def test_shooting_star():
    df = _df([(10.0, 11.0, 9.7, 9.8)])   # long upper wick, tiny lower
    assert bool(cp.shooting_star(df).iloc[0]) is True
    assert bool(cp.hammer(df).iloc[0]) is False


def test_bullish_engulfing():
    df = _df([
        (10.0, 10.1, 8.9, 9.0),     # prior bearish
        (8.9, 10.2, 8.8, 10.1),     # current bullish engulfs prior body
    ])
    s = cp.bullish_engulfing(df)
    assert bool(s.iloc[1]) is True
    assert bool(s.iloc[0]) is False
    assert bool(cp.bearish_engulfing(df).iloc[1]) is False


def test_bearish_engulfing():
    df = _df([
        (9.0, 10.1, 8.9, 10.0),     # prior bullish
        (10.1, 10.2, 8.8, 8.9),     # current bearish engulfs prior body
    ])
    assert bool(cp.bearish_engulfing(df).iloc[1]) is True


def test_piercing():
    df = _df([
        (10.0, 10.2, 8.9, 9.0),     # prior bearish, low 8.9, midpoint 9.5
        (8.8, 9.7, 8.7, 9.6),       # opens below prior low, closes above mid, below prior open
    ])
    assert bool(cp.piercing(df).iloc[1]) is True


def test_morning_star():
    df = _df([
        (11.0, 11.1, 9.9, 10.0),    # big bearish, midpoint 10.5
        (9.9, 10.0, 9.8, 9.95),     # small-body star
        (9.9, 10.8, 9.85, 10.7),    # big bullish closing above 10.5
    ])
    s = cp.morning_star(df)
    assert bool(s.iloc[2]) is True
    assert bool(s.iloc[0]) is False
    assert bool(s.iloc[1]) is False


def test_evening_star():
    df = _df([
        (10.0, 11.1, 9.9, 11.0),    # big bullish, midpoint 10.5
        (11.1, 11.2, 11.0, 11.05),  # small-body star
        (11.0, 11.1, 10.2, 10.3),   # big bearish closing below 10.5
    ])
    assert bool(cp.evening_star(df).iloc[2]) is True


def test_three_white_soldiers():
    df = _df([
        (10.0, 11.05, 9.9, 11.0),
        (10.5, 11.55, 10.4, 11.5),
        (11.0, 12.05, 10.9, 12.0),
    ])
    s = cp.three_white_soldiers(df)
    assert bool(s.iloc[2]) is True
    assert bool(s.iloc[1]) is False


def test_no_lookahead_property():
    # The value at bar i must not depend on bars after i. Build a 3-bar
    # morning star, then append a wild future bar and confirm index 2 is stable.
    base = [
        (11.0, 11.1, 9.9, 10.0),
        (9.9, 10.0, 9.8, 9.95),
        (9.9, 10.8, 9.85, 10.7),
    ]
    full = _df(base + [(50.0, 99.0, 1.0, 2.0)])  # absurd future bar
    truncated = _df(base)
    assert bool(cp.morning_star(full).iloc[2]) == bool(
        cp.morning_star(truncated).iloc[2]) is True
    # same invariance for a 3-bar momentum pattern
    tws = [
        (10.0, 11.05, 9.9, 11.0),
        (10.5, 11.55, 10.4, 11.5),
        (11.0, 12.05, 10.9, 12.0),
    ]
    full2 = _df(tws + [(0.5, 0.6, 0.1, 0.2)])
    assert bool(cp.three_white_soldiers(full2).iloc[2]) == bool(
        cp.three_white_soldiers(_df(tws)).iloc[2]) is True


def test_registries_cover_expected_patterns():
    assert set(cp.BULLISH_PATTERNS) == {
        "hammer", "bullish_engulfing", "piercing",
        "morning_star", "three_white_soldiers"}
    assert set(cp.BEARISH_PATTERNS) == {
        "bearish_engulfing", "evening_star", "shooting_star"}
