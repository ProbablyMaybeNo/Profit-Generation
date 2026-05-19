"""
test_orbo_intraday.py — 5.3.2: promote ORBO to TRACKED_STRATEGIES.

Covers:
  - INTRADAY_ORB_DECLARATIONS shape (5m bar, opening-hour window, 5 symbols)
  - compute_orbo_intraday: OR high/low correctness over 09:30-09:50 window
  - single-shot per day (only one long_entry per session-day)
  - EOD long_exit forces flat at 15:55
  - stop hit (close <= OR_low) triggers long_exit before EOD
  - intraday_fires._in_window accepts list + " ET" tag (5.3.2 plan format)
  - new declaration is included in TRACKED_STRATEGIES and resolves
"""

import sys
from datetime import datetime, time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import config as mcfg  # noqa: E402
from monitoring import intraday_fires as ifires  # noqa: E402
from monitoring import strategy_fires as sfires  # noqa: E402
from strategies.orb import orbo_intraday as oi  # noqa: E402


# ---------------- declaration shape ----------------

def test_orb_declaration_shape():
    decls = mcfg.INTRADAY_ORB_DECLARATIONS
    ids = [d["id"] for d in decls]
    assert "intraday-orbo-5m" in ids
    decl = next(d for d in decls if d["id"] == "intraday-orbo-5m")
    assert decl["id"] == "intraday-orbo-5m"
    assert decl["compute"] == "compute_orbo_intraday"
    assert decl["bar_interval"] == "5m"
    assert decl["active_on"] == ["SPY", "QQQ", "IWM", "NVDA", "TSLA"]
    assert decl["active_in_window"] == ["09:35-10:30 ET"]
    assert decl["grace_period"] is True
    assert decl["pyramidable"] is False
    assert decl["strategy_class"] == "breakout"


def test_orb_in_tracked_strategies():
    ids = [e["id"] for e in mcfg.TRACKED_STRATEGIES]
    assert "intraday-orbo-5m" in ids


def test_orb_compute_fn_resolves():
    fn = sfires._resolve_compute_fn("compute_orbo_intraday")
    assert callable(fn)


def test_orb_surfaces_in_intraday_strategies():
    surfaced = ifires.intraday_strategies(mcfg.TRACKED_STRATEGIES)
    ids = [e["id"] for e in surfaced]
    assert "intraday-orbo-5m" in ids


# ---------------- active_in_window filter ----------------

def test_in_window_list_format_with_et_tag():
    """The plan's "09:35-10:30 ET" inside a list must parse correctly."""
    # 10:00 ET — inside the 09:35-10:30 window
    assert ifires._in_window(datetime(2026, 5, 14, 10, 0),
                              ["09:35-10:30 ET"]) is True
    # 10:45 ET — outside
    assert ifires._in_window(datetime(2026, 5, 14, 10, 45),
                              ["09:35-10:30 ET"]) is False
    # 09:00 ET — before window
    assert ifires._in_window(datetime(2026, 5, 14, 9, 0),
                              ["09:35-10:30 ET"]) is False
    # Empty window list → always active
    assert ifires._in_window(datetime(2026, 5, 14, 14, 0), []) is True
    assert ifires._in_window(datetime(2026, 5, 14, 14, 0), None) is True


def test_in_window_multiple_windows():
    """List with multiple windows: active if `now` is in any one of them."""
    windows = ["09:35-10:30", "13:00-14:00"]
    assert ifires._in_window(datetime(2026, 5, 14, 10, 0), windows) is True
    assert ifires._in_window(datetime(2026, 5, 14, 13, 30), windows) is True
    assert ifires._in_window(datetime(2026, 5, 14, 11, 30), windows) is False


def test_in_window_string_with_et_tag_still_works():
    assert ifires._in_window(datetime(2026, 5, 14, 10, 0),
                              "09:35-10:30 ET") is True
    assert ifires._in_window(datetime(2026, 5, 14, 10, 0),
                              "09:35-10:30") is True


# ---------------- compute_orbo_intraday correctness ----------------

def _bars_for_day(d: str, prices_by_time):
    """Build a 5-min bar frame for a single session day from
    {time_str: close} mapping. open=close, high=close+0.5, low=close-0.5.
    """
    idx = pd.to_datetime([f"{d} {t}" for t in prices_by_time.keys()])
    closes = list(prices_by_time.values())
    return pd.DataFrame({
        "open":   closes,
        "high":   [c + 0.5 for c in closes],
        "low":    [c - 0.5 for c in closes],
        "close":  closes,
        "volume": [10_000.0] * len(closes),
    }, index=idx)


def test_orb_window_builds_or_high_low():
    """The OR over 09:30-09:50 should capture all bars in that window."""
    prices = {
        "09:30": 100.0, "09:35": 101.0, "09:40": 99.5, "09:45": 100.5,
        "09:50": 101.5,  # first bar AFTER window — breakout candidate
    }
    df = _bars_for_day("2026-05-14", prices)
    out = oi.compute_orbo_intraday(df)
    # OR window: 09:30, 09:35, 09:40, 09:45 → low=99.0, high=101.5 (from highs)
    # 09:50 bar: close 101.5, OR_high=101.5 (101.0+0.5). 101.5 > 101.5 is False.
    # No entry. (Edge case proves no false-fire at exact equality.)
    assert not out["long_entry"].iloc[-1]


def test_orb_long_entry_fires_on_first_breakout():
    """Breakout above OR_high after the window → long_entry on that bar."""
    prices = {
        "09:30": 100.0, "09:35": 100.5, "09:40": 100.2, "09:45": 100.3,
        "09:50": 100.4,  # window closed (we use < window_end so 09:50 is post)
        "09:55": 102.0,  # 102 > OR_high (101.0) → ENTRY here
        "10:00": 103.0,  # would also break but single-shot → no re-fire
        "10:05": 102.5,
    }
    df = _bars_for_day("2026-05-14", prices)
    out = oi.compute_orbo_intraday(df)
    entries = out["long_entry"]
    # 09:30..09:45 in window → no entry.
    assert not entries.iloc[0]
    assert not entries.iloc[3]
    # OR_high = max(100.0+0.5, 100.5+0.5, 100.2+0.5, 100.3+0.5) = 101.0
    # 09:50: close 100.4 — not > 101.0, no entry.
    assert not entries.iloc[4]
    # 09:55: close 102 > 101 → ENTRY
    assert entries.iloc[5]
    # 10:00: single-shot → NO second entry
    assert not entries.iloc[6]


def test_orb_long_exit_fires_at_eod():
    prices = {
        "09:30": 100.0, "09:35": 100.5, "09:40": 100.2, "09:45": 100.3,
        "09:55": 102.0,   # entry
        "15:55": 105.0,   # EOD → exit
    }
    df = _bars_for_day("2026-05-14", prices)
    out = oi.compute_orbo_intraday(df)
    assert out["long_entry"].iloc[4]   # 09:55 entry
    assert out["long_exit"].iloc[-1]   # 15:55 EOD exit


def test_orb_stop_exit_on_or_low_break():
    """If close <= OR_low after entry, long_exit fires."""
    prices = {
        "09:30": 100.0, "09:35": 100.5, "09:40": 100.2, "09:45": 100.3,
        "09:55": 102.0,   # entry, OR_low = 99.5 (100.0 - 0.5)
        "10:00": 99.0,    # close 99.0 <= 99.5 → exit
    }
    df = _bars_for_day("2026-05-14", prices)
    out = oi.compute_orbo_intraday(df)
    assert out["long_entry"].iloc[4]
    assert out["long_exit"].iloc[5]


def test_orb_single_entry_per_session_day():
    """Two consecutive trading days: each should be able to have one entry."""
    day1 = {
        "09:30": 100.0, "09:35": 100.5, "09:40": 100.2, "09:45": 100.3,
        "09:55": 102.0,
    }
    day2 = {
        "09:30": 100.0, "09:35": 100.5, "09:40": 100.2, "09:45": 100.3,
        "09:55": 102.0,
    }
    df1 = _bars_for_day("2026-05-14", day1)
    df2 = _bars_for_day("2026-05-15", day2)
    df = pd.concat([df1, df2])
    out = oi.compute_orbo_intraday(df)
    entries = out["long_entry"].astype(int).tolist()
    # Exactly one entry per day.
    assert sum(entries[:5]) == 1
    assert sum(entries[5:]) == 1


def test_orb_no_entry_when_window_never_breaks():
    """Flat session: no bar exceeds OR_high → no entry."""
    prices = {f"{h:02d}:{m:02d}": 100.0
              for h in range(9, 16)
              for m in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)
              if (h, m) >= (9, 30)}
    df = _bars_for_day("2026-05-14", prices)
    out = oi.compute_orbo_intraday(df)
    assert not out["long_entry"].any()


def test_orb_validates_window_order():
    import pytest
    df = _bars_for_day("2026-05-14", {"09:30": 100.0})
    with pytest.raises(ValueError):
        oi.compute_orbo_intraday(
            df,
            or_window_start=time(10, 0),
            or_window_end=time(9, 0),
        )


def test_orb_requires_datetime_index():
    import pytest
    df = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0],
                        "close": [1.0], "volume": [1.0]})
    with pytest.raises(ValueError):
        oi.compute_orbo_intraday(df)
