"""5.5.5.1 — Daily-report wiring for the wide-universe trend scanner."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import daily_report as dr  # noqa: E402


def test_maybe_run_trend_scanner_skipped_when_flag_off(monkeypatch):
    from monitoring import auto_trader
    monkeypatch.setattr(auto_trader, "_config",
                         lambda: {"trend_scanner_enabled": False, "enabled": True})

    called = []
    from monitoring import trend_scanner
    monkeypatch.setattr(trend_scanner, "scan_trend_universe",
                         lambda **kw: called.append(True) or [])

    rc = dr.maybe_run_trend_scanner()
    assert rc == 0
    assert called == []


def test_maybe_run_trend_scanner_skipped_when_flag_absent(monkeypatch):
    from monitoring import auto_trader
    monkeypatch.setattr(auto_trader, "_config",
                         lambda: {"enabled": True})  # no key at all

    from monitoring import trend_scanner
    called = []
    monkeypatch.setattr(trend_scanner, "scan_trend_universe",
                         lambda **kw: called.append(True) or [])

    rc = dr.maybe_run_trend_scanner()
    assert rc == 0
    assert called == []


def test_maybe_run_trend_scanner_runs_when_flag_on(monkeypatch):
    from monitoring import auto_trader
    monkeypatch.setattr(auto_trader, "_config",
                         lambda: {"trend_scanner_enabled": True, "enabled": True})

    from monitoring import trend_scanner
    fake_fires = [
        {"strategy_id": "trend-x", "symbol": "AAPL"},
        {"strategy_id": "trend-x", "symbol": "MSFT"},
    ]
    monkeypatch.setattr(trend_scanner, "scan_trend_universe",
                         lambda **kw: fake_fires)

    rc = dr.maybe_run_trend_scanner()
    assert rc == 2


def test_maybe_run_trend_scanner_isolates_scanner_exception(monkeypatch):
    from monitoring import auto_trader
    monkeypatch.setattr(auto_trader, "_config",
                         lambda: {"trend_scanner_enabled": True})

    def boom(**kw):
        raise RuntimeError("scanner crashed")

    from monitoring import trend_scanner
    monkeypatch.setattr(trend_scanner, "scan_trend_universe", boom)

    # Must NOT raise — the daily report must still publish
    rc = dr.maybe_run_trend_scanner()
    assert rc == 0


def test_maybe_run_trend_scanner_isolates_settings_exception(monkeypatch):
    from monitoring import auto_trader

    def boom():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(auto_trader, "_config", boom)

    rc = dr.maybe_run_trend_scanner()
    assert rc is None  # signals "couldn't even check"


def test_maybe_run_trend_scanner_zero_fires_still_returns_zero(monkeypatch):
    from monitoring import auto_trader
    monkeypatch.setattr(auto_trader, "_config",
                         lambda: {"trend_scanner_enabled": True})

    from monitoring import trend_scanner
    monkeypatch.setattr(trend_scanner, "scan_trend_universe", lambda **kw: [])

    rc = dr.maybe_run_trend_scanner()
    assert rc == 0
