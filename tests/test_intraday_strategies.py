"""Tests for the intraday mean-reversion strategy variants and the
validator's intraday code path."""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import validate_strategy as vs  # noqa: E402
from strategies.intraday import mean_reversion_intraday as mri  # noqa: E402
from strategies.mean_reversion import botnet101 as eod  # noqa: E402


# ---------- helpers ----------

def _make_bars(n=120, base=100.0, interval="1d", seed=42):
    """Synthetic OHLCV with mild noise, indexed at the chosen interval."""
    import numpy as np
    rng = np.random.default_rng(seed)
    drift = rng.normal(0, 0.5, n).cumsum()
    closes = base + drift
    df = pd.DataFrame({
        "open":   closes,
        "high":   closes + abs(rng.normal(0, 0.5, n)),
        "low":    closes - abs(rng.normal(0, 0.5, n)),
        "close":  closes,
        "volume": rng.integers(1_000_000, 5_000_000, n),
    })
    freq_map = {"1d": "D", "1h": "1h", "5m": "5min", "15m": "15min", "30m": "30min"}
    freq = freq_map.get(interval, "D")
    df.index = pd.date_range("2026-01-02 09:30", periods=n, freq=freq)
    return df


def _crafted_5bar_low(interval="5m"):
    """A frame where the LAST bar definitely breaks the 5-bar low — both the
    intraday and EOD compute_fns should agree the entry fires there."""
    base = [100.0, 100.5, 101.0, 100.7, 100.3, 100.1, 99.9, 99.8, 95.0]
    df = pd.DataFrame({
        "open":  base,
        "high":  [p + 0.4 for p in base],
        "low":   [p - 0.4 for p in base],
        "close": base,
        "volume": [1_000_000] * len(base),
    })
    freq_map = {"1d": "D", "5m": "5min", "15m": "15min"}
    df.index = pd.date_range("2026-01-02 09:30", periods=len(base),
                             freq=freq_map[interval])
    return df


# ---------- shape parity vs EOD botnet101 ----------

def test_intraday_n_bar_low_shape_matches_eod_5day_low_on_same_frame():
    """When fed an identical OHLCV frame, the intraday 5-bar-low compute_fn
    produces a signal frame with the same shape and same boolean values as
    the EOD `compute_5day_low` — the only difference between them is
    semantics (bars vs days), so on the same input they must agree."""
    df = _make_bars(n=80, interval="1d", seed=7)
    intraday_out = mri.compute_n_bar_low_intraday(df, lookback=5)
    eod_out = eod.compute_5day_low(df)
    assert list(intraday_out.columns) == list(eod_out.columns)
    assert intraday_out.shape == eod_out.shape
    assert intraday_out["long_entry"].equals(eod_out["long_entry"])
    assert intraday_out["long_exit"].equals(eod_out["long_exit"])


def test_intraday_3bar_low_matches_eod_no_filter_variant():
    df = _make_bars(n=80, interval="1d", seed=11)
    out_intraday = mri.compute_3bar_low_intraday(df)
    out_eod = eod.compute_3bar_low(df)  # no EMA filter
    assert out_intraday["long_entry"].equals(out_eod["long_entry"])
    assert out_intraday["long_exit"].equals(out_eod["long_exit"])


def test_intraday_consec_bearish_matches_eod():
    df = _make_bars(n=80, interval="1d", seed=13)
    out_intraday = mri.compute_consecutive_bearish_intraday(df, lookback=3)
    out_eod = eod.compute_consecutive_bearish(df, lookback=3)
    assert out_intraday["long_entry"].equals(out_eod["long_entry"])
    assert out_intraday["long_exit"].equals(out_eod["long_exit"])


# ---------- intraday-specific behavior ----------

def test_n_bar_low_fires_on_crafted_break():
    df = _crafted_5bar_low(interval="5m")
    out = mri.compute_n_bar_low_intraday(df, lookback=5)
    assert bool(out["long_entry"].iloc[-1]) is True
    # No false-positives earlier in the frame.
    assert int(out["long_entry"].iloc[:-1].sum()) == 0


def test_n_bar_low_respects_lookback_parameter():
    df = _crafted_5bar_low(interval="5m")
    out_5 = mri.compute_n_bar_low_intraday(df, lookback=5)
    out_3 = mri.compute_n_bar_low_intraday(df, lookback=3)
    # 3-bar lookback sees a tighter prior-min, so the entry STILL fires
    # at the same bar.
    assert bool(out_5["long_entry"].iloc[-1]) is True
    assert bool(out_3["long_entry"].iloc[-1]) is True
    # Sanity: the same final bar appears as an entry in both lookbacks.
    assert out_5.shape == out_3.shape


def test_n_bar_low_rejects_invalid_lookback():
    df = _crafted_5bar_low()
    with pytest.raises(ValueError):
        mri.compute_n_bar_low_intraday(df, lookback=0)
    with pytest.raises(ValueError):
        mri.compute_n_bar_low_intraday(df, lookback=5, exit_lookback=0)


def test_consec_bearish_rejects_invalid_lookback():
    df = _make_bars(20)
    with pytest.raises(ValueError):
        mri.compute_consecutive_bearish_intraday(df, lookback=0)


def test_consec_bearish_fires_on_three_lower_closes_in_a_row():
    closes = [100, 101, 102, 101, 100, 99, 98, 100]  # three lower closes at indices 3,4,5
    df = pd.DataFrame({
        "open":  closes, "close": closes,
        "high":  [c + 0.5 for c in closes],
        "low":   [c - 0.5 for c in closes],
        "volume": [1] * len(closes),
    })
    df.index = pd.date_range("2026-01-02 09:30", periods=len(closes), freq="5min")
    out = mri.compute_consecutive_bearish_intraday(df, lookback=3)
    # bar at index 5 (close=99) closes the three-down streak.
    assert bool(out["long_entry"].iloc[5]) is True


def test_strategy_registry_has_three_entries():
    """Acceptance: 2-3 representative strategies — we ship 3."""
    assert len(mri.INTRADAY_STRATEGIES) == 3
    labels = [label for label, _ in mri.INTRADAY_STRATEGIES]
    assert "intraday-5bar-low" in labels
    assert "intraday-3bar-low" in labels
    assert "intraday-consec-bearish" in labels


# ---------- validator integration ----------

def test_validate_strategy_record_tags_timeframe_with_interval():
    """When `interval='5m'` is passed, every test_run row reports
    timeframe='5m' (not the hard-coded '1d' from the old path)."""
    df = _make_bars(n=200, interval="5m", seed=3)
    # Synthetic bars_by_sym injection bypasses the load_bars call.
    result = vs.validate_strategy_record(
        strategy_id="intraday-5bar-low",
        universe=["SPY"],
        lookback_days=14,
        fn=mri.compute_n_bar_low_intraday,
        bars_by_sym={"SPY": df},
        interval="5m",
    )
    assert result["interval"] == "5m"
    assert result["test_runs"], "expected at least one test_run"
    for run in result["test_runs"]:
        assert run["timeframe"] == "5m"
        assert run["test_id"].endswith("-5m")


def test_validate_strategy_record_15m_interval_propagates():
    df = _make_bars(n=200, interval="15m", seed=4)
    result = vs.validate_strategy_record(
        strategy_id="intraday-3bar-low",
        universe=["QQQ"],
        lookback_days=30,
        fn=mri.compute_3bar_low_intraday,
        bars_by_sym={"QQQ": df},
        interval="15m",
    )
    assert result["interval"] == "15m"
    for run in result["test_runs"]:
        assert run["timeframe"] == "15m"


def test_validate_strategy_record_eod_path_unchanged():
    """Regression: default interval='1d' still produces timeframe='1d'."""
    df = _make_bars(n=80, interval="1d", seed=5)
    result = vs.validate_strategy_record(
        strategy_id="botnet101-5day-low",
        universe=["IWM"],
        lookback_days=730,
        fn=eod.compute_5day_low,
        bars_by_sym={"IWM": df},
    )
    assert result["interval"] == "1d"
    for run in result["test_runs"]:
        assert run["timeframe"] == "1d"


def test_validate_strategy_record_intraday_min_bars_gate():
    """Intraday path needs >=100 bars; a 50-bar frame should mark UNTESTED."""
    df = _make_bars(n=50, interval="5m", seed=9)
    result = vs.validate_strategy_record(
        strategy_id="intraday-5bar-low",
        universe=["SPY"],
        lookback_days=14,
        fn=mri.compute_n_bar_low_intraday,
        bars_by_sym={"SPY": df},
        interval="5m",
    )
    assert result["per_symbol"]["SPY"]["verdict"] == "UNTESTED"
    assert "insufficient" in result["per_symbol"]["SPY"]["note"]


def test_validate_strategy_record_intraday_uses_alpaca_source_by_default(
    monkeypatch,
):
    """When interval is sub-daily AND bars_by_sym is None, the loader is
    called with source='alpaca' (yfinance has no minute history)."""
    captured = {}
    def fake_load_bars(symbols, *, start, end, interval, source):
        captured["interval"] = interval
        captured["source"] = source
        df = _make_bars(n=150, interval=interval, seed=21)
        return {s: df for s in symbols}
    # Patch the symbol that validate_strategy.py imports lazily.
    import backtest.data as bd
    monkeypatch.setattr(bd, "load_bars", fake_load_bars)

    vs.validate_strategy_record(
        strategy_id="intraday-5bar-low",
        universe=["SPY"],
        lookback_days=14,
        fn=mri.compute_n_bar_low_intraday,
        interval="5m",
    )
    assert captured["interval"] == "5m"
    assert captured["source"] == "alpaca"


def test_validate_strategy_record_daily_default_source_is_yf(monkeypatch):
    captured = {}
    def fake_load_bars(symbols, *, start, end, interval, source):
        captured["source"] = source
        return {s: _make_bars(80, interval="1d", seed=22) for s in symbols}
    import backtest.data as bd
    monkeypatch.setattr(bd, "load_bars", fake_load_bars)

    vs.validate_strategy_record(
        strategy_id="botnet101-5day-low",
        universe=["SPY"],
        lookback_days=365,
        fn=eod.compute_5day_low,
    )
    assert captured["source"] == "yf"


# ---------- runner module sanity ----------

def test_runner_module_imports_and_exposes_periods_per_year():
    """The runner ships a periods-per-year lookup; key intervals must exist."""
    from strategies.intraday import runner as r
    assert r._PERIODS_PER_YEAR["5m"] > r._PERIODS_PER_YEAR["15m"]
    assert r._PERIODS_PER_YEAR["1d"] == 252
    assert "5m" in r._PERIODS_PER_YEAR
    assert "15m" in r._PERIODS_PER_YEAR
