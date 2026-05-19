"""6.3.2 — Donchian breakdown-and-retest (short side).

Mirror of 6.3.1 tests: signal sequencing, pending-window expiry,
retest tolerance, no-chase, exit logic. Also validates the short-
specific risk overrides (50% position-size multiplier, side=short).
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies.breakout import donchian_retest_short as drs  # noqa: E402
from strategies.breakout import BREAKOUT_DECLARATIONS  # noqa: E402


def _make_df(rows):
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------

def test_declaration_registered():
    ids = [d["id"] for d in BREAKOUT_DECLARATIONS]
    assert "breakout-donchian-retest-short-20" in ids


def test_declaration_is_breakout_class_short_side():
    decl = next(d for d in BREAKOUT_DECLARATIONS
                if d["id"] == "breakout-donchian-retest-short-20")
    assert decl["strategy_class"] == "breakout"
    assert decl["side"] == "short"


def test_declaration_active_in_bear_trend():
    """Project vocab: bear → trending_down. Should NOT include
    trending_up (bull) — that's the long side's territory."""
    decl = next(d for d in BREAKOUT_DECLARATIONS
                if d["id"] == "breakout-donchian-retest-short-20")
    assert "trending_down" in decl["active_in_regimes"]
    assert "trending_up" not in decl["active_in_regimes"]
    assert "choppy" not in decl["active_in_regimes"]


def test_declaration_max_position_usd_multiplier_is_half():
    """Short positions size at 50% of the long equivalent due to
    asymmetric risk (borrow costs + unlimited-loss exposure)."""
    decl = next(d for d in BREAKOUT_DECLARATIONS
                if d["id"] == "breakout-donchian-retest-short-20")
    assert decl["max_position_usd_multiplier"] == 0.5


def test_declaration_not_pyramidable():
    decl = next(d for d in BREAKOUT_DECLARATIONS
                if d["id"] == "breakout-donchian-retest-short-20")
    assert decl["pyramidable"] is False


def test_declaration_initial_stop_is_atr_initial_with_k1():
    decl = next(d for d in BREAKOUT_DECLARATIONS
                if d["id"] == "breakout-donchian-retest-short-20")
    stop = decl["initial_stop"]
    assert stop["method"] == "atr_initial"
    assert stop["multiplier"] == 1.0


# ---------------------------------------------------------------------------
# Signal contract
# ---------------------------------------------------------------------------

def test_compute_adds_required_columns():
    rows = [(100, 101, 99, 100)] * 25
    out = drs.compute_donchian_retest_short(_make_df(rows))
    for c in ("short_entry", "short_exit", "breakdown", "pending"):
        assert c in out.columns


def test_no_breakdown_no_entry():
    rows = [(100, 101, 99, 100)] * 30
    out = drs.compute_donchian_retest_short(_make_df(rows))
    assert not out["short_entry"].any()
    assert not out["breakdown"].any()


def test_breakdown_alone_does_not_fire_entry():
    """A breakdown that keeps falling, never retests → no entry."""
    rows = [(100, 101, 99, 100)] * 20
    # Bars 21+ drop steadily and never recover to retest the level.
    for i in range(20):
        base = 90 - i
        # Each bar's high is well below the original level ~99.
        rows.append((base, base + 0.1, base - 1, base - 1))
    out = drs.compute_donchian_retest_short(_make_df(rows))
    assert out["breakdown"].iloc[21:].any()
    assert not out["short_entry"].any()


def test_breakdown_then_retest_fires_entry():
    """Classic case: breakdown on bar T, retest from below on bar T+1
    → short entry on bar T+1."""
    rows = [(100, 101, 99, 100)] * 20
    # Bar 21: clear breakdown — close way below 99.
    rows.append((95, 96, 90, 91))
    # Bar 22: pushes back up to retest level 99 within ±0.5×ATR.
    rows.append((95, 99.5, 94, 98))
    rows.extend([(95, 96, 93, 94)] * 5)
    out = drs.compute_donchian_retest_short(_make_df(rows))
    assert bool(out["short_entry"].iloc[21]) or bool(out["short_entry"].iloc[22])


def test_window_expires_no_chase():
    """5 bars after breakdown without a retest → cancel; a later retest
    doesn't re-enter without a fresh breakdown."""
    rows = [(100, 101, 99, 100)] * 20
    rows.append((95, 96, 90, 91))  # breakdown
    # 6 bars staying DOWN, never retesting up to 99.
    for _ in range(6):
        rows.append((80, 82, 78, 80))
    # Bar 27: NOW pushes back to retest 99 — but window expired.
    rows.append((80, 99, 78, 90))
    rows.extend([(80, 82, 78, 80)] * 3)
    out = drs.compute_donchian_retest_short(_make_df(rows))
    assert bool(out["short_entry"].iloc[27]) is False
    assert not out["short_entry"].any()


def test_retest_must_be_above_level_not_below():
    """A retest must push UP to the broken level. A bar staying entirely
    below the level (high < level) is a breakdown continuation, not a
    retest — strategy must NOT enter."""
    rows = [(100, 101, 99, 100)] * 20
    rows.append((95, 96, 90, 91))  # breakdown (level=99)
    # Bar 22: high=95 (still well below 99). Should NOT fire entry.
    rows.append((92, 95, 90, 93))
    rows.extend([(92, 93, 90, 91)] * 3)
    out = drs.compute_donchian_retest_short(_make_df(rows))
    assert bool(out["short_entry"].iloc[21]) is False


def test_retest_blown_through_level_no_entry():
    """A bar whose high blows WAY past the level (more than 0.5×ATR
    above) isn't a clean retest — it's the breakdown failing. No entry."""
    rows = [(100, 101, 99, 100)] * 20
    rows.append((95, 96, 90, 91))  # breakdown level=99, ATR ~ 2
    # Bar 22: high=110 — many ATRs above level=99. Not a retest.
    rows.append((100, 110, 95, 109))
    rows.extend([(105, 106, 104, 105)] * 3)
    out = drs.compute_donchian_retest_short(_make_df(rows))
    assert bool(out["short_entry"].iloc[21]) is False


def test_short_exit_on_close_above_10bar_high():
    """short_exit fires when close exceeds the prior 10-bar high."""
    rows = [(100, 101, 99, 100)] * 25
    # Big spike on bar 26.
    rows.append((110, 120, 109, 120))
    out = drs.compute_donchian_retest_short(_make_df(rows))
    assert bool(out["short_exit"].iloc[-1])


# ---------------------------------------------------------------------------
# Defensive
# ---------------------------------------------------------------------------

def test_missing_columns_returns_all_false():
    df = pd.DataFrame({"x": [1, 2, 3]})
    out = drs.compute_donchian_retest_short(df)
    assert "short_entry" in out.columns
    assert not out["short_entry"].any()
