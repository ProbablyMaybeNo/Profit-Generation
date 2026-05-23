"""Tests for the dashboard live-stream control endpoints (7.5.1 helper)."""

import sys
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


def test_status_returns_not_started_when_no_row(client):
    r = client.get("/api/live_stream/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["live_stream"]["state"] == "not_started"
    assert body["live_stream"]["last_ts"] is None


def test_status_returns_row_when_present(client, tmp_path):
    conn = db.init_db(tmp_path / "trading.db")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO stream_heartbeat "
            "(component, last_ts, reconnects_today, last_error, rollover_date, state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("live_stream", "2026-05-22T23:00:00+00:00", 2, None,
             "2026-05-22", "connected"),
        )
    conn.close()
    r = client.get("/api/live_stream/status")
    assert r.status_code == 200
    ls = r.get_json()["live_stream"]
    assert ls["state"] == "connected"
    assert ls["reconnects_today"] == 2
    assert ls["last_ts"] == "2026-05-22T23:00:00+00:00"


def test_start_spawns_subprocess_and_returns_ok(client):
    with patch("dashboard.server.subprocess.Popen") as mock_popen:
        r = client.post("/api/live_stream/start")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    cmd = args[0]
    assert cmd[0] == "cmd"
    assert cmd[1] == "/c"
    assert "run_live_stream.bat" in cmd[2]
    assert kwargs["stdout"] is srv.subprocess.DEVNULL
    assert kwargs["stderr"] is srv.subprocess.DEVNULL


def test_start_returns_500_when_launcher_missing(client, monkeypatch):
    monkeypatch.setattr(srv, "LIVE_STREAM_BAT", Path("/nonexistent/run_live_stream.bat"))
    r = client.post("/api/live_stream/start")
    assert r.status_code == 500
    assert "launcher not found" in r.get_json()["error"]


def test_state_endpoint_includes_live_stream_key(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.get_json()
    assert "live_stream" in body
    assert body["live_stream"]["state"] in {"not_started", "connected", "unknown"}
