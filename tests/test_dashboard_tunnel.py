"""Tests for the TV-webhook Cloudflare-tunnel dashboard surface (2.6.5).

The .bat tunnel launcher itself is external (depends on cloudflared) —
visual / integration tests for it are skipped per the milestone. These
tests cover the dashboard side: the tunnel-url file read, the /api/tunnel
endpoint, and the index.html surface rendering the card.
"""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(srv, "DATA_DIR", data_dir)
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    yield {"data_dir": data_dir}


@pytest.fixture()
def client(isolated):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


# ---------------------------------------------------------------------------
# _state_tunnel_url
# ---------------------------------------------------------------------------

def test_tunnel_state_when_file_missing(isolated):
    out = srv._state_tunnel_url()
    assert out == {"url": None, "updated_at": None, "available": False}


def test_tunnel_state_reads_file(isolated):
    url = "https://gibbous-trailing-yellow-fox.trycloudflare.com"
    (isolated["data_dir"] / "tunnel_url.txt").write_text(url, encoding="utf-8")
    out = srv._state_tunnel_url()
    assert out["url"] == url
    assert out["available"] is True
    assert out["updated_at"]  # mtime ISO present


def test_tunnel_state_strips_whitespace(isolated):
    url = "https://gibbous-trailing-yellow-fox.trycloudflare.com"
    (isolated["data_dir"] / "tunnel_url.txt").write_text(
        "  " + url + "\n", encoding="utf-8",
    )
    out = srv._state_tunnel_url()
    assert out["url"] == url


def test_tunnel_state_empty_file_treated_unavailable(isolated):
    (isolated["data_dir"] / "tunnel_url.txt").write_text("", encoding="utf-8")
    out = srv._state_tunnel_url()
    assert out["available"] is False
    assert out["url"] is None


# ---------------------------------------------------------------------------
# /api/tunnel + /api/state
# ---------------------------------------------------------------------------

def test_tunnel_endpoint_unavailable_when_missing(client):
    body = client.get("/api/tunnel").get_json()
    assert body["available"] is False


def test_tunnel_endpoint_returns_url(client, isolated):
    url = "https://example.trycloudflare.com"
    (isolated["data_dir"] / "tunnel_url.txt").write_text(url, encoding="utf-8")
    body = client.get("/api/tunnel").get_json()
    assert body["url"] == url
    assert body["available"] is True


def test_state_endpoint_includes_tv_tunnel(client, isolated):
    body = client.get("/api/state").get_json()
    assert "tv_tunnel" in body
    assert body["tv_tunnel"]["available"] is False
    url = "https://example.trycloudflare.com"
    (isolated["data_dir"] / "tunnel_url.txt").write_text(url, encoding="utf-8")
    body2 = client.get("/api/state").get_json()
    assert body2["tv_tunnel"]["url"] == url


def test_index_html_includes_tv_tunnel_card(client):
    text = client.get("/").get_data(as_text=True)
    assert 'id="tv-tunnel"' in text
    assert "renderTunnel" in text
    assert "cloudflare tunnel" in text.lower()
