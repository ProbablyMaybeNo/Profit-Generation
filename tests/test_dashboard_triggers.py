import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "trading.db")
    db.init_db(tmp_path / "trading.db")
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    srv._LAST_TRIGGERED.clear()
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


@pytest.fixture()
def fake_popen(monkeypatch):
    """Patch subprocess.Popen so spawning never starts a real process."""
    calls = []
    def factory(args, **kwargs):
        proc = MagicMock()
        proc.pid = 99999
        calls.append({"args": args, "kwargs": kwargs})
        return proc
    monkeypatch.setattr(srv.subprocess, "Popen", factory)
    return calls


def test_trigger_returns_202_and_metadata(client, fake_popen):
    rv = client.post("/api/run/daily_report")
    assert rv.status_code == 202
    body = rv.get_json()
    assert body["ok"] is True
    assert body["trigger_id"] == "daily_report"
    assert body["pid"] == 99999
    assert "started_at" in body
    assert body["args"] == ["monitoring.daily_report"]


def test_trigger_intraday_scan_args(client, fake_popen):
    rv = client.post("/api/run/intraday_scan")
    assert rv.status_code == 202
    body = rv.get_json()
    assert body["args"] == ["monitoring.intraday_monitor", "--once", "--no-market-check"]


def test_trigger_auto_trader(client, fake_popen):
    rv = client.post("/api/run/auto_trader")
    assert rv.status_code == 202
    assert rv.get_json()["args"] == ["monitoring.auto_trader"]


def test_trigger_subprocess_invocation_uses_module_form(client, fake_popen):
    client.post("/api/run/daily_report")
    assert len(fake_popen) == 1
    spawned = fake_popen[0]
    assert spawned["args"][0] == sys.executable
    assert spawned["args"][1] == "-m"
    assert spawned["args"][2] == "monitoring.daily_report"
    assert spawned["kwargs"]["env"]["PYTHONIOENCODING"] == "utf-8"


def test_trigger_unknown_id_returns_404(client, fake_popen):
    rv = client.post("/api/run/something_bogus")
    assert rv.status_code == 404
    assert "allowed" in rv.get_json()["error"]
    assert len(fake_popen) == 0  # no subprocess spawned


def test_trigger_loopback_only(client, fake_popen, monkeypatch):
    monkeypatch.setattr(srv, "_is_loopback_request", lambda: False)
    rv = client.post("/api/run/daily_report")
    assert rv.status_code == 403
    assert len(fake_popen) == 0


def test_trigger_status_endpoint_lists_available(client):
    rv = client.get("/api/run")
    assert rv.status_code == 200
    body = rv.get_json()
    assert "daily_report" in body["available"]
    assert "intraday_scan" in body["available"]
    assert "auto_trader" in body["available"]


def test_trigger_status_endpoint_records_last_triggered(client, fake_popen):
    rv = client.post("/api/run/daily_report")
    assert rv.status_code == 202
    rv2 = client.get("/api/run")
    body = rv2.get_json()
    last = body["last_triggered"]
    assert "daily_report" in last
    assert last["daily_report"]["pid"] == 99999


def test_trigger_spawn_failure_returns_500(client, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("fork bomb prevented")
    monkeypatch.setattr(srv.subprocess, "Popen", boom)
    rv = client.post("/api/run/daily_report")
    assert rv.status_code == 500
    assert "spawn failed" in rv.get_json()["error"]
