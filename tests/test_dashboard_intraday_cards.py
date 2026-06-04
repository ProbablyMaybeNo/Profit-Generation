"""7.5.3 — Dashboard cards: live feed status, intraday bars, skip reasons.

Tests for the two new routes shipped in this milestone:
- /api/live_feed_status — enriched live stream card
- /api/intraday_bars_latest — most recent 1m bar per subscribed symbol
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard import server as srv  # noqa: E402
from data import db  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", db_path)
    monkeypatch.setattr(srv.db, "DB_FILE", db_path)
    conn = db.init_db(db_path)
    conn.close()
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# /api/live_feed_status
# ---------------------------------------------------------------------------

def test_live_feed_status_returns_not_started_when_no_row(client):
    r = client.get("/api/live_feed_status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["feed"] == "Alpaca IEX"
    assert body["state"] == "not_started"
    assert body["last_ts"] is None
    assert body["seconds_since_last_message"] is None
    assert body["reconnects_today"] == 0
    assert body["stale"] is False
    # A4: count is the listener's real subscribed universe (full intraday
    # universe), not the stale TRACKED_STOCKS + TRACKED_SECTORS 10.
    from monitoring.live_stream import DEFAULT_UNIVERSE
    assert body["subscribed_symbol_count"] == len(DEFAULT_UNIVERSE)
    assert body["subscribed_symbol_count"] > 10


def test_live_feed_status_returns_connected_with_recent_heartbeat(client, tmp_path):
    conn = db.init_db(tmp_path / "trading.db")
    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO stream_heartbeat "
            "(component, last_ts, reconnects_today, last_error, "
            " rollover_date, state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("live_stream", recent, 1, None, recent[:10], "connected"),
        )
    conn.close()
    r = client.get("/api/live_feed_status")
    body = r.get_json()
    assert body["state"] == "connected"
    assert body["reconnects_today"] == 1
    assert body["stale"] is False
    assert body["seconds_since_last_message"] is not None
    assert body["seconds_since_last_message"] < 5


def test_live_feed_status_marks_stale_when_old_heartbeat(client, tmp_path):
    """A connected heartbeat older than LIVE_FEED_STALE_SECONDS flips stale=True."""
    conn = db.init_db(tmp_path / "trading.db")
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=600)
    ).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO stream_heartbeat "
            "(component, last_ts, reconnects_today, last_error, "
            " rollover_date, state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("live_stream", old, 0, None, old[:10], "connected"),
        )
    conn.close()
    r = client.get("/api/live_feed_status")
    body = r.get_json()
    assert body["state"] == "connected"
    assert body["stale"] is True
    assert body["seconds_since_last_message"] >= 600


def test_live_feed_status_loopback_only(client):
    with patch("dashboard.server._is_loopback_request", return_value=False):
        r = client.get("/api/live_feed_status")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /api/intraday_bars_latest
# ---------------------------------------------------------------------------

def _seed_bar(conn, symbol, ts_utc, *, close=100.0, volume=1000.0):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO intraday_bars "
            "(symbol, ts_utc, open, high, low, close, volume, source, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'iex', ?)",
            (symbol, ts_utc, close, close, close, close, volume, ts_utc),
        )


def test_intraday_bars_latest_empty_table_lists_universe_with_no_data(client):
    r = client.get("/api/intraday_bars_latest")
    body = r.get_json()
    assert body["ok"] is True
    syms = body["symbols"]
    # A4: full intraday universe listed, all NO_DATA when the table is empty.
    from monitoring.live_stream import DEFAULT_UNIVERSE
    assert len(syms) == len(DEFAULT_UNIVERSE)
    assert len(syms) > 10
    for entry in syms:
        assert entry["bar"] is None
        assert entry["freshness"] == "NO_DATA"


def test_intraday_bars_latest_fresh_when_recent(client, tmp_path):
    conn = db.init_db(tmp_path / "trading.db")
    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _seed_bar(conn, "SPY", recent, close=520.5)
    conn.close()
    r = client.get("/api/intraday_bars_latest")
    body = r.get_json()
    spy = next(s for s in body["symbols"] if s["symbol"] == "SPY")
    assert spy["freshness"] == "FRESH"
    assert spy["bar"]["close"] == pytest.approx(520.5)
    assert spy["seconds_ago"] is not None
    assert spy["seconds_ago"] < 5


def test_intraday_bars_latest_stale_when_old(client, tmp_path):
    """A bar older than INTRADAY_BAR_FRESH_SECONDS flips to STALE."""
    conn = db.init_db(tmp_path / "trading.db")
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=600)
    ).isoformat(timespec="seconds")
    _seed_bar(conn, "QQQ", old, close=410.0)
    conn.close()
    r = client.get("/api/intraday_bars_latest")
    body = r.get_json()
    qqq = next(s for s in body["symbols"] if s["symbol"] == "QQQ")
    assert qqq["freshness"] == "STALE"
    assert qqq["seconds_ago"] >= 600


def test_intraday_bars_latest_returns_only_newest_per_symbol(client, tmp_path):
    """Multiple rows per symbol → only the newest ts_utc appears."""
    conn = db.init_db(tmp_path / "trading.db")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    newest = now.isoformat(timespec="seconds")
    _seed_bar(conn, "IWM", old, close=200.0)
    _seed_bar(conn, "IWM", newest, close=205.0)
    conn.close()
    r = client.get("/api/intraday_bars_latest")
    body = r.get_json()
    iwm = next(s for s in body["symbols"] if s["symbol"] == "IWM")
    assert iwm["bar"]["close"] == pytest.approx(205.0)
    assert iwm["bar"]["ts_utc"] == newest


def test_intraday_bars_latest_universe_ordering_is_stable(client):
    """Universe order: TRACKED_STOCKS first, then TRACKED_SECTORS."""
    r = client.get("/api/intraday_bars_latest")
    body = r.get_json()
    symbols_in_order = [s["symbol"] for s in body["symbols"]]
    assert symbols_in_order[:3] == ["SPY", "QQQ", "IWM"]
    assert "XLE" in symbols_in_order
    assert "XOP" in symbols_in_order


def test_intraday_bars_latest_loopback_only(client):
    with patch("dashboard.server._is_loopback_request", return_value=False):
        r = client.get("/api/intraday_bars_latest")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /api/skip_reasons — already exists from 7.5.2. Smoke-check shape only.
# ---------------------------------------------------------------------------

def test_skip_reasons_route_still_returns_expected_shape(client):
    r = client.get("/api/skip_reasons")
    body = r.get_json()
    assert body["ok"] is True
    assert "top_5" in body
    assert "recent" in body
