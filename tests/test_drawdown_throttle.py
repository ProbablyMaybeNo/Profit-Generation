import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import drawdown_throttle as dt  # noqa: E402


# ---- Pure evaluate() ------------------------------------------------------

def test_evaluate_no_history_returns_full():
    info = dt.evaluate(current_pv=10000, peak_pv=None)
    assert info["multiplier"] == 1.0
    assert info["level"] == "full"
    assert info["trip_kill_switch"] is False


def test_evaluate_full_size_above_thresholds():
    info = dt.evaluate(current_pv=9800, peak_pv=10000)  # 98%
    assert info["multiplier"] == 1.0
    assert info["level"] == "full"


def test_evaluate_halve_at_95():
    info = dt.evaluate(current_pv=9500, peak_pv=10000)  # 95%
    assert info["multiplier"] == 0.5
    assert info["level"] == "half"


def test_evaluate_halve_at_94():
    info = dt.evaluate(current_pv=9400, peak_pv=10000)  # 94%
    assert info["multiplier"] == 0.5


def test_evaluate_quarter_at_90():
    info = dt.evaluate(current_pv=9000, peak_pv=10000)  # 90%
    assert info["multiplier"] == 0.25
    assert info["level"] == "quarter"


def test_evaluate_quarter_at_87():
    info = dt.evaluate(current_pv=8700, peak_pv=10000)
    assert info["multiplier"] == 0.25


def test_evaluate_halt_at_85():
    info = dt.evaluate(current_pv=8500, peak_pv=10000)  # 85%
    assert info["multiplier"] == 0.0
    assert info["level"] == "halt"
    assert info["trip_kill_switch"] is True
    assert "85" in info["reason"] or "halt" in info["reason"].lower()


def test_evaluate_halt_at_70():
    info = dt.evaluate(current_pv=7000, peak_pv=10000)
    assert info["multiplier"] == 0.0
    assert info["trip_kill_switch"] is True


def test_evaluate_just_above_quarter_threshold():
    """91% should still be halved (not quartered)."""
    info = dt.evaluate(current_pv=9100, peak_pv=10000)
    assert info["multiplier"] == 0.5


def test_evaluate_recover_at_97():
    """When PV recovers above 97% of peak we go back to full size."""
    info = dt.evaluate(current_pv=9700, peak_pv=10000)
    # 97% sits below the halve threshold (95%) → still half? No: 97 > 95 → full.
    assert info["multiplier"] == 1.0


def test_evaluate_custom_thresholds_respected():
    info = dt.evaluate(
        current_pv=8000, peak_pv=10000,  # 80%
        settings_throttle={"halve_at_pct": 75.0, "quarter_at_pct": 65.0,
                            "kill_at_pct": 50.0},
    )
    # 80% > 75% under custom config → full size
    assert info["multiplier"] == 1.0


def test_evaluate_garbage_settings_falls_back_to_defaults():
    info = dt.evaluate(
        current_pv=9500, peak_pv=10000,
        settings_throttle={"halve_at_pct": "garbage", "quarter_at_pct": -1},
    )
    # Defaults restored → 95% still triggers halve.
    assert info["multiplier"] == 0.5


def test_evaluate_zero_or_negative_pv_returns_full():
    info = dt.evaluate(current_pv=0, peak_pv=10000)
    assert info["multiplier"] == 1.0
    info = dt.evaluate(current_pv=10000, peak_pv=0)
    assert info["multiplier"] == 1.0


# ---- Kill-switch engagement -----------------------------------------------

def test_maybe_engage_skip_when_no_trip():
    info = dt.evaluate(current_pv=10000, peak_pv=10000)
    engaged = []
    dt.maybe_engage_kill_switch(info, engage_fn=lambda r: engaged.append(r))
    assert engaged == []


def test_maybe_engage_fires_on_halt():
    info = dt.evaluate(current_pv=8000, peak_pv=10000)
    engaged = []
    dt.maybe_engage_kill_switch(info, engage_fn=lambda r: engaged.append(r))
    assert len(engaged) == 1
    assert "8000" in engaged[0] or "80" in engaged[0]


# ---- DB helpers -----------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def test_record_and_query_equity_snapshot(isolated_db):
    conn = db.init_db()
    db.record_equity_snapshot(
        conn, portfolio_value=10000.0,
        recorded_at="2026-04-01T00:00:00+00:00",
    )
    db.record_equity_snapshot(
        conn, portfolio_value=10500.0,
        recorded_at="2026-04-15T00:00:00+00:00",
    )
    db.record_equity_snapshot(
        conn, portfolio_value=9800.0,
        recorded_at="2026-05-01T00:00:00+00:00",
    )
    peak = db.trailing_peak_portfolio_value(
        conn, window_days=30,
        asof="2026-05-15T00:00:00+00:00",
    )
    # 30d window from 2026-04-15 includes the 10500 row.
    assert peak == 10500.0


def test_trailing_peak_empty_returns_none(isolated_db):
    conn = db.init_db()
    peak = db.trailing_peak_portfolio_value(
        conn, asof="2026-05-15T00:00:00+00:00",
    )
    assert peak is None


def test_record_equity_snapshot_upserts_same_ts(isolated_db):
    conn = db.init_db()
    db.record_equity_snapshot(
        conn, portfolio_value=10000.0,
        recorded_at="2026-05-01T00:00:00+00:00",
    )
    db.record_equity_snapshot(
        conn, portfolio_value=11000.0,
        recorded_at="2026-05-01T00:00:00+00:00",  # same ts
    )
    rows = conn.execute(
        "SELECT portfolio_value FROM equity_snapshots ORDER BY recorded_at"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["portfolio_value"] == 11000.0


# ---- Integration with process_signals ------------------------------------

def _winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 1, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.0,
        "max_position_usd": 1000,
    }


def _seed_winner(conn, *, returns):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    for i, ret in enumerate(returns):
        sid = db.record_signal(
            conn, strategy_id="winner", symbol="X",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0,
            bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )


def test_auto_trader_throttle_halves_notional(isolated_db, monkeypatch):
    """Seed equity history where current PV is 93% of peak — between
    halve (95%) and quarter (90%) → multiplier should be 0.5."""
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    # Seed peak equity inside the trailing-30d window (use "yesterday" UTC
    # so we're safely within range regardless of when the test runs).
    yesterday = (datetime.now(timezone.utc)
                  - timedelta(days=1)).isoformat(timespec="seconds")
    db.record_equity_snapshot(
        conn, portfolio_value=10000.0, recorded_at=yesterday,
    )
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "risk": {"max_pct_per_symbol": 1.0}}  # disable concentration cap
    # Current PV = 9300 (93%) → halve.
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {"portfolio_value": 9300.0,
                                     "equity_at_open": 9300.0,
                                     "last_equity": 9300.0},
    )
    assert "throttle" in res
    assert res["throttle"]["level"] == "half"
    assert res["throttle"]["multiplier"] == 0.5
    action = [a for a in res["actions"] if a.get("action") == "DRY_BUY"][0]
    # Default tier sizing isn't on for this test (sizing_method default = fixed
    # → notional=max_position_usd=1000 * 0.5 = 500).
    assert action["sizing"]["throttle_multiplier"] == 0.5
    assert action["sizing"]["notional_after_throttle"] == 500.0


def test_auto_trader_throttle_halt_skips_entries(isolated_db, monkeypatch):
    """Below 85% trips multiplier=0 → notional=0 → SKIP_SIZING_ZERO."""
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    from monitoring import kill_switch as ks
    test_ks = isolated_db.parent / "kill_switch.json"
    monkeypatch.setattr(ks, "KILL_SWITCH_FILE", test_ks)
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    yesterday = (datetime.now(timezone.utc)
                  - timedelta(days=1)).isoformat(timespec="seconds")
    db.record_equity_snapshot(
        conn, portfolio_value=10000.0, recorded_at=yesterday,
    )
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = _winner_settings()
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {"portfolio_value": 8000.0},  # 80% → halt
    )
    assert res["throttle"]["level"] == "halt"
    assert res["throttle"]["trip_kill_switch"] is True
    action = [a for a in res["actions"] if a["strategy_id"] == "winner"][0]
    # Throttle trip → kill switch engaged BEFORE the per-signal read pass,
    # so the entry is blocked as KILL_SWITCH_HALT in this same run.
    assert action["action"] == "KILL_SWITCH_HALT"
    assert ks.is_halted() is True


def test_auto_trader_no_throttle_when_no_history(isolated_db, monkeypatch):
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "risk": {"max_pct_per_symbol": 1.0}}
    # First run — no equity history yet. Snapshot is recorded for the
    # first time during the run, so peak == current → full size.
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {"portfolio_value": 10000.0},
    )
    assert res["throttle"]["multiplier"] == 1.0
    action = [a for a in res["actions"] if a.get("action") == "DRY_BUY"][0]
    assert action["sizing"]["throttle_multiplier"] == 1.0
