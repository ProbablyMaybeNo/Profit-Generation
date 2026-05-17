import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402


@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    test_settings = tmp_path / "settings.json"
    test_settings.write_text(json.dumps({
        "dashboard_port": 8080,
        "auto_trade": {
            "enabled": False,
            "dry_run": True,
            "min_outcomes": 30,
            "min_mean_ret_pct": 0.0,
            "min_sharpe_ish": 0.10,
            "max_position_usd": 1000,
        },
    }, indent=2), encoding="utf-8")
    monkeypatch.setattr(srv, "SETTINGS_FILE", test_settings)
    yield test_settings


@pytest.fixture()
def client(isolated_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "trading.db")
    db.init_db(tmp_path / "trading.db")
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def test_toggle_enabled_writes_settings(client, isolated_settings):
    rv = client.post("/api/auto_trade/toggle",
                     json={"key": "enabled", "value": True})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is True
    assert body["previous"] is False
    assert body["current"] is True
    settings = json.loads(isolated_settings.read_text())
    assert settings["auto_trade"]["enabled"] is True


def test_toggle_dry_run_writes_settings(client, isolated_settings):
    client.post("/api/auto_trade/toggle", json={"key": "dry_run", "value": False})
    settings = json.loads(isolated_settings.read_text())
    assert settings["auto_trade"]["dry_run"] is False


def test_toggle_rejects_unknown_key(client, isolated_settings):
    rv = client.post("/api/auto_trade/toggle",
                     json={"key": "max_position_usd", "value": 9999})
    assert rv.status_code == 400
    settings = json.loads(isolated_settings.read_text())
    assert settings["auto_trade"]["max_position_usd"] == 1000


def test_toggle_rejects_non_bool(client, isolated_settings):
    rv = client.post("/api/auto_trade/toggle",
                     json={"key": "enabled", "value": "yes"})
    assert rv.status_code == 400


def test_toggle_rejects_non_object_body(client):
    rv = client.post("/api/auto_trade/toggle", json=["enabled", True])
    assert rv.status_code == 400


def test_toggle_loopback_only_blocks_remote(client, monkeypatch):
    monkeypatch.setattr(srv, "_is_loopback_request", lambda: False)
    rv = client.post("/api/auto_trade/toggle",
                     json={"key": "enabled", "value": True})
    assert rv.status_code == 403


def test_state_includes_auto_trade_settings(client, isolated_settings):
    rv = client.get("/api/state")
    assert rv.status_code == 200
    s = rv.get_json()
    assert "auto_trade_settings" in s
    assert s["auto_trade_settings"]["enabled"] is False
    assert s["auto_trade_settings"]["dry_run"] is True


def test_toggle_then_state_reflects_change(client, isolated_settings):
    client.post("/api/auto_trade/toggle", json={"key": "enabled", "value": True})
    s = client.get("/api/state").get_json()
    assert s["auto_trade_settings"]["enabled"] is True


def test_toggle_atomic_write_preserves_other_settings(client, isolated_settings):
    """Toggling auto_trade.enabled must not clobber dashboard_port or other top-level keys."""
    client.post("/api/auto_trade/toggle", json={"key": "enabled", "value": True})
    settings = json.loads(isolated_settings.read_text())
    assert settings["dashboard_port"] == 8080
    assert settings["auto_trade"]["max_position_usd"] == 1000


def test_loopback_check_helper(monkeypatch):
    """Direct unit test of _is_loopback_request semantics."""
    with srv.app.test_request_context("/api/auto_trade/toggle",
                                       environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert srv._is_loopback_request() is True
    with srv.app.test_request_context("/api/auto_trade/toggle",
                                       environ_base={"REMOTE_ADDR": "::1"}):
        assert srv._is_loopback_request() is True
    with srv.app.test_request_context("/api/auto_trade/toggle",
                                       environ_base={"REMOTE_ADDR": "192.168.1.50"}):
        assert srv._is_loopback_request() is False


# ----- Kill switch endpoint + /api/state exposure (3.1.1) -----

@pytest.fixture()
def isolated_kill_switch(tmp_path, monkeypatch):
    from monitoring import kill_switch as ks
    test_file = tmp_path / "kill_switch.json"
    monkeypatch.setattr(ks, "KILL_SWITCH_FILE", test_file)
    return test_file


def test_state_includes_kill_switch_default_off(client, isolated_kill_switch):
    s = client.get("/api/state").get_json()
    assert "kill_switch" in s
    assert s["kill_switch"]["live_trading_halted"] is False
    assert s["kill_switch"]["reason"] == ""


def test_state_reflects_engaged_kill_switch(client, isolated_kill_switch):
    from monitoring import kill_switch as ks
    ks.engage("blowout in progress")
    s = client.get("/api/state").get_json()
    assert s["kill_switch"]["live_trading_halted"] is True
    assert s["kill_switch"]["reason"] == "blowout in progress"


def test_engage_via_endpoint(client, isolated_kill_switch):
    rv = client.post("/api/kill_switch",
                     json={"action": "engage", "reason": "from dashboard"})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is True
    assert body["kill_switch"]["live_trading_halted"] is True
    assert body["kill_switch"]["reason"] == "from dashboard"
    s = client.get("/api/state").get_json()
    assert s["kill_switch"]["live_trading_halted"] is True


def test_release_via_endpoint(client, isolated_kill_switch):
    client.post("/api/kill_switch",
                 json={"action": "engage", "reason": "first"})
    rv = client.post("/api/kill_switch", json={"action": "release"})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["kill_switch"]["live_trading_halted"] is False


def test_kill_switch_endpoint_rejects_unknown_action(client, isolated_kill_switch):
    rv = client.post("/api/kill_switch", json={"action": "toggle"})
    assert rv.status_code == 400


def test_kill_switch_endpoint_rejects_non_object(client, isolated_kill_switch):
    rv = client.post("/api/kill_switch", json=["engage"])
    assert rv.status_code == 400


def test_kill_switch_endpoint_loopback_only(client, isolated_kill_switch, monkeypatch):
    monkeypatch.setattr(srv, "_is_loopback_request", lambda: False)
    rv = client.post("/api/kill_switch",
                     json={"action": "engage", "reason": "x"})
    assert rv.status_code == 403
