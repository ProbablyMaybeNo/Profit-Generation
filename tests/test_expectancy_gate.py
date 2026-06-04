"""test_expectancy_gate.py — Sprint 2 / M4 expectancy kill/size gate.

Proves:
  - a strategy with N >= 20 closed outcomes and avg return < 0 is auto-paused
    (size-down to zero via the pause mechanism).
  - a strong strategy (N >= 20, avg >= 0) is untouched.
  - a strategy with N < 20 is NOT killed (probation; never killed on noise),
    even if its handful of outcomes are negative.
  - interval scoping: an intraday strategy is judged on its intraday outcomes.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


def _seed_outcomes(conn, sid, *, n, ret_pct, bar_interval="1d"):
    """Seed n closed outcomes each with approx `ret_pct` return."""
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    entry = 100.0
    exit_price = entry * (1.0 + ret_pct / 100.0)
    for i in range(n):
        s = db.record_signal(conn, strategy_id=sid, symbol="W",
                             bar_ts=f"2024-01-{(i % 27) + 1:02d}",
                             signal_type="long_entry", close=entry,
                             bar_interval=bar_interval)
        db.open_outcome(conn, signal_id=s, entry_ts=f"2024-01-{(i % 27) + 1:02d}",
                        entry_price=entry)
        db.close_outcome(conn, signal_id=s, exit_ts=f"2024-02-{(i % 27) + 1:02d}",
                         exit_price=exit_price, exit_reason="long_exit_signal",
                         bars_held=1)


def test_negative_with_enough_sample_is_paused(conn):
    _seed_outcomes(conn, "loser", n=25, ret_pct=-1.5)
    res = sh.evaluate_expectancy_gate(conn, "loser", bar_interval="1d")
    assert res["n"] == 25
    assert res["avg_return_pct"] < 0
    assert res["on_probation"] is False
    assert res["should_pause"] is True


def test_strong_strategy_untouched(conn):
    _seed_outcomes(conn, "winner", n=25, ret_pct=2.0)
    res = sh.evaluate_expectancy_gate(conn, "winner", bar_interval="1d")
    assert res["should_pause"] is False
    assert res["on_probation"] is False


def test_small_sample_negative_is_probation_not_killed(conn):
    _seed_outcomes(conn, "newbie", n=8, ret_pct=-3.0)
    res = sh.evaluate_expectancy_gate(conn, "newbie", bar_interval="1d")
    assert res["n"] == 8
    assert res["on_probation"] is True
    assert res["should_pause"] is False  # never kill on noise


def test_auto_check_pauses_loser_only(conn):
    _seed_outcomes(conn, "loser", n=25, ret_pct=-1.5)
    _seed_outcomes(conn, "winner", n=25, ret_pct=2.0)
    _seed_outcomes(conn, "newbie", n=8, ret_pct=-3.0)

    fired = sh.auto_expectancy_pause_check(conn, send_fn=lambda *a, **k: True)
    paused_ids = sorted(f["strategy_id"] for f in fired)
    assert paused_ids == ["loser"]
    assert sh.is_paused(conn, "loser") is True
    assert sh.is_paused(conn, "winner") is False
    assert sh.is_paused(conn, "newbie") is False  # probation


def test_intraday_judged_on_intraday_outcomes(conn):
    # An intraday strategy with negative intraday outcomes should be killed.
    _seed_outcomes(conn, "intraday-loser", n=25, ret_pct=-0.8,
                   bar_interval="1m")
    fired = sh.auto_expectancy_pause_check(conn, send_fn=lambda *a, **k: True)
    assert "intraday-loser" in {f["strategy_id"] for f in fired}
    assert sh.is_paused(conn, "intraday-loser") is True


def test_already_paused_not_double_counted(conn):
    _seed_outcomes(conn, "loser", n=25, ret_pct=-1.5)
    sh.auto_expectancy_pause_check(conn, send_fn=lambda *a, **k: True)
    fired2 = sh.auto_expectancy_pause_check(conn, send_fn=lambda *a, **k: True)
    assert fired2 == []  # already paused → skipped
