"""test_quarantine_negative_edge.py — Sprint-1 M2.

Proves the quarantine seed pauses the three negative-edge strategies via
the existing pause mechanism, and that a quarantined strategy yields no
entry order in process_signals.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402
from scripts import quarantine_negative_edge as q  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def test_quarantine_pauses_all_three_indefinitely(isolated_db):
    conn = db.init_db()
    rows = q.quarantine(conn)
    assert len(rows) == 3
    ids = sorted(r["strategy_id"] for r in rows)
    assert ids == sorted([
        "intraday-1m-orb",
        "intraday-1m-vwap-reclaim",
        "botnet101-consec-bearish",
    ])
    for sid in ids:
        assert sh.is_paused(conn, sid) is True
        row = conn.execute(
            "SELECT expires_at, reason, source FROM paused_strategies "
            " WHERE strategy_id=?", (sid,),
        ).fetchone()
        assert row["expires_at"] is None  # indefinite
        assert row["source"] == q.QUARANTINE_SOURCE
        assert q.QUARANTINE_REASON in row["reason"]
    conn.close()


def test_quarantine_is_idempotent(isolated_db):
    conn = db.init_db()
    q.quarantine(conn)
    q.quarantine(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM paused_strategies "
        " WHERE source=?", (q.QUARANTINE_SOURCE,),
    ).fetchone()[0]
    assert n == 3  # UPSERT, not duplicate rows
    conn.close()


def _winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 0, "min_mean_ret_pct": -100.0, "min_sharpe_ish": -100.0,
        "max_position_usd": 1000, "skip_intraday_signals": True,
    }


def _seed_eligible(conn, sid):
    record = {"extra": {"strategy_id": sid, "test_runs": [{
        "test_id": f"{sid}-A", "trades": 100,
        "total_return_pct": 100.0, "verdict": "PASS",
    }]}}
    db.upsert_strategy(conn, record)
    for i in range(5):
        s = db.record_signal(
            conn, strategy_id=sid, symbol="W", bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=s, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(conn, signal_id=s, exit_ts=f"2024-01-{i+2:02d}",
                         exit_price=102.0, exit_reason="long_exit_signal",
                         bars_held=1)


def test_quarantined_strategy_yields_no_entry(isolated_db):
    conn = db.init_db()
    sid = "botnet101-consec-bearish"
    _seed_eligible(conn, sid)
    q.quarantine(conn)
    db.record_signal(conn, strategy_id=sid, symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=_winner_settings())
    actions = res["actions"]
    skips = [a for a in actions
             if a.get("action") == "SKIP_PAUSED_STRATEGY"
             and a["strategy_id"] == sid]
    assert len(skips) == 1
    buys = [a for a in actions
            if a.get("action") in ("DRY_BUY", "BUY")
            and a["strategy_id"] == sid]
    assert buys == []
    conn.close()
