"""test_excursion.py — M1: MFE/MAE computation from a bar series."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import excursion  # noqa: E402


def _bars(*hl, ts_prefix="2026-05-14T"):
    out = []
    for i, (h, l) in enumerate(hl):
        out.append({"ts": f"{ts_prefix}{14 + i:02d}:00:00", "high": h, "low": l})
    return out


def test_mfe_mae_long_basic():
    # entry 100; highest high 110 (+10%), lowest low 95 (-5%).
    bars = _bars((105, 99), (110, 97), (108, 95))
    mfe, mae = excursion.compute_mfe_mae(bars, entry_price=100.0, side="long")
    assert mfe == pytest.approx(0.10)
    assert mae == pytest.approx(-0.05)


def test_mfe_mae_long_all_adverse():
    # Position only ever went down: MFE small (or 0), MAE negative.
    bars = _bars((100.5, 98), (99, 95), (97, 92))
    mfe, mae = excursion.compute_mfe_mae(bars, entry_price=100.0, side="long")
    assert mfe == pytest.approx(0.005)
    assert mae == pytest.approx(-0.08)


def test_mfe_mae_short_mirrored():
    # Short entry 100: favorable = price falls; lowest low 95 -> mfe +5%,
    # highest high 110 -> mae -10%.
    bars = _bars((105, 99), (110, 97), (108, 95))
    mfe, mae = excursion.compute_mfe_mae(bars, entry_price=100.0, side="short")
    assert mfe == pytest.approx(0.05)
    assert mae == pytest.approx(-0.10)


def test_mfe_mae_window_filters_bars():
    # Bars outside [entry_ts, exit_ts] are ignored.
    bars = [
        {"ts": "2026-05-14T13:00:00", "high": 999, "low": 1},   # before entry
        {"ts": "2026-05-14T14:30:00", "high": 110, "low": 98},  # in window
        {"ts": "2026-05-14T16:00:00", "high": 500, "low": 50},  # after exit
    ]
    mfe, mae = excursion.compute_mfe_mae(
        bars, entry_price=100.0,
        entry_ts="2026-05-14T14:00:00", exit_ts="2026-05-14T15:00:00",
        side="long",
    )
    assert mfe == pytest.approx(0.10)
    assert mae == pytest.approx(-0.02)


def test_mfe_mae_no_bars_returns_none():
    assert excursion.compute_mfe_mae([], entry_price=100.0) == (None, None)
    assert excursion.compute_mfe_mae(None, entry_price=100.0) == (None, None)


def test_mfe_mae_degenerate_entry_returns_none():
    bars = _bars((110, 90))
    assert excursion.compute_mfe_mae(bars, entry_price=0) == (None, None)
    assert excursion.compute_mfe_mae(bars, entry_price=None) == (None, None)


def test_mfe_mae_pandas_input():
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame(
        [(105, 99), (110, 95)], columns=["High", "Low"],
    )
    mfe, mae = excursion.compute_mfe_mae(df, entry_price=100.0, side="long")
    assert mfe == pytest.approx(0.10)
    assert mae == pytest.approx(-0.05)
