"""Unit tests for scripts/smoke_trend_lifecycle.py (milestone 4.7.4).

The smoke test itself is an integration runner; these unit tests cover
its scaffolding (synthetic-bar generator, donchian helper, log formatter,
and the end-to-end assertion harness).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def smoke():
    """Import the script as a module (it's not in a package)."""
    spec = importlib.util.spec_from_file_location(
        "smoke_trend_lifecycle",
        ROOT / "scripts" / "smoke_trend_lifecycle.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# synthetic bar generator
# ---------------------------------------------------------------------------

def test_synthetic_ramp_produces_60_bars_by_default(smoke):
    bars = smoke._generate_synthetic_ramp()
    assert len(bars) == 60


def test_synthetic_ramp_each_bar_has_ohlcv(smoke):
    bars = smoke._generate_synthetic_ramp(10)
    for b in bars:
        assert {"open", "high", "low", "close", "volume"} <= set(b)
        assert b["high"] >= b["low"]
        assert b["volume"] > 0


def test_synthetic_ramp_is_uptrend_with_pullback(smoke):
    """First 45 bars should ramp up; last 15 should pull back."""
    bars = smoke._generate_synthetic_ramp(60)
    assert bars[44]["close"] > bars[0]["close"]
    assert bars[59]["close"] < bars[44]["close"]


# ---------------------------------------------------------------------------
# donchian detection helpers
# ---------------------------------------------------------------------------

def test_detect_donchian_entries_fires_on_ramp(smoke):
    bars = smoke._generate_synthetic_ramp(60)
    entries = smoke._detect_donchian_entries(bars, lookback=20)
    # Some entries must fire across the uptrend.
    assert len(entries) > 5
    # Each entry must be after the lookback window.
    assert all(i >= 21 for i in entries)


def test_detect_donchian_exits_fires_on_pullback(smoke):
    bars = smoke._generate_synthetic_ramp(60)
    exits = smoke._detect_donchian_exits(bars, lookback=10)
    # At least one exit fires once price drops below the 10-day low.
    assert len(exits) >= 1


# ---------------------------------------------------------------------------
# Log formatter
# ---------------------------------------------------------------------------

def test_format_human_log_contains_expected_blocks(smoke):
    report = {
        "strategy_id": "x",
        "n_bars": 60, "entries": 1, "pyramids": 3, "exits": 1,
        "entry_price": 100.0, "exit_price": 90.0, "total_qty": 100.0,
        "approx_pnl_usd": -1000.0,
        "trade_log": [
            {"bar_index": 5, "bar_ts": "2024-01-06", "close": 100.0,
             "action": "BUY", "qty": 10, "tier": 0},
            {"bar_index": 6, "bar_ts": "2024-01-07", "close": 102.0,
             "action": "PYRAMID_ADDON", "qty": 5, "tier": 1},
        ],
    }
    out = smoke._format_human_log(report)
    assert "SMOKE TEST" in out
    assert "bars processed" in out
    assert "BUY" in out
    assert "PYRAMID_ADDON" in out


# ---------------------------------------------------------------------------
# End-to-end harness
# ---------------------------------------------------------------------------

def test_smoke_run_emits_full_lifecycle(smoke):
    report = smoke._run_smoke("donchian_breakout_20", dry_run=False)
    # Entry must fire.
    assert report["entries"] >= 1, (
        f"no entries detected — wiring appears broken; report={report}"
    )
    # At least one pyramid add-on must fire on the uptrend.
    assert report["pyramids"] >= 1
    # And an exit must fire on the pullback (either signal or trailing).
    assert report["exits"] >= 1


def test_smoke_log_has_expected_action_types(smoke):
    report = smoke._run_smoke("donchian_breakout_20", dry_run=False)
    actions = {t["action"] for t in report["trade_log"]}
    assert "BUY" in actions
    assert "PYRAMID_ADDON" in actions
    assert "SELL" in actions
