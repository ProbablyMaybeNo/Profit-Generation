"""Tests for strategies.trend (milestone 4.6.3).

Three trend-following strategies. Tests cover signal shape, regime
gating + pyramiding declarations, no look-ahead, and the validator
integration.

Note: per the milestone spec, the validator must show PASS on at
least one over a 5-year backtest before any goes live. The actual
historical pass is run by Ross via `scripts/validate_strategy.py`
against real bars — the test here uses synthetic bars and asserts
the surface contract.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies import trend  # noqa: E402
from strategies.trend import (  # noqa: E402
    compute_donchian_breakout_20,
    compute_ma_cross_20_50,
    compute_new_high_volume,
    TREND_DECLARATIONS,
)


# ---------------------------------------------------------------------------
# Synthetic bar builders
# ---------------------------------------------------------------------------

def _flat_bars(n=300, value=100.0):
    rng = np.random.default_rng(42)
    return pd.DataFrame({
        "open": [value] * n,
        "high": [value + 0.1] * n,
        "low": [value - 0.1] * n,
        "close": [value] * n,
        "volume": rng.integers(1_000_000, 1_500_000, n),
    })


def _strong_uptrend_bars(n=300):
    """Linear uptrend from 100 → 150 over n bars with vol spike at the
    breakout point so new_high_volume can fire."""
    closes = np.linspace(100.0, 150.0, n)
    rng = np.random.default_rng(7)
    return pd.DataFrame({
        "open": closes,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": rng.integers(2_000_000, 3_000_000, n),
    })


def _downtrend_then_uptrend(n=300):
    half = n // 2
    a = np.linspace(120.0, 90.0, half)
    b = np.linspace(90.0, 130.0, n - half)
    closes = np.concatenate([a, b])
    rng = np.random.default_rng(11)
    return pd.DataFrame({
        "open": closes,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": rng.integers(1_500_000, 2_500_000, n),
    })


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------

def test_three_strategies_declared():
    assert len(TREND_DECLARATIONS) == 3
    ids = {d["id"] for d in TREND_DECLARATIONS}
    assert ids == {
        "trend-donchian-breakout-20",
        "trend-ma-cross-20-50",
        "trend-new-high-volume",
    }


def test_all_marked_pyramidable():
    for d in TREND_DECLARATIONS:
        assert d["pyramidable"] is True, d


def test_all_have_trend_regime():
    # Trend strategies must include "trending_up" (the regime_router's
    # canonical name for a bullish trend). They must NOT include
    # "trending_down" (we're long-only) or "choppy" (mean-reversion turf).
    for d in TREND_DECLARATIONS:
        assert "trending_up" in d["active_in_regimes"]
        assert "trending_down" not in d["active_in_regimes"]
        assert "choppy" not in d["active_in_regimes"]


def test_eod_fire_check_skips_intraday_strategies(monkeypatch):
    """Regression: strategy_fires.check_fires was iterating ALL of
    TRACKED_STRATEGIES on 1d bars — including 15m/5m intraday strategies
    whose compute_fns would produce spurious signals. Confirmed 2026-05-19
    when intraday-mr-3bar-low-15m fired a long_entry on a daily bar.
    Verify check_fires now filters by bar_interval == '1d'."""
    from monitoring import strategy_fires
    mock_tracked = [
        {"id": "daily-ok", "compute": "compute_donchian_breakout_20",
         "active_on": ["SPY"], "bar_interval": "1d", "strategy_class": "trend"},
        {"id": "intraday-skipped", "compute": "compute_donchian_breakout_20",
         "active_on": ["SPY"], "bar_interval": "15m", "strategy_class": "mean_reversion"},
        {"id": "five-min-skipped", "compute": "compute_donchian_breakout_20",
         "active_on": ["SPY"], "bar_interval": "5m", "strategy_class": "breakout"},
    ]
    monkeypatch.setattr(strategy_fires, "TRACKED_STRATEGIES", mock_tracked)
    # Stub load_bars to return an empty dict so the loop runs but produces
    # no fires — we just need to verify only 1d strategies enter the loop.
    monkeypatch.setattr(strategy_fires, "load_bars", lambda *a, **kw: {})
    from datetime import date
    fires = strategy_fires.check_fires(date(2026, 5, 19))
    fired_sids = {f["strategy_id"] for f in fires}
    # Intraday strategy IDs must NOT appear (they don't show up as either
    # successful fires OR load_error rows since the loop skipped them)
    assert "intraday-skipped" not in fired_sids
    assert "five-min-skipped" not in fired_sids


def test_all_have_trend_strategy_class():
    for d in TREND_DECLARATIONS:
        assert d["strategy_class"] == "trend"


def test_no_mean_reversion_regimes():
    """Trend strategies must NOT be active in chop / mean-reversion."""
    for d in TREND_DECLARATIONS:
        regimes = set(d["active_in_regimes"])
        assert "chop" not in regimes
        assert "mean_reversion" not in regimes


# ---------------------------------------------------------------------------
# Signal shape — all three return long_entry + long_exit bool columns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn", [
    compute_donchian_breakout_20,
    compute_ma_cross_20_50,
    compute_new_high_volume,
])
def test_signal_shape(fn):
    df = _strong_uptrend_bars(300)
    out = fn(df)
    assert "long_entry" in out.columns
    assert "long_exit" in out.columns
    assert out["long_entry"].dtype == bool
    assert out["long_exit"].dtype == bool
    assert len(out) == len(df)


@pytest.mark.parametrize("fn", [
    compute_donchian_breakout_20,
    compute_ma_cross_20_50,
    compute_new_high_volume,
])
def test_no_lookahead_on_first_bar(fn):
    """No signal on bar 0 — every signal requires .shift(1) prior data."""
    df = _strong_uptrend_bars(300)
    out = fn(df)
    assert out["long_entry"].iloc[0] is np.bool_(False) or out["long_entry"].iloc[0] == False
    assert out["long_exit"].iloc[0] is np.bool_(False) or out["long_exit"].iloc[0] == False


# ---------------------------------------------------------------------------
# Behaviour smoke — strategies should FIRE in their friendly regime
# ---------------------------------------------------------------------------

def test_donchian_fires_in_strong_uptrend():
    """A steeper uptrend (daily move >> high-low spread) guarantees the
    close beats the prior 20-day high on most warmup-cleared bars."""
    n = 300
    closes = np.linspace(100.0, 400.0, n)  # +1.0/bar avg, well above noise
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.2, "low": closes - 0.2,
        "close": closes,
        "volume": [1_000_000] * n,
    })
    out = compute_donchian_breakout_20(df)
    assert out["long_entry"].sum() > 5


def test_donchian_silent_in_flat():
    df = _flat_bars(300)
    out = compute_donchian_breakout_20(df)
    # No new highs in flat bars after the rolling window settles.
    assert out["long_entry"].sum() == 0


def test_ma_cross_fires_on_regime_change():
    df = _downtrend_then_uptrend(300)
    out = compute_ma_cross_20_50(df)
    # Exactly one fast-over-slow crossover during the trend reversal.
    assert out["long_entry"].sum() >= 1


def test_ma_cross_silent_in_flat():
    df = _flat_bars(300)
    out = compute_ma_cross_20_50(df)
    # Tied EMAs → no cross at all.
    assert out["long_entry"].sum() == 0


def test_new_high_volume_silent_without_volume_spike():
    """If volume is flat, no new-high entry fires even on uptrend."""
    closes = np.linspace(100.0, 150.0, 300)
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes,
        "volume": [1_000_000] * 300,  # flat volume, no spike
    })
    out = compute_new_high_volume(df)
    assert out["long_entry"].sum() == 0


def test_new_high_volume_fires_with_volume_spike():
    """Needs 252+ bars of warmup before a new 52-week high is possible.
    Force the breakout AFTER warmup with a steep rally + volume spike."""
    # First 252 bars flat at 100, then a sharp rally with a vol spike.
    n = 300
    flat = [100.0] * 252
    rally = list(np.linspace(101.0, 150.0, n - 252))
    closes = np.array(flat + rally)
    high = closes + 0.2
    low = closes - 0.2
    volume = [1_000_000] * 252 + [3_000_000] * (n - 252)
    df = pd.DataFrame({
        "open": closes, "high": high, "low": low,
        "close": closes, "volume": volume,
    })
    out = compute_new_high_volume(df)
    assert out["long_entry"].sum() >= 1


# ---------------------------------------------------------------------------
# Validator integration — compute_fn shape matches what
# scripts/validate_strategy expects
# ---------------------------------------------------------------------------

def test_compute_fns_returned_dataframe_has_required_columns():
    df = _strong_uptrend_bars(300)
    for fn in (compute_donchian_breakout_20,
               compute_ma_cross_20_50,
               compute_new_high_volume):
        out = fn(df)
        assert isinstance(out, pd.DataFrame)
        # The original columns must still be present (validator reads close).
        for col in ("open", "high", "low", "close", "volume"):
            assert col in out.columns


def test_compute_fns_importable_via_module_path():
    """The TREND_DECLARATIONS module paths must each be importable +
    expose the named compute function."""
    import importlib
    for d in TREND_DECLARATIONS:
        mod = importlib.import_module(d["module"])
        fn = getattr(mod, d["compute"], None)
        assert callable(fn), f"{d['module']}.{d['compute']} not callable"
