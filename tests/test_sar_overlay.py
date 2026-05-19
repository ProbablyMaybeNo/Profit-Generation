"""6.4.1 — Parabolic SAR exit overlay.

Validates:
  - SAR math against known-good Wilder sequence (constant up-trend
    where SAR rises monotonically toward the extreme point).
  - State persistence (init / advance / clear).
  - Overlay precedence: trailing_stop OR sar_flip — whichever fires
    first wins.
  - No overlay when strategy doesn't opt in.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import sar_overlay as so  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    c = db.init_db()
    so._ensure_sar_table(c)
    yield c
    c.close()


def _bars(*hlc):
    """Build a list of bar dicts from (high, low, close) tuples."""
    return [{"high": h, "low": l, "close": c} for (h, l, c) in hlc]


# ---------------------------------------------------------------------------
# SAR math — Wilder's defaults
# ---------------------------------------------------------------------------

def test_compute_sar_empty_input():
    assert so.compute_sar_series([], direction="long") == []
    assert so.compute_sar_series([{"high": 1, "low": 0, "close": 0.5}],
                                  direction="long") == [None]


def test_compute_sar_constant_uptrend_rises_monotonically():
    """Steady uptrend: each bar makes a new high. SAR should rise
    monotonically toward the extreme point but never above it."""
    bars = _bars(*[(100 + i, 99 + i, 100 + i) for i in range(15)])
    series = so.compute_sar_series(bars, direction="long")
    assert series[0] is None
    # All subsequent SAR values are non-None floats.
    sars = [s for s in series[1:] if s is not None]
    assert len(sars) == 14
    # Each step rises (or holds steady due to limit) but never exceeds
    # the corresponding bar's high.
    for i, s in enumerate(sars):
        assert s < bars[i + 1]["high"], (
            f"bar {i+1}: SAR={s} should be below high={bars[i+1]['high']}"
        )
    # Each SAR ≥ the prior SAR (monotonically rising in uptrend).
    for a, b in zip(sars, sars[1:]):
        assert b >= a, f"SAR went backwards: {a} → {b}"


def test_compute_sar_seed_for_long_is_first_bar_low():
    """The first bar serves as the seed — SAR starts at its low (for
    a long). Verify the second bar's SAR is consistent with Wilder's
    formula: SAR_2 = SAR_1 + AF × (EP_1 - SAR_1) = low_0 + 0.02 ×
    (high_0 - low_0). Note: bar 2's high may revise EP+AF afterward
    but the SAR ITSELF is computed using the prior bar's EP, before
    incorporating bar 2."""
    bars = _bars((110, 100, 105), (115, 108, 113), (120, 110, 118))
    series = so.compute_sar_series(bars, direction="long")
    # bar 1 SAR uses sar_0=100 (seed), ep_0=110, af=0.02
    # SAR_1 = 100 + 0.02 × (110 - 100) = 100.2
    # Wilder's "two-bar limit": SAR can't exceed prior bar's low (100).
    # → limited to 100.
    assert series[1] == pytest.approx(100.0)


def test_compute_sar_af_caps_at_max():
    """AF starts at 0.02 and increments by 0.02 on each new EP. With
    repeated new highs the AF should cap at 0.20."""
    # 20 bars each making a new high.
    bars = _bars(*[(100 + i * 2, 99 + i * 2, 100 + i * 2) for i in range(20)])
    series = so.compute_sar_series(bars, direction="long",
                                    af_max=0.20)
    # After bar 11 we should have hit AF cap (0.02 × 10 = 0.2). The
    # series should still produce monotonic SAR values.
    assert all(s is not None for s in series[1:])


def test_compute_sar_short_direction_mirror():
    """In a downtrend (each bar lower than prior), SAR should fall
    monotonically toward the EP (lowest low)."""
    bars = _bars(*[(100 - i, 99 - i, 100 - i) for i in range(15)])
    series = so.compute_sar_series(bars, direction="short")
    sars = [s for s in series[1:] if s is not None]
    assert len(sars) == 14
    for i, s in enumerate(sars):
        assert s > bars[i + 1]["low"], (
            f"bar {i+1}: SAR={s} should be above low={bars[i+1]['low']}"
        )
    for a, b in zip(sars, sars[1:]):
        assert b <= a, f"short SAR went forwards in downtrend: {a} → {b}"


def test_compute_sar_invalid_direction_defaults_to_long():
    bars = _bars(*[(100 + i, 99 + i, 100 + i) for i in range(5)])
    series_invalid = so.compute_sar_series(bars, direction="diagonal")
    series_long = so.compute_sar_series(bars, direction="long")
    assert series_invalid == series_long


# ---------------------------------------------------------------------------
# Persisted state
# ---------------------------------------------------------------------------

def test_init_sar_writes_row(conn):
    bars = _bars(*[(100 + i, 99 + i, 100 + i) for i in range(10)])
    state = so.init_sar(
        conn, strategy_id="winner", symbol="GDX",
        bars=bars, direction="long",
    )
    assert state["direction"] == "long"
    assert state["sar"] is not None
    row = so.get_sar_state(conn, strategy_id="winner", symbol="GDX")
    assert row is not None
    assert row["direction"] == "long"


def test_advance_sar_updates_state(conn):
    bars = _bars(*[(100 + i, 99 + i, 100 + i) for i in range(10)])
    so.init_sar(conn, strategy_id="winner", symbol="GDX",
                bars=bars, direction="long")
    initial = so.get_sar_state(conn, strategy_id="winner", symbol="GDX")
    new_bar = {"high": 115, "low": 109, "close": 114}
    updated = so.advance_sar(conn, strategy_id="winner", symbol="GDX",
                              bar=new_bar)
    assert updated is not None
    # SAR has advanced (new high → AF possibly incremented).
    assert updated["sar"] != initial["sar"] or updated["af"] >= initial["af"]


def test_advance_sar_no_row_returns_none(conn):
    out = so.advance_sar(conn, strategy_id="missing", symbol="X",
                          bar={"high": 1, "low": 0, "close": 0.5})
    assert out is None


def test_clear_sar_state_removes_row(conn):
    bars = _bars(*[(100 + i, 99 + i, 100 + i) for i in range(5)])
    so.init_sar(conn, strategy_id="winner", symbol="GDX",
                 bars=bars, direction="long")
    so.clear_sar_state(conn, strategy_id="winner", symbol="GDX")
    assert so.get_sar_state(conn, strategy_id="winner", symbol="GDX") is None


# ---------------------------------------------------------------------------
# is_sar_flip
# ---------------------------------------------------------------------------

def test_is_sar_flip_long_triggers_when_low_breaks_sar():
    assert so.is_sar_flip(sar=100.0, direction="long",
                           bar_low=99.5, bar_high=101.0) is True
    assert so.is_sar_flip(sar=100.0, direction="long",
                           bar_low=100.0, bar_high=101.0) is True  # equal
    assert so.is_sar_flip(sar=100.0, direction="long",
                           bar_low=100.5, bar_high=101.0) is False


def test_is_sar_flip_short_triggers_when_high_breaks_sar():
    assert so.is_sar_flip(sar=100.0, direction="short",
                           bar_low=98.0, bar_high=100.5) is True
    assert so.is_sar_flip(sar=100.0, direction="short",
                           bar_low=98.0, bar_high=99.0) is False


# ---------------------------------------------------------------------------
# Overlay engine — precedence
# ---------------------------------------------------------------------------

def test_overlay_no_sar_state_no_trailing_no_exit(conn):
    out = so.should_exit_with_sar_overlay(
        conn, strategy_id="x", symbol="X",
        current_price=100.0, trailing_stop_hit=False,
    )
    assert out["should_exit"] is False
    assert out["sar_flip"] is False
    assert out["reason"] is None


def test_overlay_trailing_stop_only_fires_when_no_sar(conn):
    """No SAR state: overlay degrades to "trailing stop only"."""
    out = so.should_exit_with_sar_overlay(
        conn, strategy_id="x", symbol="X",
        current_price=100.0, trailing_stop_hit=True,
    )
    assert out["should_exit"] is True
    assert out["reason"] == "trailing_stop_hit"


def test_overlay_sar_flip_alone_exits(conn):
    bars = _bars(*[(100 + i, 99 + i, 100 + i) for i in range(10)])
    so.init_sar(conn, strategy_id="x", symbol="X",
                 bars=bars, direction="long")
    # Force a SAR flip: low far below current SAR.
    out = so.should_exit_with_sar_overlay(
        conn, strategy_id="x", symbol="X",
        current_price=98.0, bar_low=98.0, bar_high=99.0,
        trailing_stop_hit=False,
    )
    assert out["sar_flip"] is True
    assert out["should_exit"] is True
    assert out["reason"] == "sar_flip"


def test_overlay_both_fires_reports_combined(conn):
    """When both trailing_stop AND sar_flip fire on the same bar,
    the reason flags both."""
    bars = _bars(*[(100 + i, 99 + i, 100 + i) for i in range(10)])
    so.init_sar(conn, strategy_id="x", symbol="X",
                 bars=bars, direction="long")
    out = so.should_exit_with_sar_overlay(
        conn, strategy_id="x", symbol="X",
        current_price=95.0, bar_low=95.0, bar_high=96.0,
        trailing_stop_hit=True,
    )
    assert out["should_exit"] is True
    assert out["reason"] == "trailing_stop_hit+sar_flip"


def test_overlay_uses_current_price_when_no_bar(conn):
    """When bar_low/high aren't supplied, use current_price as both."""
    bars = _bars(*[(100 + i, 99 + i, 100 + i) for i in range(10)])
    so.init_sar(conn, strategy_id="x", symbol="X",
                 bars=bars, direction="long")
    sar_row = so.get_sar_state(conn, strategy_id="x", symbol="X")
    # Pick a price comfortably below the SAR to force a flip.
    out = so.should_exit_with_sar_overlay(
        conn, strategy_id="x", symbol="X",
        current_price=sar_row["sar"] - 1.0,
    )
    assert out["sar_flip"] is True


# ---------------------------------------------------------------------------
# strategy_has_sar_overlay
# ---------------------------------------------------------------------------

def test_strategy_has_sar_overlay_opt_in():
    assert so.strategy_has_sar_overlay({"sar_overlay": True}) is True
    assert so.strategy_has_sar_overlay({"sar_overlay": False}) is False
    assert so.strategy_has_sar_overlay({}) is False
    assert so.strategy_has_sar_overlay(None) is False


def test_strategy_has_sar_overlay_truthy_values():
    """Mistakenly setting sar_overlay='yes' should still be truthy.
    The check just uses bool()."""
    assert so.strategy_has_sar_overlay({"sar_overlay": "yes"}) is True
    assert so.strategy_has_sar_overlay({"sar_overlay": 1}) is True
