"""6.3.1 — Donchian breakout-and-retest strategy.

Covers signal sequencing, pending-window expiry, retest tolerance,
and the no-chase invariant: a breakout that never retests within 5
bars produces no entry.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies.breakout import donchian_retest as dr  # noqa: E402
from strategies.breakout import BREAKOUT_DECLARATIONS  # noqa: E402


def _make_df(rows):
    """rows is a list of (open, high, low, close) tuples. Builds a daily-
    indexed DataFrame."""
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


# ---------------------------------------------------------------------------
# Declaration — registered, breakout class, retest tight stop, not pyramidable
# ---------------------------------------------------------------------------

def test_declaration_registered():
    ids = [d["id"] for d in BREAKOUT_DECLARATIONS]
    assert "breakout-donchian-retest-20" in ids


def test_declaration_strategy_class_breakout():
    decl = next(d for d in BREAKOUT_DECLARATIONS
                if d["id"] == "breakout-donchian-retest-20")
    assert decl["strategy_class"] == "breakout"


def test_declaration_not_pyramidable():
    decl = next(d for d in BREAKOUT_DECLARATIONS
                if d["id"] == "breakout-donchian-retest-20")
    assert decl["pyramidable"] is False


def test_declaration_active_in_regimes_includes_trending_up():
    decl = next(d for d in BREAKOUT_DECLARATIONS
                if d["id"] == "breakout-donchian-retest-20")
    assert "trending_up" in decl["active_in_regimes"]
    # The milestone says "bull / trend" — our project vocab maps to
    # trending_up + mixed. trending_down and choppy are excluded
    # (the strategy is meant for bull markets only).
    assert "trending_down" not in decl["active_in_regimes"]
    assert "choppy" not in decl["active_in_regimes"]


def test_declaration_initial_stop_is_atr_initial_with_k1():
    decl = next(d for d in BREAKOUT_DECLARATIONS
                if d["id"] == "breakout-donchian-retest-20")
    stop = decl["initial_stop"]
    assert stop["method"] == "atr_initial"
    assert stop["multiplier"] == 1.0  # tight per 6.3.1
    assert stop["atr_period"] == 14


# ---------------------------------------------------------------------------
# Signal contract
# ---------------------------------------------------------------------------

def test_compute_adds_required_columns():
    rows = [(100, 101, 99, 100)] * 25
    out = dr.compute_donchian_retest(_make_df(rows))
    for c in ("long_entry", "long_exit", "breakout", "pending"):
        assert c in out.columns


def test_no_breakout_no_entry():
    """Flat market — no breakout, no entry, ever."""
    rows = [(100, 101, 99, 100)] * 30
    out = dr.compute_donchian_retest(_make_df(rows))
    assert not out["long_entry"].any()
    assert not out["breakout"].any()


def test_breakout_alone_does_not_fire_entry():
    """A breakout without a retest in the window produces NO entry.
    Build a steady up-only price that breaks the 20-bar high and keeps
    climbing — no retest ever happens, so no entry."""
    # 20 baseline bars at high=101, then steady climb +1/bar.
    rows = [(100, 101, 99, 100)] * 20
    # Day 21 onward: each bar opens above prior high and keeps climbing —
    # close > prior 20-day high. NEVER returns to retest the level.
    for i in range(20):
        # Each new bar's low is well above the original level (~101).
        base = 110 + i
        rows.append((base, base + 1, base - 0.1, base + 1))
    out = dr.compute_donchian_retest(_make_df(rows))
    # Breakouts did fire.
    assert out["breakout"].iloc[21:].any()
    # But no retest occurs because price keeps climbing.
    assert not out["long_entry"].any()


def test_breakout_then_immediate_retest_fires_entry():
    """Classic case: breakout on bar T, retest of the level on bar T+1
    → entry on bar T+1."""
    # 20 baseline bars (high=101) then bar 21 breaks out, bar 22 dips
    # back to retest.
    rows = [(100, 101, 99, 100)] * 20
    # Bar 21: clear breakout — close way above 101.
    rows.append((105, 110, 104, 109))
    # Bar 22: pulls back so that the bar's range touches level=101
    # within ±0.5×ATR. ATR is ~2 at this point.
    rows.append((105, 105, 100, 102))
    # A few more bars so the index is valid.
    rows.extend([(102, 103, 101, 102)] * 5)
    out = dr.compute_donchian_retest(_make_df(rows))
    # Entry fires on bar 22.
    assert bool(out["long_entry"].iloc[21]) or bool(out["long_entry"].iloc[22])


def test_window_expires_no_chase():
    """If 5 bars elapse with no retest, the pending entry is dropped
    and a later retest cannot re-enter without a fresh breakout."""
    rows = [(100, 101, 99, 100)] * 20
    # Bar 21: breakout.
    rows.append((105, 110, 104, 109))
    # 5 bars of price drifting UP, never touching 101 ± tolerance.
    for _ in range(6):
        rows.append((115, 117, 114, 116))
    # Bar 27: NOW dips back to retest — but window has expired.
    rows.append((105, 105, 100, 101))
    rows.extend([(102, 103, 101, 102)] * 3)
    out = dr.compute_donchian_retest(_make_df(rows))
    # The retest bar at index 27 should NOT fire entry because the
    # 5-bar window for the breakout at bar 21 already expired.
    assert bool(out["long_entry"].iloc[27]) is False
    # The whole series should have no entry at all.
    assert not out["long_entry"].any()


def test_retest_tolerance_uses_half_atr_band():
    """A bar that touches level ± 0.5×ATR retests. A bar that misses
    that band by more than 0.5×ATR does not."""
    rows = [(100, 101, 99, 100)] * 20  # baseline high = 101, ATR ~ 2
    rows.append((105, 110, 104, 109))  # bar 20: breakout
    # Bar 21: low is 99 — 2 points below level 101 (= 1×ATR away).
    # 0.5×ATR ≈ 1. lower band ≈ 100, upper band ≈ 102. low=99 → just
    # outside the band (low ≤ 102 ✓, high ≥ 100 — high=109 ✓ — so
    # range straddles. Should fire.)
    # The TEST: a bar whose ENTIRE range is above the upper band
    # (e.g. low=105, high=110) should NOT fire.
    rows.append((108, 110, 105, 109))
    rows.extend([(102, 103, 101, 102)] * 3)
    out = dr.compute_donchian_retest(_make_df(rows))
    # Bar 21 has low=105 → above upper band → no retest.
    assert bool(out["long_entry"].iloc[21]) is False


def test_breakout_within_pending_window_replaces_level():
    """A second breakout while a prior retest window is still open
    replaces the active waiting level (most recent breakout wins)."""
    rows = [(100, 101, 99, 100)] * 20
    rows.append((105, 110, 104, 109))  # breakout #1, level=101
    rows.append((110, 115, 109, 114))  # breakout #2 (close > prior 20-day high)
    rows.append((110, 112, 108, 109))  # retest of breakout #2 level (~109)
    rows.extend([(105, 106, 104, 105)] * 5)
    out = dr.compute_donchian_retest(_make_df(rows))
    # Should still record pending bars after the second breakout.
    assert bool(out["pending"].iloc[22]) or bool(out["pending"].iloc[23])


def test_exit_on_close_below_10bar_low():
    """long_exit fires when close < trailing 10-bar low."""
    # Build a series that triggers an exit late.
    rows = [(100, 101, 99, 100)] * 25
    # Big drop on bar 26 below the prior 10-bar low (which is ~99).
    rows.append((90, 90, 80, 80))
    out = dr.compute_donchian_retest(_make_df(rows))
    assert bool(out["long_exit"].iloc[-1])


# ---------------------------------------------------------------------------
# Defensive: missing columns
# ---------------------------------------------------------------------------

def test_missing_columns_returns_all_false():
    """If the DataFrame is missing required columns, fail safe."""
    df = pd.DataFrame({"x": [1, 2, 3]})
    out = dr.compute_donchian_retest(df)
    assert "long_entry" in out.columns
    assert not out["long_entry"].any()


# ---------------------------------------------------------------------------
# ATR helper
# ---------------------------------------------------------------------------

def test_atr_constant_bars():
    rows = [(100, 102, 98, 100)] * 20
    df = _make_df(rows)
    atr = dr._atr(df, period=14)
    # TR each bar = max(4, |2-0|, |-2-0|) = 4 → ATR = 4
    assert atr.iloc[-1] == pytest.approx(4.0)


def test_atr_undefined_when_too_few_bars():
    rows = [(100, 102, 98, 100)] * 5
    df = _make_df(rows)
    atr = dr._atr(df, period=14)
    # All NaN since we need ≥ 14 bars.
    assert atr.isna().all()
