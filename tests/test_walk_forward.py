import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import walk_forward as wf  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic bars + compute_fn helpers
# ---------------------------------------------------------------------------

def _make_bars(n=400, base=100.0, drift=0.0, seed=0):
    rng = np.random.default_rng(seed)
    closes = base + rng.normal(drift, 0.5, n).cumsum()
    df = pd.DataFrame({
        "open": closes,
        "high": closes + abs(rng.normal(0, 0.5, n)),
        "low": closes - abs(rng.normal(0, 0.5, n)),
        "close": closes,
        "volume": rng.integers(1_000_000, 5_000_000, n),
    })
    df.index = pd.date_range("2024-01-01", periods=n, freq="D")
    return df


def _always_winning_compute(bars):
    """compute_fn that enters every 5 bars and exits the next bar with a +1% bump."""
    out = bars.copy()
    out["long_entry"] = False
    out["long_exit"] = False
    # Pretend price bumps +1% on entry; we simulate by making the exit price
    # one bar after entry exactly 1% above the entry close.
    out["close"] = out["close"].astype(float)
    n = len(out)
    for i in range(0, n - 1, 5):
        out.iloc[i, out.columns.get_loc("long_entry")] = True
        if i + 1 < n:
            out.iloc[i + 1, out.columns.get_loc("long_exit")] = True
            # Mutate the exit's close to be +1% of the entry's close.
            entry_price = out.iloc[i]["close"]
            out.iloc[i + 1, out.columns.get_loc("close")] = entry_price * 1.01
    return out


def _always_losing_compute(bars):
    out = bars.copy()
    out["long_entry"] = False
    out["long_exit"] = False
    out["close"] = out["close"].astype(float)
    n = len(out)
    for i in range(0, n - 1, 5):
        out.iloc[i, out.columns.get_loc("long_entry")] = True
        if i + 1 < n:
            out.iloc[i + 1, out.columns.get_loc("long_exit")] = True
            entry_price = out.iloc[i]["close"]
            out.iloc[i + 1, out.columns.get_loc("close")] = entry_price * 0.99
    return out


def _never_fires_compute(bars):
    out = bars.copy()
    out["long_entry"] = False
    out["long_exit"] = False
    return out


_FLAKY_PIVOT = pd.Timestamp("2025-03-01")


def _flaky_compute(bars):
    """Wins for trades opened before March 2025; loses after — unstable across
    walk-forward windows because the regime change is keyed on absolute date,
    not the position within the slice."""
    out = bars.copy()
    out["long_entry"] = False
    out["long_exit"] = False
    out["close"] = out["close"].astype(float)
    n = len(out)
    for i in range(0, n - 1, 5):
        out.iloc[i, out.columns.get_loc("long_entry")] = True
        if i + 1 < n:
            out.iloc[i + 1, out.columns.get_loc("long_exit")] = True
            entry_price = out.iloc[i]["close"]
            sign = 1.01 if out.index[i] < _FLAKY_PIVOT else 0.99
            out.iloc[i + 1, out.columns.get_loc("close")] = entry_price * sign
    return out


# ---------------------------------------------------------------------------
# build_windows
# ---------------------------------------------------------------------------

def test_build_windows_basic():
    win = wf.build_windows(0, 400,
                           train_days=180, test_days=90, step_days=90)
    # 400 bars, 180+90=270 first window ends at 270; step 90 → next is 90-360
    # Need test_hi <= 400 so cursors at 0 → 270; 90 → 360; 180 → 450 (too far)
    assert win == [(0, 180, 180, 270), (90, 270, 270, 360)]


def test_build_windows_no_fits():
    win = wf.build_windows(0, 200,
                           train_days=180, test_days=90, step_days=90)
    # need >= 270 bars; 200 not enough
    assert win == []


def test_build_windows_exact_fit():
    win = wf.build_windows(0, 270,
                           train_days=180, test_days=90, step_days=90)
    assert win == [(0, 180, 180, 270)]


def test_build_windows_rejects_non_positive():
    with pytest.raises(ValueError):
        wf.build_windows(0, 400, train_days=0, test_days=90, step_days=90)
    with pytest.raises(ValueError):
        wf.build_windows(0, 400, train_days=180, test_days=0, step_days=90)
    with pytest.raises(ValueError):
        wf.build_windows(0, 400, train_days=180, test_days=90, step_days=0)


def test_build_windows_smaller_step_overlaps():
    win = wf.build_windows(0, 400,
                           train_days=180, test_days=90, step_days=45)
    # cursors at 0, 45, 90, 135, ... ; window length = 270; need test_hi <= 400.
    cursors = [w[0] for w in win]
    # last cursor such that cursor + 270 <= 400 → cursor <= 130 → 0, 45, 90.
    # Actually 0+270=270, 45+270=315, 90+270=360, 135+270=405 > 400 → stop.
    assert cursors == [0, 45, 90]


# ---------------------------------------------------------------------------
# verdict_class & verdicts_match
# ---------------------------------------------------------------------------

def test_verdict_class_known_values():
    assert wf.verdict_class("PASS") == "positive"
    assert wf.verdict_class("PASS_WITH_NUANCE") == "positive"
    assert wf.verdict_class("MARGINAL") == "positive"
    assert wf.verdict_class("FAIL") == "negative"
    assert wf.verdict_class("UNTESTED") == "negative"
    assert wf.verdict_class("") == "unknown"
    assert wf.verdict_class("anything_else") == "unknown"


def test_verdicts_match_same_class():
    assert wf.verdicts_match("PASS", "MARGINAL") is True
    assert wf.verdicts_match("FAIL", "UNTESTED") is True
    assert wf.verdicts_match("PASS", "FAIL") is False
    assert wf.verdicts_match("MARGINAL", "FAIL") is False


# ---------------------------------------------------------------------------
# walk_forward_symbol — synthetic strategies
# ---------------------------------------------------------------------------

def test_walk_forward_stable_for_consistently_winning_strategy():
    bars = _make_bars(n=800, seed=1)
    res = wf.walk_forward_symbol(
        bars, _always_winning_compute,
        train_days=120, test_days=120, step_days=120,
    )
    # In-sample with +1% per trade → positive verdict.
    assert wf.verdict_class(res["in_sample_verdict"]) == "positive"
    # All test windows should also be positive.
    assert res["n_windows"] > 0
    assert res["match_ratio"] == 1.0


def test_walk_forward_stable_for_consistently_losing_strategy():
    bars = _make_bars(n=800, seed=2)
    res = wf.walk_forward_symbol(
        bars, _always_losing_compute,
        train_days=120, test_days=120, step_days=120,
    )
    # In-sample is negative; all windows also negative → stable.
    assert wf.verdict_class(res["in_sample_verdict"]) == "negative"
    assert res["match_ratio"] == 1.0


def test_walk_forward_unstable_for_flaky_strategy():
    bars = _make_bars(n=800, seed=3)
    res = wf.walk_forward_symbol(
        bars, _flaky_compute,
        train_days=120, test_days=120, step_days=120,
    )
    # The early test windows should be positive; later ones should flip negative.
    # In-sample is mixed → either class. Whichever it is, the windows that
    # disagree pull match_ratio below 1.0.
    assert res["n_windows"] >= 2
    assert res["match_ratio"] < 1.0


def test_walk_forward_zero_windows_when_bars_too_short():
    bars = _make_bars(n=50, seed=4)
    res = wf.walk_forward_symbol(
        bars, _always_winning_compute,
        train_days=60, test_days=30, step_days=30,
    )
    assert res["n_windows"] == 0
    assert res["match_ratio"] == 0.0


def test_walk_forward_empty_bars():
    empty = pd.DataFrame()
    res = wf.walk_forward_symbol(
        empty, _always_winning_compute,
        train_days=60, test_days=30, step_days=30,
    )
    assert res["in_sample_verdict"] == "UNTESTED"
    assert res["windows"] == []


def test_walk_forward_handles_compute_fn_raising():
    bars = _make_bars(n=400, seed=5)

    def broken(bars):
        raise RuntimeError("kaboom")

    res = wf.walk_forward_symbol(
        bars, broken, train_days=120, test_days=120, step_days=120,
    )
    # Each window's test verdict should be FAIL → "negative". In-sample is
    # also FAIL → match_ratio == 1.0 (both classes negative). The point is
    # the function doesn't crash.
    assert res["n_windows"] > 0
    assert all(w["test_verdict"] == "FAIL" for w in res["windows"])


# ---------------------------------------------------------------------------
# walk_forward_strategy — orchestration
# ---------------------------------------------------------------------------

def test_walk_forward_strategy_aggregates_across_universe():
    bars_by_sym = {
        "GDX": _make_bars(n=800, seed=10),
        "KRE": _make_bars(n=800, seed=11),
    }
    result = wf.walk_forward_strategy(
        strategy_id="alpha", universe=["GDX", "KRE"],
        train_days=120, test_days=120, step_days=120,
        fn=_always_winning_compute, bars_by_sym=bars_by_sym,
    )
    assert "GDX" in result["per_symbol"]
    assert "KRE" in result["per_symbol"]
    assert result["total_windows"] > 0
    assert result["walk_forward_stable"] is True
    assert result["overall_match_ratio"] == 1.0


def test_walk_forward_strategy_marks_unstable_when_below_threshold():
    bars_by_sym = {"GDX": _make_bars(n=800, seed=20)}
    result = wf.walk_forward_strategy(
        strategy_id="flaky", universe=["GDX"],
        train_days=120, test_days=120, step_days=120,
        stable_ratio=0.95,  # demanding
        fn=_flaky_compute, bars_by_sym=bars_by_sym,
    )
    assert result["walk_forward_stable"] is False


def test_walk_forward_strategy_handles_missing_symbol_bars():
    bars_by_sym = {"GDX": _make_bars(n=800, seed=30)}  # KRE missing
    result = wf.walk_forward_strategy(
        strategy_id="alpha", universe=["GDX", "KRE"],
        train_days=120, test_days=120, step_days=120,
        fn=_always_winning_compute, bars_by_sym=bars_by_sym,
    )
    assert result["per_symbol"]["KRE"]["n_windows"] == 0
    assert result["per_symbol"]["GDX"]["n_windows"] > 0


def test_walk_forward_strategy_unstable_when_zero_windows():
    # Universe of symbols all with too-few bars.
    bars_by_sym = {"GDX": _make_bars(n=50, seed=40)}
    result = wf.walk_forward_strategy(
        strategy_id="x", universe=["GDX"],
        train_days=180, test_days=90, step_days=90,
        fn=_always_winning_compute, bars_by_sym=bars_by_sym,
    )
    assert result["total_windows"] == 0
    assert result["walk_forward_stable"] is False


# ---------------------------------------------------------------------------
# Record write-back
# ---------------------------------------------------------------------------

def test_apply_walk_forward_to_record_sets_stable_flag():
    rec = {"extra": {"strategy_id": "abc"}}
    result = {
        "walk_forward_stable": True,
        "evaluated_iso": "2026-05-15",
        "train_days": 180, "test_days": 90, "step_days": 90,
        "total_windows": 5, "total_matching": 5, "overall_match_ratio": 1.0,
        "stable_ratio_required": 0.7,
        "universe": ["GDX"],
    }
    wf.apply_walk_forward_to_record(rec, result)
    assert rec["extra"]["walk_forward_stable"] is True
    assert rec["extra"]["walk_forward_summary"]["total_windows"] == 5
    assert rec["extra"]["walk_forward_summary"]["overall_match_ratio"] == 1.0


def test_apply_walk_forward_overrides_existing_value():
    rec = {"extra": {"strategy_id": "abc", "walk_forward_stable": True}}
    result = {
        "walk_forward_stable": False,
        "evaluated_iso": "2026-05-15",
        "train_days": 180, "test_days": 90, "step_days": 90,
        "total_windows": 5, "total_matching": 1, "overall_match_ratio": 0.2,
        "stable_ratio_required": 0.7,
        "universe": ["GDX"],
    }
    wf.apply_walk_forward_to_record(rec, result)
    assert rec["extra"]["walk_forward_stable"] is False


def test_find_record_returns_matching_record():
    records = [
        {"extra": {"strategy_id": "a"}},
        {"extra": {"strategy_id": "b"}},
    ]
    assert wf._find_record(records, "b")["extra"]["strategy_id"] == "b"
    assert wf._find_record(records, "missing") is None


def test_records_round_trip(tmp_path):
    p = tmp_path / "r.jsonl"
    records = [{"extra": {"strategy_id": "a"}},
               {"extra": {"strategy_id": "b"}}]
    wf._save_records(p, records)
    assert wf._load_records(p) == records


def test_load_records_returns_empty_for_missing(tmp_path):
    assert wf._load_records(tmp_path / "no.jsonl") == []


# ---------------------------------------------------------------------------
# In-sample verdict drives matching, not absolute verdict
# ---------------------------------------------------------------------------

def test_walk_forward_negative_in_sample_with_negative_windows_is_stable():
    """A consistently-failing strategy is 'stable' (FAIL all windows == FAIL in-sample)."""
    bars = _make_bars(n=400, seed=50)
    res = wf.walk_forward_symbol(
        bars, _never_fires_compute,
        train_days=120, test_days=120, step_days=120,
    )
    # never_fires → UNTESTED in all windows AND in-sample → match_ratio = 1.0
    assert res["in_sample_verdict"] == "UNTESTED"
    assert res["match_ratio"] == 1.0
