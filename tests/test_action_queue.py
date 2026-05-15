import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "market_is_open", lambda: True)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "strat-A"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "strat-B"}})
    yield test_db


@pytest.fixture()
def client(isolated_db):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def _today():
    return date.today().isoformat()


def _open_position(conn, strat, sym, entry_price, entry_ts="2026-04-01"):
    sid = db.record_signal(conn, strategy_id=strat, symbol=sym,
                           bar_ts=entry_ts, signal_type="long_entry",
                           close=entry_price, bar_interval="1d")
    db.open_outcome(conn, signal_id=sid, entry_ts=entry_ts, entry_price=entry_price)
    return sid


def _snap(conn, sym, close, on_date=None):
    on_date = on_date or _today()
    db.record_snapshot_row(conn, on_date, {
        "symbol": sym, "asset_class": "sector_etf", "bar_date": on_date,
        "close": close, "ret_1d_pct": 0, "ret_5d_pct": 0, "ret_20d_pct": 0,
        "rvol_vs_20d": 1.0, "dist_sma20_pct": 0,
    })


def test_aq_empty_when_no_fires_no_positions(client):
    s = client.get("/api/state").get_json()
    assert s["action_queue"] == []


def test_aq_exit_row_for_today_exit_on_held_position(client, isolated_db):
    conn = db.init_db()
    _open_position(conn, "strat-A", "GDX", 100.0)
    _snap(conn, "GDX", 110.0)
    db.record_signal(conn, strategy_id="strat-A", symbol="GDX",
                     bar_ts=_today(), signal_type="long_exit",
                     close=110.0, bar_interval="1d")
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    assert len(queue) == 1
    q = queue[0]
    assert q["action"] == "EXIT"
    assert q["symbol"] == "GDX"
    assert q["entry_price"] == 100.0
    assert q["current_price"] == 110.0
    assert q["unrealised_pct"] == 10.0
    assert "1d" in q["intervals"]
    assert "SELL GDX" in q["paste"]
    assert "tradingview.com" in q["tv_url"]


def test_aq_no_exit_when_we_dont_hold(client, isolated_db):
    conn = db.init_db()
    db.record_signal(conn, strategy_id="strat-A", symbol="GDX",
                     bar_ts=_today(), signal_type="long_exit",
                     close=110.0, bar_interval="1d")
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    assert queue == []


def test_aq_enter_row_when_new_signal_not_held(client, isolated_db):
    conn = db.init_db()
    db.record_signal(conn, strategy_id="strat-A", symbol="KRE",
                     bar_ts=_today(), signal_type="long_entry",
                     close=68.0, bar_interval="1d")
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    assert len(queue) == 1
    assert queue[0]["action"] == "ENTER"
    assert queue[0]["symbol"] == "KRE"
    assert queue[0]["current_price"] == 68.0
    assert "BUY KRE" in queue[0]["paste"]


def test_aq_no_enter_when_already_held(client, isolated_db):
    conn = db.init_db()
    _open_position(conn, "strat-A", "GDX", 100.0)
    db.record_signal(conn, strategy_id="strat-A", symbol="GDX",
                     bar_ts=_today(), signal_type="long_entry",
                     close=98.0, bar_interval="1d")
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    assert queue == []


def test_aq_dedupes_across_intervals(client, isolated_db):
    conn = db.init_db()
    db.record_signal(conn, strategy_id="strat-A", symbol="KRE",
                     bar_ts=_today(), signal_type="long_entry",
                     close=68.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="strat-A", symbol="KRE",
                     bar_ts=_today(), signal_type="long_entry",
                     close=68.0, bar_interval="1d-intraday")
    db.record_signal(conn, strategy_id="strat-A", symbol="KRE",
                     bar_ts=_today(), signal_type="long_entry",
                     close=68.0, bar_interval="tv-webhook")
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    assert len(queue) == 1
    assert set(queue[0]["intervals"]) == {"1d", "1d-intraday", "tv-webhook"}


def test_aq_review_row_for_big_unrealised_loss(client, isolated_db):
    conn = db.init_db()
    _open_position(conn, "strat-A", "XHB", 100.0)
    _snap(conn, "XHB", 88.0)  # -12% unrealised
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    assert len(queue) == 1
    q = queue[0]
    assert q["action"] == "REVIEW"
    assert q["unrealised_pct"] == -12.0
    assert q["symbol"] == "XHB"


def test_aq_review_skipped_above_threshold(client, isolated_db):
    conn = db.init_db()
    _open_position(conn, "strat-A", "XHB", 100.0)
    _snap(conn, "XHB", 95.0)  # -5%, above -8% threshold
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    assert queue == []


def test_aq_review_skipped_when_exit_already_queued(client, isolated_db):
    """If today's exit fires, no need for separate REVIEW row on same position."""
    conn = db.init_db()
    _open_position(conn, "strat-A", "XHB", 100.0)
    _snap(conn, "XHB", 88.0)
    db.record_signal(conn, strategy_id="strat-A", symbol="XHB",
                     bar_ts=_today(), signal_type="long_exit",
                     close=88.0, bar_interval="1d")
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    assert len(queue) == 1
    assert queue[0]["action"] == "EXIT"


def test_aq_priority_sort_exits_first(client, isolated_db):
    conn = db.init_db()
    _open_position(conn, "strat-A", "GDX", 100.0)
    _snap(conn, "GDX", 110.0)
    _open_position(conn, "strat-B", "XHB", 100.0)
    _snap(conn, "XHB", 88.0)  # -12% review
    db.record_signal(conn, strategy_id="strat-A", symbol="GDX",
                     bar_ts=_today(), signal_type="long_exit",
                     close=110.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="strat-A", symbol="KRE",
                     bar_ts=_today(), signal_type="long_entry",
                     close=68.0, bar_interval="1d")
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    actions = [q["action"] for q in queue]
    assert actions == ["EXIT", "ENTER", "REVIEW"]


def test_aq_falls_back_to_latest_snapshot_when_signal_close_missing(client, isolated_db):
    conn = db.init_db()
    db.record_signal(conn, strategy_id="strat-A", symbol="KRE",
                     bar_ts=_today(), signal_type="long_entry",
                     close=None, bar_interval="1d")
    _snap(conn, "KRE", 67.50)
    conn.close()
    queue = client.get("/api/state").get_json()["action_queue"]
    assert len(queue) == 1
    assert queue[0]["current_price"] == 67.50
