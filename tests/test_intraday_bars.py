"""
test_intraday_bars.py — 5.1.1: load_intraday_bars cache + shape behaviour.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest import data as bt_data  # noqa: E402
from config import cache as cache_mod  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.db"
    monkeypatch.setattr(cache_mod, "CACHE_FILE", cache_file)
    yield
    cache_mod.cache_clear()


def _frame(start: str, n: int, interval_min: int, base_price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n, freq=f"{interval_min}min")
    closes = [base_price + i * 0.10 for i in range(n)]
    return pd.DataFrame({
        "open":   closes,
        "high":   [c + 0.20 for c in closes],
        "low":    [c - 0.20 for c in closes],
        "close":  closes,
        "volume": [10_000.0] * n,
    }, index=idx)


def test_load_intraday_bars_5m_shape():
    df = _frame("2026-05-14 09:30", 60, 5)
    calls = []
    def fetcher(symbol, start, end, interval):
        calls.append((symbol, interval))
        return df.copy()
    out = bt_data.load_intraday_bars(
        ["SPY"], "5m", lookback_bars=20,
        now=datetime(2026, 5, 14, 15, 0), fetcher=fetcher,
    )
    assert "SPY" in out
    assert len(out["SPY"]) == 20
    assert list(out["SPY"].columns) == ["open", "high", "low", "close", "volume"]


def test_load_intraday_bars_15m_shape():
    df = _frame("2026-05-14 09:30", 30, 15)
    def fetcher(symbol, start, end, interval):
        return df.copy()
    out = bt_data.load_intraday_bars(
        ["QQQ"], "15m", lookback_bars=10,
        now=datetime(2026, 5, 14, 15, 30), fetcher=fetcher,
    )
    assert len(out["QQQ"]) == 10


def test_load_intraday_bars_1h_shape():
    df = _frame("2026-05-14 09:30", 12, 60)
    def fetcher(symbol, start, end, interval):
        return df.copy()
    out = bt_data.load_intraday_bars(
        ["IWM"], "1h", lookback_bars=6,
        now=datetime(2026, 5, 14, 16, 0), fetcher=fetcher,
    )
    assert len(out["IWM"]) == 6


def test_cache_hit_within_same_bar_window():
    """Two calls within the same 15-min window only fetch once."""
    df = _frame("2026-05-14 09:30", 30, 15)
    call_count = {"n": 0}
    def fetcher(symbol, start, end, interval):
        call_count["n"] += 1
        return df.copy()
    asof1 = datetime(2026, 5, 14, 12, 1)  # 12:00 bucket
    asof2 = datetime(2026, 5, 14, 12, 14)  # still 12:00 bucket
    bt_data.load_intraday_bars(["SPY"], "15m", 10, now=asof1, fetcher=fetcher)
    bt_data.load_intraday_bars(["SPY"], "15m", 10, now=asof2, fetcher=fetcher)
    assert call_count["n"] == 1


def test_cache_miss_when_new_bar_closes():
    """Crossing a 15-min boundary invalidates the cache."""
    df = _frame("2026-05-14 09:30", 30, 15)
    call_count = {"n": 0}
    def fetcher(symbol, start, end, interval):
        call_count["n"] += 1
        return df.copy()
    asof1 = datetime(2026, 5, 14, 12, 14)   # 12:00 bucket
    asof2 = datetime(2026, 5, 14, 12, 16)   # 12:15 bucket — new bar
    bt_data.load_intraday_bars(["SPY"], "15m", 10, now=asof1, fetcher=fetcher)
    bt_data.load_intraday_bars(["SPY"], "15m", 10, now=asof2, fetcher=fetcher)
    assert call_count["n"] == 2


def test_per_symbol_cache_keys():
    """SPY and QQQ have independent cache entries."""
    df = _frame("2026-05-14 09:30", 30, 15)
    fetched = []
    def fetcher(symbol, start, end, interval):
        fetched.append(symbol)
        return df.copy()
    asof = datetime(2026, 5, 14, 12, 5)
    bt_data.load_intraday_bars(["SPY", "QQQ"], "15m", 10, now=asof, fetcher=fetcher)
    bt_data.load_intraday_bars(["SPY"], "15m", 10, now=asof, fetcher=fetcher)
    bt_data.load_intraday_bars(["QQQ"], "15m", 10, now=asof, fetcher=fetcher)
    assert sorted(fetched) == ["QQQ", "SPY"]  # only first call hits fetcher


def test_per_interval_cache_keys():
    """5m and 15m are cached independently for the same symbol."""
    df5 = _frame("2026-05-14 09:30", 60, 5)
    df15 = _frame("2026-05-14 09:30", 30, 15)
    fetched = []
    def fetcher(symbol, start, end, interval):
        fetched.append(interval)
        return (df5 if interval == "5m" else df15).copy()
    asof = datetime(2026, 5, 14, 12, 5)
    bt_data.load_intraday_bars(["SPY"], "5m",  10, now=asof, fetcher=fetcher)
    bt_data.load_intraday_bars(["SPY"], "15m", 10, now=asof, fetcher=fetcher)
    bt_data.load_intraday_bars(["SPY"], "5m",  10, now=asof, fetcher=fetcher)
    bt_data.load_intraday_bars(["SPY"], "15m", 10, now=asof, fetcher=fetcher)
    assert sorted(fetched) == ["15m", "5m"]


def test_no_data_returns_empty_dict_entry_dropped():
    def fetcher(symbol, start, end, interval):
        return pd.DataFrame()
    out = bt_data.load_intraday_bars(
        ["ZZZ"], "15m", 10,
        now=datetime(2026, 5, 14, 12, 5), fetcher=fetcher,
    )
    assert out == {}


def test_no_data_is_cached_negatively():
    """Empty fetch result is cached so we don't hammer the API."""
    calls = {"n": 0}
    def fetcher(symbol, start, end, interval):
        calls["n"] += 1
        return pd.DataFrame()
    asof = datetime(2026, 5, 14, 12, 5)
    bt_data.load_intraday_bars(["ZZZ"], "15m", 10, now=asof, fetcher=fetcher)
    bt_data.load_intraday_bars(["ZZZ"], "15m", 10, now=asof, fetcher=fetcher)
    assert calls["n"] == 1


def test_fetcher_exception_treated_as_no_data():
    def fetcher(symbol, start, end, interval):
        raise RuntimeError("alpaca went boom")
    out = bt_data.load_intraday_bars(
        ["SPY"], "15m", 10,
        now=datetime(2026, 5, 14, 12, 5), fetcher=fetcher,
    )
    assert out == {}


def test_unsupported_interval_raises():
    with pytest.raises(ValueError, match="unsupported intraday interval"):
        bt_data.load_intraday_bars(["SPY"], "1d", 10)


def test_zero_or_negative_lookback_raises():
    with pytest.raises(ValueError, match="lookback_bars must be positive"):
        bt_data.load_intraday_bars(["SPY"], "15m", 0)
    with pytest.raises(ValueError, match="lookback_bars must be positive"):
        bt_data.load_intraday_bars(["SPY"], "15m", -5)


def test_last_closed_bar_ts_floors_correctly():
    f = bt_data._last_closed_bar_ts
    assert f(datetime(2026, 5, 14, 9, 32), "5m")  == datetime(2026, 5, 14, 9, 30)
    assert f(datetime(2026, 5, 14, 9, 38), "5m")  == datetime(2026, 5, 14, 9, 35)
    assert f(datetime(2026, 5, 14, 9, 44), "15m") == datetime(2026, 5, 14, 9, 30)
    assert f(datetime(2026, 5, 14, 9, 45), "15m") == datetime(2026, 5, 14, 9, 45)
    assert f(datetime(2026, 5, 14, 10, 59), "1h") == datetime(2026, 5, 14, 10, 0)
    assert f(datetime(2026, 5, 14, 11, 0),  "1h") == datetime(2026, 5, 14, 11, 0)
