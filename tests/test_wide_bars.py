import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import wide_bars  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Reroute the config.cache sqlite file to a tmp location per test."""
    from config import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_FILE", tmp_path / "cache.db")
    yield


def _make_bars(symbol: str, n: int = 100, base: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(end="2026-05-19", periods=n, freq="B")
    closes = np.linspace(base, base * 1.1, n)
    return pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1_000_000),
    }, index=idx)


# ---------------------------------------------------------------------------
# Batch sizing
# ---------------------------------------------------------------------------


def test_fetch_wide_daily_bars_batches_by_size():
    syms = [f"S{i}" for i in range(125)]
    calls = []

    def fake_fetcher(batch, *, lookback_bars, as_of=None):
        calls.append(tuple(batch))
        return {s: _make_bars(s) for s in batch}

    out = wide_bars.fetch_wide_daily_bars(
        syms, batch_size=50, alpaca_fetcher=fake_fetcher,
        as_of=datetime(2026, 5, 19, 17, 0),
    )
    # 125 / 50 → 3 batches of 50, 50, 25
    assert [len(b) for b in calls] == [50, 50, 25]
    assert len(out) == 125


def test_fetch_wide_daily_bars_dedupes_symbols():
    calls = []

    def fake_fetcher(batch, *, lookback_bars, as_of=None):
        calls.append(tuple(batch))
        return {s: _make_bars(s) for s in batch}

    out = wide_bars.fetch_wide_daily_bars(
        ["AAPL", "aapl", "AAPL", "MSFT"],
        batch_size=10, alpaca_fetcher=fake_fetcher,
        as_of=datetime(2026, 5, 19, 17, 0),
    )
    assert calls == [("AAPL", "MSFT")]
    assert sorted(out.keys()) == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_fetch_wide_daily_bars_cache_hit_skips_refetch():
    calls = []

    def fake_fetcher(batch, *, lookback_bars, as_of=None):
        calls.append(tuple(batch))
        return {s: _make_bars(s) for s in batch}

    as_of = datetime(2026, 5, 19, 17, 0)
    out1 = wide_bars.fetch_wide_daily_bars(
        ["AAPL", "MSFT"], alpaca_fetcher=fake_fetcher, as_of=as_of,
    )
    out2 = wide_bars.fetch_wide_daily_bars(
        ["AAPL", "MSFT"], alpaca_fetcher=fake_fetcher, as_of=as_of,
    )
    # Second call was a cache hit — fetcher not invoked
    assert len(calls) == 1
    assert sorted(out1.keys()) == ["AAPL", "MSFT"]
    assert sorted(out2.keys()) == ["AAPL", "MSFT"]


def test_fetch_wide_daily_bars_cache_miss_for_new_symbol():
    calls = []

    def fake_fetcher(batch, *, lookback_bars, as_of=None):
        calls.append(tuple(batch))
        return {s: _make_bars(s) for s in batch}

    as_of = datetime(2026, 5, 19, 17, 0)
    wide_bars.fetch_wide_daily_bars(
        ["AAPL"], alpaca_fetcher=fake_fetcher, as_of=as_of,
    )
    wide_bars.fetch_wide_daily_bars(
        ["AAPL", "MSFT"], alpaca_fetcher=fake_fetcher, as_of=as_of,
    )
    # Two calls — first for AAPL, second for just MSFT (AAPL cached)
    assert len(calls) == 2
    assert "AAPL" in calls[0]
    assert calls[1] == ("MSFT",)


def test_fetch_wide_daily_bars_bypass_cache_refetches():
    calls = []

    def fake_fetcher(batch, *, lookback_bars, as_of=None):
        calls.append(tuple(batch))
        return {s: _make_bars(s) for s in batch}

    as_of = datetime(2026, 5, 19, 17, 0)
    wide_bars.fetch_wide_daily_bars(
        ["AAPL"], alpaca_fetcher=fake_fetcher, as_of=as_of,
    )
    wide_bars.fetch_wide_daily_bars(
        ["AAPL"], alpaca_fetcher=fake_fetcher, as_of=as_of,
        bypass_cache=True,
    )
    assert len(calls) == 2


def test_fetch_wide_daily_bars_cache_keyed_to_bar_close_date():
    """Different bar-close dates should be separate cache entries."""
    calls = []

    def fake_fetcher(batch, *, lookback_bars, as_of=None):
        calls.append(tuple(batch))
        return {s: _make_bars(s) for s in batch}

    # Call 1: pre-close (16:00) — uses prior day as bar_date
    wide_bars.fetch_wide_daily_bars(
        ["AAPL"], alpaca_fetcher=fake_fetcher,
        as_of=datetime(2026, 5, 19, 16, 0),
    )
    # Call 2: post-close (17:00) — uses today as bar_date → cache miss
    wide_bars.fetch_wide_daily_bars(
        ["AAPL"], alpaca_fetcher=fake_fetcher,
        as_of=datetime(2026, 5, 19, 17, 0),
    )
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_fetch_wide_daily_bars_batch_failure_falls_back_to_single():
    calls = []

    def fake_fetcher(batch, *, lookback_bars, as_of=None):
        calls.append(tuple(batch))
        if len(batch) > 1:
            raise RuntimeError("batch failed")
        return {batch[0]: _make_bars(batch[0])}

    out = wide_bars.fetch_wide_daily_bars(
        ["AAPL", "MSFT", "NVDA"], batch_size=10,
        alpaca_fetcher=fake_fetcher,
        as_of=datetime(2026, 5, 19, 17, 0),
    )
    # First call attempted batched, then 3 single-symbol fallbacks
    assert len(calls) == 4
    assert sorted(out.keys()) == ["AAPL", "MSFT", "NVDA"]


def test_fetch_wide_daily_bars_single_symbol_failure_silently_dropped():
    def fake_fetcher(batch, *, lookback_bars, as_of=None):
        if len(batch) > 1:
            raise RuntimeError("batch failed")
        if batch[0] == "BAD":
            raise RuntimeError("symbol failed")
        return {batch[0]: _make_bars(batch[0])}

    out = wide_bars.fetch_wide_daily_bars(
        ["AAPL", "BAD", "MSFT"], batch_size=10,
        alpaca_fetcher=fake_fetcher,
        as_of=datetime(2026, 5, 19, 17, 0),
    )
    assert sorted(out.keys()) == ["AAPL", "MSFT"]


def test_fetch_wide_daily_bars_empty_input():
    out = wide_bars.fetch_wide_daily_bars(
        [], alpaca_fetcher=lambda batch, **kw: {},
    )
    assert out == {}


# ---------------------------------------------------------------------------
# Bar-close date logic
# ---------------------------------------------------------------------------


def test_last_completed_close_before_market_close():
    out = wide_bars._last_completed_close(datetime(2026, 5, 19, 10, 0))
    assert out == "2026-05-18"


def test_last_completed_close_after_market_close():
    out = wide_bars._last_completed_close(datetime(2026, 5, 19, 17, 0))
    assert out == "2026-05-19"


def test_last_completed_close_exactly_at_close_minute():
    # 16:30 ET == bar close minute → consider closed
    out = wide_bars._last_completed_close(datetime(2026, 5, 19, 16, 30))
    assert out == "2026-05-19"
