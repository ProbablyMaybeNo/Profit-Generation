import sys
from pathlib import Path

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
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def test_list_guides(client):
    rv = client.get("/api/guide")
    assert rv.status_code == 200
    body = rv.get_json()
    assert "tradingview" in body["available"]


def test_get_known_guide_returns_markdown(client):
    rv = client.get("/api/guide/tradingview")
    assert rv.status_code == 200
    assert "text/markdown" in rv.headers["Content-Type"]
    body = rv.get_data(as_text=True)
    assert "# TradingView" in body
    assert "Glossary" in body


def test_unknown_guide_returns_404(client):
    rv = client.get("/api/guide/nonexistent")
    assert rv.status_code == 404
    body = rv.get_json()
    assert "available" in body
    assert "tradingview" in body["available"]


def test_guide_name_case_insensitive(client):
    rv = client.get("/api/guide/TradingView")
    assert rv.status_code == 200


def test_path_traversal_attempt_returns_404(client):
    """Whitelist enforcement: any name not in GUIDES is rejected."""
    for evil in ["..%2F..%2Fconfig%2Fcredentials.json",
                 "%2E%2E%2Fconfig%2Fcredentials.json",
                 "../../config/credentials.json"]:
        rv = client.get(f"/api/guide/{evil}")
        assert rv.status_code == 404, f"Path-traversal-like name {evil!r} should 404, got {rv.status_code}"


def test_guide_returns_500_if_file_missing(client, monkeypatch):
    monkeypatch.setattr(srv, "GUIDES", {"tradingview": "DOES_NOT_EXIST.md"})
    rv = client.get("/api/guide/tradingview")
    assert rv.status_code == 500
    assert "missing" in rv.get_json()["error"]
