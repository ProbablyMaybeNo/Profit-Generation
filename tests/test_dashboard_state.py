import json
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
    monkeypatch.setattr(srv, "_safe_account", lambda: None)  # default: no Alpaca
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    yield test_db


@pytest.fixture()
def client(isolated_db):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def _seed_basic_strategy(symbol="GDX"):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "test-strat"}})
    return conn


def test_state_empty_db_returns_defaults(client):
    rv = client.get("/api/state")
    assert rv.status_code == 200
    s = rv.get_json()
    assert s["market_open"] is False
    assert s["account"] is None
    assert s["today_report"] == {}
    assert s["strategy_edge"] == []
    assert s["open_positions"] == []
    assert s["today_signals"] == []
    assert s["recent_news"] == []
    assert "now" in s


def test_state_today_signals_filters_to_today(client, isolated_db):
    conn = _seed_basic_strategy()
    today = date.today().isoformat()
    db.record_signal(conn, strategy_id="test-strat", symbol="GDX",
                     bar_ts=today, signal_type="long_entry", close=42.0,
                     bar_interval="1d")
    db.record_signal(conn, strategy_id="test-strat", symbol="GDX",
                     bar_ts="2024-01-15", signal_type="long_entry", close=30.0,
                     bar_interval="1d")
    conn.close()
    rv = client.get("/api/state")
    s = rv.get_json()
    assert len(s["today_signals"]) == 1
    assert s["today_signals"][0]["symbol"] == "GDX"
    assert s["today_signals"][0]["close"] == 42.0


def test_state_strategy_edge_aggregates_closed(client, isolated_db):
    conn = _seed_basic_strategy()
    sid = db.record_signal(conn, strategy_id="test-strat", symbol="GDX",
                           bar_ts="2024-08-01", signal_type="long_entry",
                           close=100.0, bar_interval="1d")
    db.open_outcome(conn, signal_id=sid, entry_ts="2024-08-01", entry_price=100.0)
    db.close_outcome(conn, signal_id=sid, exit_ts="2024-08-05",
                     exit_price=110.0, exit_reason="long_exit_signal", bars_held=4)
    sid2 = db.record_signal(conn, strategy_id="test-strat", symbol="GDX",
                            bar_ts="2024-09-01", signal_type="long_entry",
                            close=100.0, bar_interval="1d")
    db.open_outcome(conn, signal_id=sid2, entry_ts="2024-09-01", entry_price=100.0)
    db.close_outcome(conn, signal_id=sid2, exit_ts="2024-09-03",
                     exit_price=95.0, exit_reason="long_exit_signal", bars_held=2)
    conn.close()
    s = client.get("/api/state").get_json()
    edge = s["strategy_edge"]
    assert len(edge) == 1
    assert edge[0]["strategy_id"] == "test-strat"
    assert edge[0]["n"] == 2
    assert abs(edge[0]["mean_ret"] - 2.5) < 0.001  # (10 + -5) / 2 = 2.5
    assert edge[0]["win_rate"] == 0.5
    assert edge[0]["max_loss"] == -5.0
    assert edge[0]["max_win"] == 10.0


def test_state_open_positions_joins_snapshot(client, isolated_db):
    conn = _seed_basic_strategy()
    today = date.today().isoformat()
    sid = db.record_signal(conn, strategy_id="test-strat", symbol="GDX",
                           bar_ts="2026-04-01", signal_type="long_entry",
                           close=80.0, bar_interval="1d")
    db.open_outcome(conn, signal_id=sid, entry_ts="2026-04-01", entry_price=80.0)
    db.record_snapshot_row(conn, today, {
        "symbol": "GDX", "asset_class": "sector_etf", "bar_date": today,
        "close": 92.0, "ret_1d_pct": 1.0, "ret_5d_pct": 5.0, "ret_20d_pct": 10.0,
        "rvol_vs_20d": 1.0, "dist_sma20_pct": 2.0,
    })
    conn.close()
    s = client.get("/api/state").get_json()
    assert len(s["open_positions"]) == 1
    p = s["open_positions"][0]
    assert p["symbol"] == "GDX"
    assert p["entry_price"] == 80.0
    assert p["current_price"] == 92.0
    assert p["unrealised_pct"] == 15.0  # (92-80)/80*100
    assert p["days_open"] is not None and p["days_open"] >= 0


def test_state_open_positions_handles_missing_snapshot(client, isolated_db):
    conn = _seed_basic_strategy()
    sid = db.record_signal(conn, strategy_id="test-strat", symbol="UNTRACKED",
                           bar_ts="2026-04-01", signal_type="long_entry",
                           close=80.0, bar_interval="1d")
    db.open_outcome(conn, signal_id=sid, entry_ts="2026-04-01", entry_price=80.0)
    conn.close()
    s = client.get("/api/state").get_json()
    p = s["open_positions"][0]
    assert p["current_price"] is None
    assert p["unrealised_pct"] is None
    assert p["current_as_of"] is None


def test_state_recent_news(client, isolated_db):
    conn = db.init_db()
    db.insert_news(conn, {
        "polygon_id": "p-1", "published_utc": "2026-05-14T12:00:00Z",
        "symbol": "GDX", "title": "Gold pops", "url": "https://x",
        "publisher": "Reuters",
    })
    db.insert_news(conn, {
        "polygon_id": "p-2", "published_utc": "2026-05-13T12:00:00Z",
        "symbol": "SPY", "title": "Market up", "url": "https://y",
        "publisher": "Bloomberg",
    })
    conn.close()
    s = client.get("/api/state").get_json()
    assert len(s["recent_news"]) == 2
    assert s["recent_news"][0]["symbol"] == "GDX"  # most recent first
    assert s["recent_news"][1]["symbol"] == "SPY"


def test_state_today_report_parses_tags_json(client, isolated_db):
    conn = db.init_db()
    today = date.today().isoformat()
    db.record_daily_report(
        conn, report_date=today, market_regime="trending_up",
        importance=3, fires_count=2, watchlist_count=10, notable_movers_count=4,
        tags=["gap-up", "against-news"], symbols_watched=["SPY", "QQQ"],
        notion_page_id="page-123",
    )
    conn.close()
    s = client.get("/api/state").get_json()
    r = s["today_report"]
    assert r["report_date"] == today
    assert r["importance"] == 3
    assert r["tags"] == ["gap-up", "against-news"]
    assert r["notion_page_id"] == "page-123"
    assert "tags_json" not in r


def test_state_account_returned_when_safe_account_succeeds(client, isolated_db, monkeypatch):
    monkeypatch.setattr(srv, "_safe_account",
                        lambda: {"portfolio_value": 1000.0, "cash": 500.0,
                                 "buying_power": 500.0, "equity": 1000.0,
                                 "daytrade_count": 0})
    s = client.get("/api/state").get_json()
    assert s["account"]["portfolio_value"] == 1000.0


def test_index_serves_html(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"profit generation" in rv.data.lower()
