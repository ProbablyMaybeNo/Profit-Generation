"""
test_auto_trader_intraday.py — 5.2.2: intraday auto-trader trigger.

Covers:
  - intraday_enabled=false short-circuits with DISABLED_INTRADAY
  - intraday_enabled=true processes signals at each configured interval
  - end-to-end: synthetic 15m signal → SUBMIT_DRY action
  - default interval list is ["15m"] when settings doesn't declare one
  - per-interval results carry their bar_interval
"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import auto_trader_intraday as ati  # noqa: E402


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


def _intraday_settings(enabled: bool = True,
                       intervals=None,
                       observe_only: bool = True):
    intervals = intervals if intervals is not None else ["15m"]
    return {
        "auto_trade": {
            "enabled": True,
            "dry_run": True,
            "intraday_enabled": enabled,
            "intraday_intervals": intervals,
            # observe-only: huge min_outcomes => every signal SKIP_NO_EDGE
            "min_outcomes": 999 if observe_only else 0,
            "min_mean_ret_pct": 0.0,
            "min_sharpe_ish": 0.10,
            "max_position_usd": 1000,
        }
    }


def test_disabled_short_circuits(isolated_db):
    res = ati.process_intraday(
        asof=date(2026, 5, 14),
        settings=_intraday_settings(enabled=False),
    )
    assert res["status"] == "DISABLED_INTRADAY"
    assert res["intraday_enabled"] is False
    assert res["results"] == []


def test_disabled_does_not_call_process_signals(isolated_db):
    called = []
    def fake_ps(*args, **kwargs):
        called.append(kwargs.get("bar_interval"))
        return {"status": "OK", "actions": []}
    ati.process_intraday(
        asof=date(2026, 5, 14),
        settings=_intraday_settings(enabled=False),
        process_signals_fn=fake_ps,
    )
    assert called == []


def test_enabled_processes_each_interval(isolated_db):
    calls = []
    def fake_ps(*args, **kwargs):
        calls.append(kwargs.get("bar_interval"))
        return {"status": "OK", "actions": []}
    res = ati.process_intraday(
        asof=date(2026, 5, 14),
        settings=_intraday_settings(enabled=True, intervals=["5m", "15m", "1h"]),
        process_signals_fn=fake_ps,
    )
    assert res["status"] == "OK"
    assert calls == ["5m", "15m", "1h"]
    assert len(res["results"]) == 3
    assert [r["bar_interval"] for r in res["results"]] == ["5m", "15m", "1h"]


def test_default_intervals_is_15m_only(isolated_db):
    """Empty/missing intraday_intervals defaults to ['15m']."""
    calls = []
    def fake_ps(*args, **kwargs):
        calls.append(kwargs.get("bar_interval"))
        return {"status": "OK", "actions": []}
    settings = {"auto_trade": {"enabled": True, "dry_run": True,
                                 "intraday_enabled": True,
                                 "min_outcomes": 0,
                                 "min_mean_ret_pct": 0.0,
                                 "min_sharpe_ish": 0.10,
                                 "max_position_usd": 1000}}
    ati.process_intraday(
        asof=date(2026, 5, 14),
        settings=settings,
        process_signals_fn=fake_ps,
    )
    assert calls == ["15m"]


def test_end_to_end_intraday_signal_to_action(isolated_db):
    """Synthetic 15m long_entry signal → at least one observe-only action."""
    _seed_strategy("mr-intra-15m")
    conn = db.init_db()
    db.record_signal(conn, strategy_id="mr-intra-15m", symbol="SPY",
                     bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
                     close=400.0, bar_interval="15m")
    res = ati.process_intraday(
        asof=date(2026, 5, 14),
        settings=_intraday_settings(enabled=True, intervals=["15m"]),
        conn=conn,
    )
    assert res["status"] == "OK"
    assert len(res["results"]) == 1
    actions = res["results"][0]["actions"]
    # observe-only mode: SKIP_NO_EDGE expected, what we care about is the
    # SELECT actually picked up our 15m signal.
    assert len(actions) >= 1
    assert actions[0].get("symbol") == "SPY"


def test_intervals_isolation_between_calls(isolated_db):
    """A 15m signal must not appear in a 5m run."""
    _seed_strategy("mr-intra-15m")
    conn = db.init_db()
    db.record_signal(conn, strategy_id="mr-intra-15m", symbol="SPY",
                     bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
                     close=400.0, bar_interval="15m")
    res = ati.process_intraday(
        asof=date(2026, 5, 14),
        settings=_intraday_settings(enabled=True, intervals=["5m"]),
        conn=conn,
    )
    assert res["status"] == "OK"
    assert res["results"][0]["actions"] == []


def test_passes_through_process_signals_status(isolated_db):
    def fake_ps(*args, **kwargs):
        return {"status": "DISABLED", "actions": []}
    res = ati.process_intraday(
        asof=date(2026, 5, 14),
        settings=_intraday_settings(enabled=True, intervals=["15m"]),
        process_signals_fn=fake_ps,
    )
    assert res["status"] == "OK"  # wrapper status is OK even if inner is DISABLED
    assert res["results"][0]["status"] == "DISABLED"


def test_intraday_enabled_missing_defaults_to_false(isolated_db):
    settings = {"auto_trade": {"enabled": True, "dry_run": True}}
    res = ati.process_intraday(asof=date(2026, 5, 14), settings=settings)
    assert res["status"] == "DISABLED_INTRADAY"
    assert res["intraday_enabled"] is False


def test_intraday_config_helper():
    cfg = ati._intraday_config({
        "auto_trade": {
            "intraday_enabled": True,
            "intraday_intervals": ["5m", "15m"],
        }
    })
    assert cfg["enabled"] is True
    assert cfg["intervals"] == ["5m", "15m"]


def test_intraday_config_helper_defaults():
    cfg = ati._intraday_config({})
    assert cfg["enabled"] is False
    assert cfg["intervals"] == ["15m"]
