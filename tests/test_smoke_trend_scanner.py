"""Unit tests for scripts/smoke_trend_scanner.py (milestone 5.5.7.1).

The smoke test itself is an integration runner; these unit tests cover
its scaffolding (synthetic-bar generator, universe / liquidity wiring,
trace formatter) and exercise the full end-to-end harness on a clean
in-memory DB.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def smoke():
    spec = importlib.util.spec_from_file_location(
        "smoke_trend_scanner",
        ROOT / "scripts" / "smoke_trend_scanner.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# synthetic bar generators
# ---------------------------------------------------------------------------

def test_breakout_bars_have_ohlcv_and_ramp(smoke):
    bars = smoke._breakout_bars(n=60)
    assert len(bars) == 60
    for col in ("open", "high", "low", "close", "volume"):
        assert col in bars.columns
    # Last bar must be above the flat baseline (ramp ended at peak)
    assert bars["close"].iloc[-1] > bars["close"].iloc[0]


def test_flat_bars_no_movement(smoke):
    bars = smoke._flat_bars(n=30)
    assert bars["close"].min() == bars["close"].max() == 100.0


def test_date_string_index_ends_today(smoke):
    """Index must be date strings so bar_ts matches asof.isoformat().
    On weekends pandas freq='B' snaps end to Friday, so we align with
    the smoke script's _latest_business_day() helper rather than
    asserting strict date.today()."""
    bars = smoke._breakout_bars(n=10)
    last = bars.index[-1]
    assert isinstance(last, str)
    assert last == smoke._latest_business_day().isoformat()


def test_universe_bars_covers_all_synthetic_symbols(smoke):
    bars = smoke._build_universe_bars()
    expected = set(smoke.BREAKOUT_SYMBOLS) | set(smoke.FLAT_SYMBOLS) \
        | set(smoke.ILLIQUID_SYMBOLS)
    assert set(bars.keys()) == expected
    assert len(bars) == smoke.N_SYNTHETIC_SYMBOLS


# ---------------------------------------------------------------------------
# Trace formatter
# ---------------------------------------------------------------------------

def test_format_trace_emits_each_stage(smoke):
    trace = {
        "n_universe": 20, "n_after_liquidity": 18,
        "skipped_liquidity": ["ILQ00", "ILQ01"],
        "n_scanner_fires": 12,
        "fired_symbols": ["BRK00", "BRK01"],
        "ranked_top_5": [{"symbol": "BRK00", "score": 2.5}],
        "cap": 5,
        "n_buys": 5,
        "buy_symbols": ["BRK00", "BRK01", "BRK02", "BRK03", "BRK04"],
        "n_skip_capacity": 7,
        "n_skip_ineligible": 0,
        "n_paper_trades_scanner_tagged": 5,
        "n_paper_trades_total": 5,
        "action_tally": {"BUY": 5, "SKIP_CAPACITY": 7},
    }
    out = smoke._format_trace(trace)
    assert "universe size" in out
    assert "after liquidity" in out
    assert "scanner fires" in out
    assert "Top-5 by ranker score" in out
    assert "BRK00" in out
    assert "SKIP_CAPACITY" in out
    assert "PASS" in out


def test_format_trace_detects_failures(smoke):
    trace = {
        "n_universe": 20, "n_after_liquidity": 20,
        "skipped_liquidity": [],
        "n_scanner_fires": 2,
        "fired_symbols": ["BRK00"],
        "ranked_top_5": [],
        "cap": 5, "n_buys": 0,
        "buy_symbols": [],
        "n_skip_capacity": 0, "n_skip_ineligible": 0,
        "n_paper_trades_scanner_tagged": 0,
        "n_paper_trades_total": 0,
        "action_tally": {},
    }
    out = smoke._format_trace(trace)
    assert "FAIL" in out
    assert "scanner fires" in out
    assert "liquidity filter" in out


# ---------------------------------------------------------------------------
# End-to-end harness
# ---------------------------------------------------------------------------

def test_smoke_test_full_pipeline_passes(smoke):
    """Walk the entire pipeline and assert every stage fired."""
    trace = smoke.smoke_test(cap=5)

    # universe → liquidity
    assert trace["n_universe"] == smoke.N_SYNTHETIC_SYMBOLS
    assert trace["n_after_liquidity"] == (
        smoke.N_SYNTHETIC_SYMBOLS - len(smoke.ILLIQUID_SYMBOLS))
    assert set(trace["skipped_liquidity"]) == set(smoke.ILLIQUID_SYMBOLS)

    # scanner — every breakout symbol must fire, no flats / illiquids
    fired = set(trace["fired_symbols"])
    assert fired == set(smoke.BREAKOUT_SYMBOLS)
    assert not (fired & set(smoke.FLAT_SYMBOLS))
    assert not (fired & set(smoke.ILLIQUID_SYMBOLS))

    # ranker — top-5 returned, all positive
    assert len(trace["ranked_top_5"]) == 5
    assert all(r["score"] > 0 for r in trace["ranked_top_5"])

    # capacity cap honoured — exactly `cap` buys
    assert trace["n_buys"] == 5
    assert trace["n_skip_capacity"] == trace["n_scanner_fires"] - 5

    # every paper trade carries the scanner tag
    assert trace["n_paper_trades_scanner_tagged"] == trace["n_buys"]
    assert trace["n_paper_trades_total"] == trace["n_buys"]


def test_smoke_test_cap_zero_means_uncapped(smoke):
    """cap=0 disables the capacity check — every fire that's eligible
    should fire (subject to other gates)."""
    trace = smoke.smoke_test(cap=0)
    # With cap disabled, no SKIP_CAPACITY rows.
    assert trace["n_skip_capacity"] == 0
    # buys should equal scanner fires (12) — every BRK fired and passed
    # eligibility.
    assert trace["n_buys"] == trace["n_scanner_fires"]


def test_smoke_test_cap_one(smoke):
    """cap=1 — only the top-ranked fire submits."""
    trace = smoke.smoke_test(cap=1)
    assert trace["n_buys"] == 1
    assert trace["n_skip_capacity"] == trace["n_scanner_fires"] - 1
