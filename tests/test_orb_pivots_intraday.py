"""
test_orb_pivots_intraday.py — 5.3.3: promote orb_pivots to TRACKED_STRATEGIES.

Covers:
  - INTRADAY_ORB_DECLARATIONS includes the pivots entry alongside ORBO
  - declaration shape (5m bar, opening-hour window, 5 symbols, grace, no-pyramid)
  - compute_fn resolves through monitoring.strategy_fires._resolve_compute_fn
  - prior-day H/L/C derivation from intraday bars
  - pivot R1 confirmation gates entries
  - single-shot per session-day
  - EOD long_exit, prior-day-low stop exit
  - intraday_fires.intraday_strategies surfaces both ORB entries
"""

import sys
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import config as mcfg  # noqa: E402
from monitoring import intraday_fires as ifires  # noqa: E402
from monitoring import strategy_fires as sfires  # noqa: E402
from strategies.orb import orb_pivots_intraday as opi  # noqa: E402


# ---------------- declaration ----------------

def test_orb_pivots_declaration_shape():
    decls = mcfg.INTRADAY_ORB_DECLARATIONS
    ids = [d["id"] for d in decls]
    assert "intraday-orb-pivots-5m" in ids
    decl = next(d for d in decls if d["id"] == "intraday-orb-pivots-5m")
    assert decl["compute"] == "compute_orb_pivots_intraday"
    assert decl["bar_interval"] == "5m"
    assert decl["active_on"] == ["SPY", "QQQ", "IWM", "NVDA", "TSLA"]
    assert decl["active_in_window"] == ["09:35-10:30 ET"]
    assert decl["grace_period"] is True
    assert decl["pyramidable"] is False
    assert decl["strategy_class"] == "breakout"


def test_orb_pivots_in_tracked_strategies():
    ids = [e["id"] for e in mcfg.TRACKED_STRATEGIES]
    assert "intraday-orb-pivots-5m" in ids


def test_orb_pivots_compute_fn_resolves():
    fn = sfires._resolve_compute_fn("compute_orb_pivots_intraday")
    assert callable(fn)


def test_both_orb_variants_surface_in_intraday_strategies():
    surfaced = ifires.intraday_strategies(mcfg.TRACKED_STRATEGIES)
    ids = [e["id"] for e in surfaced]
    assert "intraday-orbo-5m" in ids
    assert "intraday-orb-pivots-5m" in ids


# ---------------- pivot math ----------------

def _two_day_frame(day1_close=100.0, day1_range=(99.0, 101.0),
                    day2_bars=None):
    """Build a 2-day 5m frame. Day 1 is a single representative bar set
    that yields pdh/pdl/pdc; day 2 is the test target.

    day2_bars: list of (HHMM_str, open, high, low, close)
    """
    rows = []
    rows.append(("2026-05-13 09:30", 100.0, day1_range[1], day1_range[0], day1_close))
    rows.append(("2026-05-13 09:35", 100.0, day1_range[1], day1_range[0], day1_close))
    rows.append(("2026-05-13 09:40", 100.0, day1_range[1], day1_range[0], day1_close))
    rows.append(("2026-05-13 15:55", 100.0, day1_range[1], day1_range[0], day1_close))
    for t, o, h, l, c in day2_bars or []:
        rows.append((f"2026-05-14 {t}", o, h, l, c))
    idx = pd.to_datetime([r[0] for r in rows])
    df = pd.DataFrame({
        "open":   [r[1] for r in rows],
        "high":   [r[2] for r in rows],
        "low":    [r[3] for r in rows],
        "close":  [r[4] for r in rows],
        "volume": [10_000.0] * len(rows),
    }, index=idx)
    return df


def test_prior_day_hlc_is_correct():
    """Day 2 rows should carry day 1's H/L/C as pdh/pdl/pdc."""
    df = _two_day_frame(
        day1_close=100.0, day1_range=(99.0, 101.0),
        day2_bars=[("09:30", 100.0, 100.5, 99.5, 100.0)],
    )
    out = opi.compute_orb_pivots_intraday(df)
    # Day 1 rows: pdh/pdl/pdc NaN (no prior day)
    assert np.isnan(out["pdh"].iloc[0])
    # Day 2 row: pdh=101, pdl=99, pdc=100
    day2_idx = out.index.get_loc(pd.Timestamp("2026-05-14 09:30"))
    assert out["pdh"].iloc[day2_idx] == 101.0
    assert out["pdl"].iloc[day2_idx] == 99.0
    assert out["pdc"].iloc[day2_idx] == 100.0
    # R1 = 2*P - pdl = 2*100 - 99 = 101
    assert out["R1"].iloc[day2_idx] == 101.0


def test_entry_fires_when_r1_confirms():
    """OR_high=100.5. R1=101>OR_high. Bar open<OR_high<bar high → ENTRY."""
    df = _two_day_frame(
        day1_close=100.0, day1_range=(99.0, 101.0),
        day2_bars=[
            ("09:30", 99.5,  100.5, 99.5,  100.0),
            ("09:35", 100.0, 100.5, 99.7,  100.0),
            ("09:40", 100.0, 100.5, 99.7,  100.0),
            ("09:50", 100.0, 101.0, 99.8,  101.0),  # breakout + R1 confirms
            ("10:00", 101.0, 102.0, 100.5, 102.0),  # would re-fire — single-shot blocks
        ],
    )
    out = opi.compute_orb_pivots_intraday(df)
    entries = out["long_entry"]
    # 09:50 is the entry
    idx_entry = out.index.get_loc(pd.Timestamp("2026-05-14 09:50"))
    assert entries.iloc[idx_entry]
    # 10:00 is NOT a re-fire
    idx_next = out.index.get_loc(pd.Timestamp("2026-05-14 10:00"))
    assert not entries.iloc[idx_next]


def test_no_entry_when_r1_does_not_confirm():
    """OR_high so high that R1 < OR_high → no entry even on breakout."""
    df = _two_day_frame(
        day1_close=100.0, day1_range=(99.0, 100.5),  # R1 = 200/3*2 - 99 ≈ 100.33
        day2_bars=[
            ("09:30", 99.5,  105.0, 99.5,  100.0),   # huge OR top = 105
            ("09:35", 100.0, 105.0, 99.7,  100.0),
            ("09:40", 100.0, 105.0, 99.7,  100.0),
            ("09:50", 100.0, 106.0, 99.8,  106.0),   # breakout but R1<<OR_high
        ],
    )
    out = opi.compute_orb_pivots_intraday(df)
    # OR_high = 105. R1 ~100.33. R1 NOT > or_high → no entry.
    assert not out["long_entry"].any()


def test_no_entry_when_bar_open_already_above_or_high():
    """If bar.open >= or_high, the rule says no entry (gap-open suppression)."""
    df = _two_day_frame(
        day1_close=100.0, day1_range=(99.0, 101.0),
        day2_bars=[
            ("09:30", 99.5,  100.5, 99.5, 100.0),
            ("09:35", 100.0, 100.5, 99.7, 100.0),
            ("09:40", 100.0, 100.5, 99.7, 100.0),
            ("09:50", 101.0, 102.0, 100.8, 102.0),   # open 101 > or_high 100.5
        ],
    )
    out = opi.compute_orb_pivots_intraday(df)
    assert not out["long_entry"].any()


def test_eod_exit_fires():
    df = _two_day_frame(
        day1_close=100.0, day1_range=(99.0, 101.0),
        day2_bars=[
            ("09:30", 99.5,  100.5, 99.5,  100.0),
            ("09:35", 100.0, 100.5, 99.7,  100.0),
            ("09:40", 100.0, 100.5, 99.7,  100.0),
            ("09:50", 100.0, 101.0, 99.8,  101.0),
            ("15:55", 101.0, 101.0, 100.5, 100.5),  # EOD
        ],
    )
    out = opi.compute_orb_pivots_intraday(df)
    idx_eod = out.index.get_loc(pd.Timestamp("2026-05-14 15:55"))
    assert out["long_exit"].iloc[idx_eod]


def test_stop_exit_on_prior_day_low_break():
    """After entry, if close <= pdl (99.0), long_exit fires."""
    df = _two_day_frame(
        day1_close=100.0, day1_range=(99.0, 101.0),
        day2_bars=[
            ("09:30", 99.5,  100.5, 99.5,  100.0),
            ("09:35", 100.0, 100.5, 99.7,  100.0),
            ("09:40", 100.0, 100.5, 99.7,  100.0),
            ("09:50", 100.0, 101.0, 99.8,  101.0),  # entry
            ("10:00", 101.0, 101.0, 98.0,  98.5),   # close 98.5 <= pdl 99
        ],
    )
    out = opi.compute_orb_pivots_intraday(df)
    idx_stop = out.index.get_loc(pd.Timestamp("2026-05-14 10:00"))
    assert out["long_exit"].iloc[idx_stop]


def test_empty_frame_returns_empty_columns():
    df = pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []},
                      index=pd.DatetimeIndex([]))
    out = opi.compute_orb_pivots_intraday(df)
    assert "long_entry" in out.columns
    assert "long_exit" in out.columns
    assert len(out) == 0


def test_validates_inputs():
    import pytest
    df = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0],
                        "close": [1.0], "volume": [1.0]})
    with pytest.raises(ValueError):
        opi.compute_orb_pivots_intraday(df)  # no DatetimeIndex
    df.index = pd.date_range("2026-01-01 09:30", periods=1, freq="5min")
    with pytest.raises(ValueError):
        opi.compute_orb_pivots_intraday(
            df,
            or_window_start=time(10, 0),
            or_window_end=time(9, 0),
        )
