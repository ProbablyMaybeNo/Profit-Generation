"""7.5.5 — 1-minute-native strategies (ORB / momentum / VWAP-reclaim).

Validates:
  - INTRADAY_1M_DECLARATIONS registered in TRACKED_STRATEGIES.
  - All three declarations have bar_interval="1m" and max_position_usd=200.
  - Each compute_fn resolves through monitoring.strategy_fires._resolve_compute_fn.
  - ORB-1m: defines opening range from first 5 minutes; entry on first
    breakout thereafter; single-shot per day.
  - Momentum-1m: 3 consecutive closes above rising EMA20 + rvol > 1.5.
  - VWAP-reclaim: dip below VWAP, cross back above with rvol > 1.0.
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring.config import (  # noqa: E402
    INTRADAY_1M_DECLARATIONS,
    TRACKED_STRATEGIES,
)
from monitoring.strategy_fires import _resolve_compute_fn  # noqa: E402
from strategies.intraday.momentum_1m import compute_intraday_1m_momentum  # noqa: E402
from strategies.intraday.orb_1m import compute_intraday_1m_orb  # noqa: E402
from strategies.intraday.vwap_reclaim_1m import (  # noqa: E402
    compute_intraday_1m_vwap_reclaim,
)


# ---------------------------------------------------------------------------
# 1. Registration in TRACKED_STRATEGIES + resolver
# ---------------------------------------------------------------------------

def test_three_1m_declarations_present():
    ids = {d["id"] for d in INTRADAY_1M_DECLARATIONS}
    expected = {
        "intraday-1m-orb",
        "intraday-1m-momentum",
        "intraday-1m-vwap-reclaim",
    }
    assert ids == expected


def test_declarations_are_1m_with_capped_position():
    for d in INTRADAY_1M_DECLARATIONS:
        assert d["bar_interval"] == "1m"
        assert d["max_position_usd"] == 200
        assert d.get("grace_period") is True
        assert d.get("pyramidable") is False


def test_declarations_merged_into_tracked_strategies():
    tracked_ids = {
        d["id"] for d in TRACKED_STRATEGIES if isinstance(d, dict)
    }
    for d in INTRADAY_1M_DECLARATIONS:
        assert d["id"] in tracked_ids, f"{d['id']} missing from TRACKED_STRATEGIES"


def test_all_three_compute_fns_resolve():
    """The static module list update means each compute_fn resolves."""
    for d in INTRADAY_1M_DECLARATIONS:
        fn = _resolve_compute_fn(d["compute"])
        assert callable(fn)


# ---------------------------------------------------------------------------
# 2. ORB-1m compute logic
# ---------------------------------------------------------------------------

def _bar(t, *, open_, high, low, close, volume=1000):
    return {"open": open_, "high": high, "low": low,
            "close": close, "volume": volume, "ts": t}


def _ts(minute_offset):
    """Build a 1-minute timestamp starting at 09:30 ET on 2026-05-22."""
    base = pd.Timestamp("2026-05-22 09:30:00")
    return base + pd.Timedelta(minutes=minute_offset)


def _build_1m_df(rows):
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(df["ts"])
    return df.drop(columns=["ts"])


def test_orb_1m_no_fire_during_opening_window():
    """Bars inside 09:30-09:35 ET build the range; no entry yet."""
    rows = []
    for minute in range(5):
        t = _ts(minute)
        rows.append(_bar(t, open_=100, high=101, low=99, close=100.5))
    df = _build_1m_df(rows)
    out = compute_intraday_1m_orb(df)
    assert not out["long_entry"].any()


def test_orb_1m_fires_on_first_breakout_after_window():
    rows = []
    # Range: 09:30-09:34 → high=101
    for minute in range(5):
        t = _ts(minute)
        rows.append(_bar(t, open_=100, high=101, low=99, close=100.5))
    # 09:35 — close 100.5 < 101 → no entry
    rows.append(_bar(_ts(5),
                     open_=100.5, high=100.8, low=100.4, close=100.5))
    # 09:36 — close 101.5 > 101 → ENTRY
    rows.append(_bar(_ts(6),
                     open_=100.5, high=102.0, low=100.5, close=101.5))
    # 09:37 — another breakout → NOT re-fire (single-shot)
    rows.append(_bar(_ts(7),
                     open_=101.5, high=102.5, low=101.5, close=102.0))
    df = _build_1m_df(rows)
    out = compute_intraday_1m_orb(df)
    entry_indices = list(out.index[out["long_entry"]])
    assert len(entry_indices) == 1
    assert entry_indices[0] == pd.Timestamp("2026-05-22 09:36:00")


def test_orb_1m_eod_exit_fires():
    rows = []
    for minute in range(5):
        t = _ts(minute)
        rows.append(_bar(t, open_=100, high=101, low=99, close=100.5))
    # Entry at 09:36
    rows.append(_bar(_ts(6),
                     open_=100.5, high=102.0, low=100.5, close=101.5))
    # 15:55 — eod_exit (385 minutes after 09:30)
    rows.append(_bar(_ts(385),
                     open_=101.5, high=101.7, low=101.4, close=101.5))
    df = _build_1m_df(rows)
    out = compute_intraday_1m_orb(df)
    # EOD bar should have long_exit=True
    eod_idx = pd.Timestamp("2026-05-22 15:55:00")
    assert bool(out.loc[eod_idx, "long_exit"])


# ---------------------------------------------------------------------------
# 3. Momentum-1m compute logic
# ---------------------------------------------------------------------------

def test_momentum_1m_fires_on_three_consec_above_rising_ema_with_rvol():
    """Build a series where prices rise steadily for ~30 bars to seed the
    EMA, then a final 3-bar high-volume continuation triggers entry."""
    rows = []
    base = 100.0
    for i in range(30):
        # Rising trend so EMA20 trends up.
        price = base + i * 0.3
        t = _ts(i)
        rows.append(_bar(t, open_=price, high=price + 0.1, low=price - 0.1,
                         close=price, volume=1000))
    # 3 last bars: above-trend price + 3x volume (rvol > 1.5)
    for j, vol in enumerate([3000, 3500, 4000]):
        t = _ts(30 + j)
        price = base + 30 * 0.3 + (j + 1) * 0.5
        rows.append(_bar(t, open_=price, high=price + 0.2, low=price - 0.1,
                         close=price, volume=vol))
    df = _build_1m_df(rows)
    out = compute_intraday_1m_momentum(df)
    assert out["long_entry"].any()


def test_momentum_1m_no_fire_without_rvol():
    """Without elevated volume, the consec-above-EMA condition alone doesn't fire."""
    rows = []
    for i in range(40):
        price = 100.0 + i * 0.3
        t = _ts(i)
        rows.append(_bar(t, open_=price, high=price + 0.1, low=price - 0.1,
                         close=price, volume=1000))
    df = _build_1m_df(rows)
    out = compute_intraday_1m_momentum(df)
    # rvol == 1.0 across the run (constant volume), threshold is 1.5 → no fire.
    assert not out["long_entry"].any()


def test_momentum_1m_exit_on_close_below_ema():
    """Exit fires when close drops back below EMA."""
    rows = []
    # Long uptrend
    for i in range(30):
        price = 100.0 + i * 0.5
        t = _ts(i)
        rows.append(_bar(t, open_=price, high=price + 0.1, low=price - 0.1,
                         close=price, volume=1000))
    # Then sudden drop way below EMA
    rows.append(_bar(_ts(30),
                     open_=110, high=110, low=90, close=90.0, volume=1000))
    df = _build_1m_df(rows)
    out = compute_intraday_1m_momentum(df)
    # The drop bar should have long_exit=True (close=90 < EMA ~ 107).
    assert bool(out.iloc[-1]["long_exit"])


# ---------------------------------------------------------------------------
# 4. VWAP-reclaim compute logic
# ---------------------------------------------------------------------------

def test_vwap_reclaim_fires_on_cross_back_above():
    """Price starts above VWAP, dips below, crosses back above with rvol."""
    rows = []
    # Bars above VWAP
    for i in range(5):
        t = _ts(i)
        rows.append(_bar(t, open_=100, high=101, low=99.5, close=100.5,
                         volume=1000))
    # Dip below VWAP for several bars
    for i in range(5):
        t = _ts(5 + i)
        rows.append(_bar(t, open_=99, high=99.5, low=98.5, close=99.0,
                         volume=1000))
    # Cross back above with rvol > 1.0
    rows.append(_bar(_ts(10),
                     open_=99.5, high=102.0, low=99.5, close=101.5,
                     volume=2500))
    df = _build_1m_df(rows)
    out = compute_intraday_1m_vwap_reclaim(df, rvol_lookback=5)
    # Cross bar at minute 10 (09:40) fires.
    cross_idx = pd.Timestamp("2026-05-22 09:40:00")
    assert bool(out.loc[cross_idx, "long_entry"])


def test_vwap_reclaim_no_fire_without_prior_dip():
    """Price stays above VWAP — no reclaim event."""
    rows = []
    for i in range(20):
        t = _ts(i)
        # Constantly above any reasonable VWAP
        rows.append(_bar(t, open_=100, high=101, low=99.5, close=100.5,
                         volume=1000))
    df = _build_1m_df(rows)
    out = compute_intraday_1m_vwap_reclaim(df)
    assert not out["long_entry"].any()


def test_vwap_column_present_in_output():
    rows = []
    for i in range(5):
        t = _ts(i)
        rows.append(_bar(t, open_=100, high=101, low=99, close=100,
                         volume=1000))
    df = _build_1m_df(rows)
    out = compute_intraday_1m_vwap_reclaim(df)
    assert "vwap" in out.columns


# ---------------------------------------------------------------------------
# 5. Smoke test — full pipeline integration via _resolve_compute_fn
# ---------------------------------------------------------------------------

def test_smoke_resolves_and_runs_each_1m_compute_fn():
    """End-to-end: resolver returns the fn, fn accepts a tiny 1m df."""
    rows = []
    for i in range(15):
        t = _ts(i)
        rows.append(_bar(t, open_=100, high=101, low=99, close=100,
                         volume=1000))
    df = _build_1m_df(rows)
    for d in INTRADAY_1M_DECLARATIONS:
        fn = _resolve_compute_fn(d["compute"])
        out = fn(df)
        assert "long_entry" in out.columns
        assert "long_exit" in out.columns
        assert len(out) == len(df)


def test_empty_df_does_not_crash():
    """Empty input → empty output, no exception."""
    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df.index = pd.DatetimeIndex([])
    for fn in (compute_intraday_1m_orb, compute_intraday_1m_momentum,
               compute_intraday_1m_vwap_reclaim):
        out = fn(df)
        assert len(out) == 0
        assert "long_entry" in out.columns
        assert "long_exit" in out.columns
