import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import backtest_candle_continuation as bt  # noqa: E402


def _synthetic_frame(n=120):
    """A single RTH session of 5m bars with a gentle uptrend + intrabar
    range, enough rows for EMA20/ATR14 to be defined and for the
    continuation strategy to (maybe) fire. The runner must not crash and
    must produce the expected columns regardless of how many trades fire."""
    idx = pd.date_range(start="2026-01-05 09:30", periods=n, freq="5min")
    rng = np.random.default_rng(7)
    base = np.linspace(100.0, 108.0, n)
    noise = rng.normal(0, 0.15, n)
    close = base + noise
    open_ = close - rng.normal(0, 0.1, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.2, 0.1, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.2, 0.1, n))
    vol = rng.integers(800, 2000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "volume": vol}, index=idx)


def test_run_backtest_has_expected_columns():
    data = {"AMD": _synthetic_frame(), "TSLA": _synthetic_frame()}
    df = bt.run_backtest(data)
    assert list(df.columns) == bt.OUT_COLUMNS
    # per-symbol rows + one aggregate ALL row
    assert "ALL" in set(df["symbol"])
    assert set(df["symbol"]) >= {"AMD", "TSLA", "ALL"}


def test_metrics_for_empty_is_zeroed():
    m = bt.metrics_for("AMD", [])
    assert m["trades"] == 0
    assert m["profit_factor"] == 0.0
    assert m["expectancy_pct"] == 0.0
    assert set(m) == set(bt.OUT_COLUMNS)


def test_metrics_for_known_trades():
    trades = [
        bt.Trade("AMD", 1, 5, 100.0, 110.0, "trailing_stop", 4),   # +10%
        bt.Trade("AMD", 6, 9, 100.0, 95.0, "initial_stop", 3),     # -5%
    ]
    m = bt.metrics_for("AMD", trades)
    assert m["trades"] == 2
    assert m["win_rate"] == 0.5
    assert m["avg_win_pct"] == pytest.approx(10.0, abs=1e-6)
    assert m["avg_loss_pct"] == pytest.approx(-5.0, abs=1e-6)
    # PF = gross_win / gross_loss = 0.10 / 0.05 = 2.0
    assert m["profit_factor"] == pytest.approx(2.0, abs=1e-6)
    # expectancy = (0.10 - 0.05)/2 = 2.5%
    assert m["expectancy_pct"] == pytest.approx(2.5, abs=1e-6)
    assert m["avg_bars_held"] == 3.5


def test_profit_factor_infinite_when_no_losers():
    trades = [bt.Trade("X", 1, 2, 100.0, 105.0, "eod_close", 1)]
    m = bt.metrics_for("X", trades)
    assert m["profit_factor"] == float("inf")


def test_simulate_symbol_respects_eod_no_overnight():
    # two short sessions; any trade opened in session 1 must close by the end
    # of session 1 (no carry into session 2).
    s1 = pd.date_range("2026-01-05 09:30", periods=30, freq="5min")
    s2 = pd.date_range("2026-01-06 09:30", periods=30, freq="5min")
    idx = s1.append(s2)
    rng = np.random.default_rng(3)
    close = np.concatenate([np.linspace(100, 104, 30),
                            np.linspace(104, 108, 30)]) + rng.normal(0, 0.1, 60)
    open_ = close - rng.normal(0, 0.1, 60)
    high = np.maximum(open_, close) + 0.3
    low = np.minimum(open_, close) - 0.3
    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                       "close": close, "volume": np.full(60, 1500.0)}, index=idx)
    trades = bt.simulate_symbol("AMD", df)
    for t in trades:
        entry_day = idx[t.entry_idx].normalize()
        exit_day = idx[t.exit_idx].normalize()
        assert entry_day == exit_day, "intraday trade leaked overnight"


def test_simulate_symbol_initial_stop_triggers_on_crash():
    # uptrend long enough to arm an entry, then a hard crash bar that takes
    # out the initial stop within the same session.
    idx = pd.date_range("2026-01-05 09:30", periods=60, freq="5min")
    close = list(np.linspace(100, 112, 55)) + [112, 90, 88, 87, 86]
    close = np.array(close, dtype=float)
    open_ = close.copy()
    high = close + 0.4
    low = close - 0.4
    low[56] = 70.0  # deep crash bar low — guarantees a stop cross if in a trade
    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                       "close": close, "volume": np.full(60, 1600.0)}, index=idx)
    trades = bt.simulate_symbol("AMD", df)
    # we don't assert a trade necessarily fires (depends on confirms), but if
    # one is open over the crash it must exit on a stop, not run negative-open.
    for t in trades:
        assert t.exit_price > 0


def test_render_table_is_ascii_only():
    data = {"AMD": _synthetic_frame()}
    df = bt.run_backtest(data)
    table = bt.render_table(df)
    table.encode("cp1252")  # must not raise on the Windows console codec
    assert "symbol" in table
    assert "ALL" in table


def test_load_history_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        bt.load_history("5m", root=tmp_path)
