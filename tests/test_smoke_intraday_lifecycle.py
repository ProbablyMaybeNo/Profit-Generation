"""Unit tests for scripts/smoke_intraday_lifecycle.py (milestone 5.7.1).

The smoke script itself is an integration runner; these unit tests cover
its scaffolding (synthetic-bar generator, declaration shape, log
formatter) and an end-to-end assertion harness that drives the full
lifecycle (intraday fire-check → auto_trader entry/exit →
close_intraday_positions sweep).
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
    """Import the script as a module (it's not in a package)."""
    spec = importlib.util.spec_from_file_location(
        "smoke_intraday_lifecycle",
        ROOT / "scripts" / "smoke_intraday_lifecycle.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# synthetic bar generator
# ---------------------------------------------------------------------------

def test_synthetic_bars_default_count(smoke):
    bars = smoke._generate_synthetic_intraday_bars()
    assert len(bars) == 30


def test_synthetic_bars_have_ohlcv(smoke):
    bars = smoke._generate_synthetic_intraday_bars(n_bars=10, interval_min=15)
    for col in ("open", "high", "low", "close", "volume"):
        assert col in bars.columns
    for _, row in bars.iterrows():
        assert row["high"] >= row["low"]
        assert row["volume"] > 0


def test_synthetic_bars_index_spacing(smoke):
    """15-minute bars should be 15 minutes apart."""
    bars = smoke._generate_synthetic_intraday_bars(n_bars=5, interval_min=15)
    gaps = bars.index.to_series().diff().dropna().unique()
    assert len(gaps) == 1
    assert int(gaps[0].total_seconds() / 60) == 15


def test_synthetic_bars_have_dip_then_rebound(smoke):
    """Series must dip into the closing window then rebound — otherwise
    the lifecycle won't trigger both entry and exit."""
    bars = smoke._generate_synthetic_intraday_bars()
    closes = bars["close"].tolist()
    min_idx = closes.index(min(closes))
    # Minimum lies in the back half of the series.
    assert min_idx >= len(closes) // 2
    # Rebound after the min.
    assert closes[-1] > closes[min_idx]


# ---------------------------------------------------------------------------
# declaration + settings helpers
# ---------------------------------------------------------------------------

def test_declaration_has_required_intraday_fields(smoke):
    d = smoke._declaration()
    assert d["bar_interval"] == "15m"
    assert d["compute"] == "compute_3bar_low_intraday"
    assert d["grace_period"] is True
    assert d["pyramidable"] is False
    assert smoke.SYNTHETIC_SYMBOL in d["active_on"]


def test_make_settings_dry_run_false_and_enabled(smoke):
    s = smoke._make_settings()
    assert s["enabled"] is True
    assert s["dry_run"] is False  # smoke needs paper_trades writes to flow
    assert s["max_position_usd"] == 1000.0


# ---------------------------------------------------------------------------
# log formatter
# ---------------------------------------------------------------------------

def test_format_human_log_contains_expected_blocks(smoke):
    report = {
        "strategy_id": "x",
        "interval": "15m",
        "n_bars": 30, "n_fires": 4,
        "n_entry_fires": 3, "n_exit_fires": 1,
        "entries": 1, "exits_via_signal": 1,
        "close_out": {"status": "OK", "scanned": 1, "closed": 1,
                       "skipped": 0},
        "entry_price": 99.0, "exit_price": 101.0, "total_qty": 10.0,
        "approx_pnl_usd": 20.0,
        "trade_log": [
            {"bar_index": 5, "bar_dt": "2026-05-18T10:45:00", "close": 99.0,
             "action": "BUY", "qty": 10},
        ],
        "fires_log": [
            {"bar_index": 5, "bar_ts": "2026-05-18T10:45:00",
             "strategy_id": "x", "symbol": "SPY",
             "signal_type": "long_entry", "close": 99.0},
        ],
    }
    out = smoke._format_human_log(report)
    assert "SMOKE TEST" in out
    assert "interval: 15m" in out
    assert "EOD close-out" in out
    assert "BUY" in out
    assert "long_entry" in out


# ---------------------------------------------------------------------------
# End-to-end harness
# ---------------------------------------------------------------------------

def test_smoke_run_emits_full_lifecycle(smoke):
    """The full chain must fire: signal commit → entry → close-out."""
    report = smoke._run_smoke(interval="15m")
    assert report["n_entry_fires"] >= 1, (
        f"no entry fires — wiring appears broken; report={report}"
    )
    assert report["entries"] >= 1, (
        f"no auto_trader BUYs — process_signals not picking up the "
        f"intraday signal. report={report}"
    )
    # Either an exit signal fired OR the close-out swept the position;
    # both prove the position-flat discipline is intact.
    swept = report["close_out"]["closed"] + report["exits_via_signal"]
    assert swept >= 1, (
        f"no position closed via signal OR EOD sweep; report={report}"
    )


def test_smoke_close_out_runs_in_dry_run_path(smoke):
    """close_intraday_positions executed and returned status='OK'."""
    report = smoke._run_smoke(interval="15m")
    assert report["close_out"]["status"] == "OK"


def test_smoke_log_has_expected_action_types(smoke):
    report = smoke._run_smoke(interval="15m")
    actions = {t["action"] for t in report["trade_log"]}
    # Live (non-dry) BUY is the minimum signal that the auto_trader saw
    # an eligible intraday entry. The smoke uses dry_run=False with a
    # stubbed submitter so we expect "BUY" (not "DRY_BUY").
    assert "BUY" in actions, f"BUY not in actions: {actions}"


def test_smoke_test_public_entrypoint(smoke):
    """smoke_test() public entrypoint returns a valid report."""
    report = smoke.smoke_test(interval="15m")
    assert report["strategy_id"].startswith("smoke-intraday-")
    assert report["interval"] == "15m"
    assert report["n_bars"] == 30
