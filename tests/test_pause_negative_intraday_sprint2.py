"""test_pause_negative_intraday_sprint2.py — Sprint 2 / M3.

Proves the six negative-expectancy intraday strategies are paused (observe-only)
and yield NO new entry in process_signals, while trend-donchian-breakout-20 is
unaffected and still produces its entry. Outcomes/exits remain tracked (the
pause only gates entries), so they keep recording for re-evaluation.
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
from scripts import pause_negative_intraday_sprint2 as m3  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 0, "min_mean_ret_pct": -100.0, "min_sharpe_ish": -100.0,
        "max_position_usd": 1000, "skip_intraday_signals": False,
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


def test_m3_pauses_all_six_indefinitely(isolated_db):
    conn = db.init_db()
    rows = m3.pause_negative_intraday(conn)
    ids = sorted(r["strategy_id"] for r in rows)
    assert ids == sorted([
        "intraday-1m-momentum", "intraday-1m-vwap-reclaim", "intraday-1m-orb",
        "intraday-orb-pivots-5m", "intraday-orbo-5m", "rsi2-oversold",
    ])
    for sid in ids:
        assert sh.is_paused(conn, sid) is True
        row = conn.execute(
            "SELECT expires_at, source FROM paused_strategies WHERE strategy_id=?",
            (sid,),
        ).fetchone()
        assert row["expires_at"] is None  # indefinite
        assert row["source"] == m3.PAUSE_SOURCE
    conn.close()


def test_m3_is_idempotent(isolated_db):
    conn = db.init_db()
    m3.pause_negative_intraday(conn)
    m3.pause_negative_intraday(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM paused_strategies WHERE source=?",
        (m3.PAUSE_SOURCE,),
    ).fetchone()[0]
    assert n == 6  # UPSERT, not duplicates
    conn.close()


def test_paused_intraday_yields_no_entry_but_donchian_does(isolated_db):
    conn = db.init_db()
    paused_sid = "intraday-1m-momentum"
    donchian_sid = "trend-donchian-breakout-20"
    _seed_eligible(conn, paused_sid)
    _seed_eligible(conn, donchian_sid)
    m3.pause_negative_intraday(conn)

    # Use a 1d signal for the paused strategy so the (interval-agnostic) pause
    # gate is the thing under test — not the separate intraday eligibility
    # plumbing. The pause blocks the entry regardless of interval.
    db.record_signal(conn, strategy_id=paused_sid, symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    db.record_signal(conn, strategy_id=donchian_sid, symbol="SPY",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=400.0, bar_interval="1d")

    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=_winner_settings())
    actions = res["actions"]

    # Paused intraday: a pause-skip, no buy.
    paused_skips = [a for a in actions
                    if a.get("action") == "SKIP_PAUSED_STRATEGY"
                    and a["strategy_id"] == paused_sid]
    assert len(paused_skips) == 1
    paused_buys = [a for a in actions
                   if a.get("action") in ("DRY_BUY", "BUY")
                   and a["strategy_id"] == paused_sid]
    assert paused_buys == []

    # Donchian: unaffected — still produces its entry.
    assert sh.is_paused(conn, donchian_sid) is False
    donchian_buys = [a for a in actions
                     if a.get("action") in ("DRY_BUY", "BUY")
                     and a["strategy_id"] == donchian_sid]
    assert len(donchian_buys) == 1
    conn.close()
