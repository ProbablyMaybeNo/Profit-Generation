import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

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
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "loser"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "untested"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


@pytest.fixture()
def winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000, "skip_intraday_signals": True,
    }


def _seed_outcomes(strat, returns):
    """Seed N closed outcomes with given return %s for strategy."""
    conn = db.init_db()
    for i, ret in enumerate(returns):
        sid = db.record_signal(conn, strategy_id=strat, symbol="X",
                               bar_ts=f"2024-01-{i+1:02d}",
                               signal_type="long_entry", close=100.0,
                               bar_interval="1d")
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        exit_price = 100.0 * (1 + ret / 100)
        db.close_outcome(conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
                         exit_price=exit_price, exit_reason="long_exit_signal",
                         bars_held=1)
    return conn


# ----- Eligibility -----

def test_eligible_when_thresholds_met(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    ok, stats = at._is_eligible(conn, "winner", winner_settings)
    assert ok is True
    assert stats["n"] == 36


def test_ineligible_too_few_outcomes(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 5)
    ok, stats = at._is_eligible(conn, "winner", winner_settings)
    assert ok is False
    assert stats["n"] == 10


def test_ineligible_negative_mean(isolated_db, winner_settings):
    conn = _seed_outcomes("loser", [-1.0, 0.0] * 18)
    ok, stats = at._is_eligible(conn, "loser", winner_settings)
    assert ok is False
    assert stats["mean"] < 0


def test_ineligible_low_sharpe(isolated_db, winner_settings):
    # Mean +0.1%, big stdev → low sharpe-ish
    rets = [10, -10] * 18  # 36 outcomes alternating, mean ~0
    conn = _seed_outcomes("winner", rets)
    ok, stats = at._is_eligible(conn, "winner", winner_settings)
    assert ok is False
    assert stats["sharpe"] < winner_settings["min_sharpe_ish"]


def test_ineligible_no_outcomes(isolated_db, winner_settings):
    ok, stats = at._is_eligible(db.init_db(), "untested", winner_settings)
    assert ok is False
    assert stats["n"] == 0


# ----- Sizing -----

def test_calc_qty_floor():
    assert at._calc_qty(67.74, 1000) == 14
    assert at._calc_qty(100.0, 1000) == 10
    assert at._calc_qty(2000.0, 1000) == 0
    assert at._calc_qty(None, 1000) == 0
    assert at._calc_qty(0, 1000) == 0


# ----- Disabled / blocked -----

def test_disabled_short_circuits(isolated_db):
    conn = db.init_db()
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings={"enabled": False, "dry_run": True})
    assert res["status"] == "DISABLED"
    assert res["actions"] == []


def test_blocked_when_not_paper_mode(isolated_db, monkeypatch, winner_settings):
    monkeypatch.setattr(at, "is_paper_mode", lambda: False)
    conn = db.init_db()
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    assert res["status"] == "BLOCKED_LIVE_MODE"


# ----- Dry-run -----

def test_dry_run_logs_buy_no_db_write(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    assert res["status"] == "OK"
    assert res["dry_run"] is True
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert len(actions) == 1
    assert actions[0]["action"] == "DRY_BUY"
    assert actions[0]["qty"] == 14
    n_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert n_trades == 0


def test_dry_run_skip_ineligible(isolated_db, winner_settings):
    conn = _seed_outcomes("loser", [-1.0, 0.0] * 18)
    db.record_signal(conn, strategy_id="loser", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    losers = [a for a in res["actions"] if a["strategy_id"] == "loser"]
    assert len(losers) == 1
    assert losers[0]["action"] == "SKIP_INELIGIBLE"


# ----- Live-mode order submission (mocked client) -----

def _mk_client():
    client = MagicMock()
    client._submitted = []
    return client


@pytest.fixture()
def stub_submit(monkeypatch):
    """Replace _submit_market_order so the alpaca-py import never runs."""
    submitted = []
    def fake_submit(client, *, symbol, qty, side):
        submitted.append((symbol, qty, side))
        order = MagicMock()
        order.id = f"alpaca-order-{len(submitted)}"
        order.status = "accepted"
        order.submitted_at = "2026-05-14T20:30:00Z"
        client._submitted = submitted
        return order
    monkeypatch.setattr(at, "_submit_market_order", fake_submit)
    return submitted


def test_live_buy_submits_and_records(isolated_db, winner_settings, stub_submit):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": False}
    client = _mk_client()
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=settings, client=client)
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert len(actions) == 1
    assert actions[0]["action"] == "BUY"
    assert actions[0]["qty"] == 14
    assert actions[0]["order_id"] == "alpaca-order-1"
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE strategy_id='winner'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["side"] == "buy"
    assert rows[0]["qty"] == 14
    assert rows[0]["alpaca_order_id"] == "alpaca-order-1"
    assert ("GDX", 14, "buy") in stub_submit


def test_live_re_run_is_idempotent_via_signal_id_dedupe(isolated_db, winner_settings, stub_submit):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": False}
    client = _mk_client()
    at.process_signals(conn, asof=date(2026, 5, 14), settings=settings, client=client)
    res2 = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings, client=client)
    actions2 = [a for a in res2["actions"] if a["strategy_id"] == "winner"]
    assert actions2[0]["action"] == "SKIP_DUPLICATE"
    n = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert n == 1


def test_live_exit_closes_open_position(isolated_db, winner_settings, stub_submit):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": False}
    client = _mk_client()
    at.process_signals(conn, asof=date(2026, 5, 14), settings=settings, client=client)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-15", signal_type="long_exit",
                     close=72.0, bar_interval="1d")
    res2 = at.process_signals(conn, asof=date(2026, 5, 15),
                              settings=settings, client=client)
    sells = [a for a in res2["actions"] if a["strategy_id"] == "winner"]
    assert len(sells) == 1
    assert sells[0]["action"] == "SELL"
    assert sells[0]["qty"] == 14


def test_exit_no_position_skips(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_exit",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": False}
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=settings, client=_mk_client())
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "SKIP_NO_POSITION"


def test_skips_intraday_bar_interval(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d-intraday")
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    assert all(a["strategy_id"] != "winner" for a in res["actions"]) or res["actions"] == []


def test_asof_filters_signals(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="winner", symbol="KRE",
                     bar_ts="2026-05-15", signal_type="long_entry",
                     close=68.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    syms = [a["symbol"] for a in res["actions"]]
    assert "GDX" in syms
    assert "KRE" not in syms


def test_qty_zero_skips_when_price_too_high(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="BRK.A",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=600000.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "SKIP_PRICE"


def test_alpaca_failure_logged_not_raised(isolated_db, winner_settings, monkeypatch):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    def boom(*a, **kw):
        raise RuntimeError("alpaca down")
    monkeypatch.setattr(at, "_submit_market_order", boom)
    settings = {**winner_settings, "dry_run": False}
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=settings, client=MagicMock())
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "ERROR"
    assert "alpaca down" in actions[0]["error"]
    n = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert n == 0
