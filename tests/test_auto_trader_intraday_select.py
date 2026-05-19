"""
test_auto_trader_intraday_select.py — 5.2.1: process_signals widening to
accept bar_interval. Confirms:
  - default '1d' behaviour is unchanged for existing callers
  - bar_interval='15m' filters to 15m signals only (and matches
    datetime-shaped bar_ts via the asof date prefix)
  - other intervals follow the same rule
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_strategy(sid: str):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    conn.close()


def _settings_observe_only():
    """enabled but min_outcomes=999 so every signal becomes SKIP_NO_EDGE —
    that's enough to verify which signals the SELECT picked up."""
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 999, "min_mean_ret_pct": 0.0,
        "min_sharpe_ish": 0.10, "max_position_usd": 1000,
        "skip_intraday_signals": False,
    }


def test_default_bar_interval_unchanged(isolated_db):
    """No bar_interval kwarg passed → only 1d signals on asof are selected."""
    _seed_strategy("s1")
    conn = db.init_db()
    db.record_signal(conn, strategy_id="s1", symbol="SPY",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="s1", symbol="SPY",
                     bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
                     close=101.0, bar_interval="15m")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_settings_observe_only())
    seen = {(a.get("strategy_id"), a.get("symbol"), a.get("signal_id"))
            for a in res["actions"]}
    # 1 EOD signal should be picked up; 15m signal should NOT
    assert len(seen) == 1


def test_explicit_bar_interval_1d_matches_default(isolated_db):
    _seed_strategy("s1")
    conn = db.init_db()
    db.record_signal(conn, strategy_id="s1", symbol="SPY",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_settings_observe_only(),
                              bar_interval="1d")
    assert len(res["actions"]) == 1


def test_15m_bar_interval_filters_to_intraday_only(isolated_db):
    _seed_strategy("s1")
    conn = db.init_db()
    db.record_signal(conn, strategy_id="s1", symbol="SPY",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="s1", symbol="QQQ",
                     bar_ts="2026-05-14T10:00:00", signal_type="long_entry",
                     close=200.0, bar_interval="15m")
    db.record_signal(conn, strategy_id="s1", symbol="IWM",
                     bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
                     close=150.0, bar_interval="15m")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_settings_observe_only(),
                              bar_interval="15m")
    symbols = {a.get("symbol") for a in res["actions"]
               if a.get("symbol") is not None}
    assert symbols == {"QQQ", "IWM"}  # SPY (1d) excluded


def test_5m_bar_interval_filters_independently(isolated_db):
    _seed_strategy("s1")
    conn = db.init_db()
    db.record_signal(conn, strategy_id="s1", symbol="SPY",
                     bar_ts="2026-05-14T09:35:00", signal_type="long_entry",
                     close=100.0, bar_interval="5m")
    db.record_signal(conn, strategy_id="s1", symbol="QQQ",
                     bar_ts="2026-05-14T10:00:00", signal_type="long_entry",
                     close=200.0, bar_interval="15m")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_settings_observe_only(),
                              bar_interval="5m")
    symbols = {a.get("symbol") for a in res["actions"]
               if a.get("symbol") is not None}
    assert symbols == {"SPY"}


def test_1h_bar_interval(isolated_db):
    _seed_strategy("s1")
    conn = db.init_db()
    db.record_signal(conn, strategy_id="s1", symbol="SPY",
                     bar_ts="2026-05-14T13:00:00", signal_type="long_entry",
                     close=100.0, bar_interval="1h")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_settings_observe_only(),
                              bar_interval="1h")
    symbols = {a.get("symbol") for a in res["actions"]
               if a.get("symbol") is not None}
    assert symbols == {"SPY"}


def test_intraday_select_does_not_leak_across_days(isolated_db):
    _seed_strategy("s1")
    conn = db.init_db()
    db.record_signal(conn, strategy_id="s1", symbol="SPY",
                     bar_ts="2026-05-13T14:30:00", signal_type="long_entry",
                     close=100.0, bar_interval="15m")
    db.record_signal(conn, strategy_id="s1", symbol="QQQ",
                     bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
                     close=200.0, bar_interval="15m")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_settings_observe_only(),
                              bar_interval="15m")
    symbols = {a.get("symbol") for a in res["actions"]
               if a.get("symbol") is not None}
    assert symbols == {"QQQ"}


def test_no_intraday_signals_returns_ok_empty(isolated_db):
    _seed_strategy("s1")
    conn = db.init_db()
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_settings_observe_only(),
                              bar_interval="15m")
    assert res["status"] == "OK"
    assert res["actions"] == []
