import pandas as pd

from strategies.intraday import candle_continuation as cc


def _s(vals):
    return pd.Series(vals)


def test_combine_entry_three_of_five():
    pattern = _s([True, True, False])
    trend = _s([True, True, True])
    vwap = _s([True, False, True])
    level = _s([False, False, True])
    vol = _s([False, True, True])
    time_ok = _s([True, True, True])
    out = cc.combine_entry(pattern, trend, vwap, level, vol, time_ok,
                           min_confirms=3)
    assert list(out) == [True, True, False]


def test_combine_entry_pattern_is_mandatory():
    pattern = _s([False])
    allt = _s([True])
    out = cc.combine_entry(pattern, allt, allt, allt, allt, allt,
                           min_confirms=1)
    assert bool(out.iloc[0]) is False


def test_combine_entry_min_confirms_threshold():
    pattern = _s([True])
    trend = _s([True])
    off = _s([False])
    time_ok = _s([True])
    # gates satisfied = trend + pattern = 2
    assert bool(cc.combine_entry(pattern, trend, off, off, off, time_ok,
                                 min_confirms=3).iloc[0]) is False
    assert bool(cc.combine_entry(pattern, trend, off, off, off, time_ok,
                                 min_confirms=2).iloc[0]) is True


def test_combine_entry_time_is_hard_filter():
    t = _s([True])
    blocked = _s([False])
    assert bool(cc.combine_entry(t, t, t, t, t, blocked,
                                 min_confirms=1).iloc[0]) is False


def test_session_vwap_single_session():
    df = pd.DataFrame(
        {"high": [10.0, 12.0], "low": [8.0, 10.0],
         "close": [9.0, 11.0], "volume": [100.0, 100.0]})
    v = cc.session_vwap(df)
    assert round(v.iloc[0], 4) == 9.0
    assert round(v.iloc[1], 4) == 10.0   # (900+1100)/200


def test_time_mask_blocks_lunch_lull():
    idx = pd.to_datetime(["2026-06-08 10:00", "2026-06-08 11:15",
                          "2026-06-08 13:00", "2026-06-08 15:00"])
    m = cc.time_mask(idx, cc.DEFAULTS["active_windows"])
    assert list(m) == [True, False, True, False]


def test_bearish_exit_excludes_shooting_star():
    # lone shooting star bar: shooting_star fires, exit_any must NOT
    df = pd.DataFrame({"open": [10.0], "high": [11.0],
                       "low": [9.7], "close": [9.8]})
    assert bool(cc.cp.shooting_star(df).iloc[0]) is True
    assert bool(cc.bearish_exit_any(df).iloc[0]) is False


def test_compute_outputs_contract_columns():
    df = pd.DataFrame({
        "open": [10, 10.1, 10.2, 10.3, 10.4],
        "high": [10.2, 10.3, 10.4, 10.5, 10.6],
        "low": [9.9, 10.0, 10.1, 10.2, 10.3],
        "close": [10.1, 10.2, 10.3, 10.4, 10.5],
        "volume": [100, 100, 100, 100, 100],
    })
    out = cc.compute_candle_continuation(df)
    for col in ("long_entry", "long_exit", "ema_fast", "ema_slow", "vwap"):
        assert col in out.columns
    assert out["long_entry"].dtype == bool
    assert out["long_exit"].dtype == bool
