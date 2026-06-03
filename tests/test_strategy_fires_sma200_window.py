"""F1 (audit 2026-06-03) — rsi2/rsi14-oversold can never fire because the
check_fires bar-load window was too short to compute a 200-bar SMA.

These are WIRING tests: they drive the real `strategy_fires.check_fires`
entry point (and the real rsi2-oversold compute_fn) and assert against the
window that check_fires actually requests from load_bars. With the old
120-day window the synthetic history is too short -> sma200 is all-NaN ->
long_entry is never True, so `test_rsi2_can_fire_through_check_fires` FAILS.
"""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from monitoring import strategy_fires


def _business_day_index(start_iso: str, end_iso: str) -> pd.DatetimeIndex:
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return pd.bdate_range(start=start, end=end)


def test_check_fires_loads_enough_history_for_sma200(monkeypatch):
    """The window check_fires requests from load_bars must contain >=200
    trading bars so a 200-bar SMA is non-NaN on the latest bar."""
    captured = {}

    def fake_load_bars(symbols, start, end, interval="1d", source="yf"):
        idx = _business_day_index(start, end)
        captured["bars"] = len(idx)
        out = {}
        for s in symbols:
            df = pd.DataFrame(
                {
                    "open": np.linspace(100, 110, len(idx)),
                    "high": np.linspace(101, 111, len(idx)),
                    "low": np.linspace(99, 109, len(idx)),
                    "close": np.linspace(100, 110, len(idx)),
                    "volume": np.full(len(idx), 1_000_000),
                },
                index=idx,
            )
            out[s] = df
        return out

    mock_tracked = [
        {
            "id": "rsi2-oversold",
            "compute": "compute_rsi2_oversold",
            "strategy_class": "mean_reversion",
            "active_on": ["SPY"],
            "bar_interval": "1d",
        }
    ]
    monkeypatch.setattr(strategy_fires, "TRACKED_STRATEGIES", mock_tracked)
    monkeypatch.setattr(strategy_fires, "load_bars", fake_load_bars)

    fires = strategy_fires.check_fires(date(2026, 6, 3))

    # The actual bars handed to the compute_fn must support a 200-bar SMA.
    assert captured["bars"] >= 200, (
        f"check_fires loaded only {captured['bars']} trading bars; "
        "a 200-bar SMA strategy (rsi2/rsi14-oversold) can never fire"
    )
    # And the SMA200 on that exact loaded series is non-NaN on the last bar.
    idx = _business_day_index(
        (date(2026, 6, 3) - timedelta(days=320)).isoformat(),
        (date(2026, 6, 3) + timedelta(days=1)).isoformat(),
    )
    sma200 = pd.Series(np.linspace(100, 110, len(idx))).rolling(200).mean()
    assert not np.isnan(sma200.iloc[-1])
    # No load/compute errors surfaced.
    assert not any("error" in f for f in fires), fires


def test_rsi2_can_fire_through_check_fires(monkeypatch):
    """End-to-end: a constructed series with an rsi2 oversold setup on the
    latest bar produces a real fire through check_fires + the real
    compute_rsi2_oversold. FAILS under the old 120-day window because
    sma200 is all-NaN (close > sma200 gate can never be True)."""

    def fake_load_bars(symbols, start, end, interval="1d", source="yf"):
        idx = _business_day_index(start, end)
        n = len(idx)
        # Long uptrend so the latest close sits well above its 200-bar SMA,
        # then a sharp 3-bar drop to drive RSI(2) below 10 on the last bar
        # (long_entry = rsi2 < 10 AND close > sma200).
        close = np.linspace(100.0, 200.0, n)
        if n >= 4:
            close[-3] = close[-4] - 3.0
            close[-2] = close[-3] - 3.0
            close[-1] = close[-2] - 3.0
        out = {}
        for s in symbols:
            out[s] = pd.DataFrame(
                {
                    "open": close,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": np.full(n, 1_000_000),
                },
                index=idx,
            )
        return out

    mock_tracked = [
        {
            "id": "rsi2-oversold",
            "compute": "compute_rsi2_oversold",
            "strategy_class": "mean_reversion",
            "active_on": ["SPY"],
            "bar_interval": "1d",
        }
    ]
    monkeypatch.setattr(strategy_fires, "TRACKED_STRATEGIES", mock_tracked)
    monkeypatch.setattr(strategy_fires, "load_bars", fake_load_bars)

    fires = strategy_fires.check_fires(date(2026, 6, 3))
    rsi2 = [f for f in fires if f["strategy_id"] == "rsi2-oversold"]
    assert rsi2, "rsi2-oversold produced no result row at all"
    assert rsi2[0].get("fired") is True, (
        "rsi2-oversold did not fire — 200-bar SMA likely starved by a too-short "
        f"load window (row={rsi2[0]})"
    )
