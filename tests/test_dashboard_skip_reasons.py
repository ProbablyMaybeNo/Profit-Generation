"""7.5.2 — Dashboard /api/skip_reasons route response shape + loopback guard."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _seed_skip(db_path, *, gate, recorded_at, strategy_id="s", symbol="X",
                signal_type="long_entry", bar_ts="2026-05-22",
                source="daily", reason="r"):
    conn = db.init_db(db_path)
    try:
        db.record_intraday_skip(
            conn, strategy_id=strategy_id, symbol=symbol,
            bar_ts=bar_ts, signal_type=signal_type,
            gate=gate, reason_detail=reason, source=source,
            recorded_at=recorded_at,
        )
    finally:
        conn.close()


def test_skip_reasons_returns_empty_top5_when_no_rows(client):
    r = client.get("/api/skip_reasons")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["top_5"] == []
    assert body["recent"] == []


def test_skip_reasons_top5_orders_by_count_desc(client, tmp_path):
    db_path = tmp_path / "trading.db"
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    # Seed 3 cool_down, 2 kill_switch, 1 earnings_veto
    for _ in range(3):
        _seed_skip(db_path, gate="cool_down", recorded_at=ts)
    for _ in range(2):
        _seed_skip(db_path, gate="kill_switch", recorded_at=ts)
    _seed_skip(db_path, gate="earnings_veto", recorded_at=ts)

    r = client.get("/api/skip_reasons?hours=24")
    body = r.get_json()
    top = body["top_5"]
    assert top[0] == {"gate": "cool_down", "count": 3}
    assert top[1] == {"gate": "kill_switch", "count": 2}
    assert top[2] == {"gate": "earnings_veto", "count": 1}


def test_skip_reasons_recent_rows_newest_first(client, tmp_path):
    db_path = tmp_path / "trading.db"
    now = datetime.now(timezone.utc)
    _seed_skip(
        db_path, gate="cool_down",
        recorded_at=(now - timedelta(minutes=10)).isoformat(timespec="seconds"),
        strategy_id="old", symbol="A",
    )
    _seed_skip(
        db_path, gate="kill_switch",
        recorded_at=(now - timedelta(minutes=1)).isoformat(timespec="seconds"),
        strategy_id="new", symbol="B",
    )
    r = client.get("/api/skip_reasons?limit=10")
    body = r.get_json()
    recent = body["recent"]
    assert len(recent) == 2
    # Newest first (id DESC).
    assert recent[0]["gate"] == "kill_switch"
    assert recent[0]["strategy_id"] == "new"
    assert recent[1]["strategy_id"] == "old"


def test_skip_reasons_hours_window_filters_old_rows(client, tmp_path):
    db_path = tmp_path / "trading.db"
    now = datetime.now(timezone.utc)
    # 200h ago — outside default 24h window
    _seed_skip(
        db_path, gate="cool_down",
        recorded_at=(now - timedelta(hours=200)).isoformat(timespec="seconds"),
    )
    # 1h ago — inside
    _seed_skip(
        db_path, gate="kill_switch",
        recorded_at=(now - timedelta(hours=1)).isoformat(timespec="seconds"),
    )
    r = client.get("/api/skip_reasons?hours=24")
    body = r.get_json()
    gates = {row["gate"] for row in body["recent"]}
    assert gates == {"kill_switch"}


def test_skip_reasons_loopback_only(client):
    # Simulate a non-loopback remote address.
    headers = {}
    # Flask test client by default reports remote_addr as 127.0.0.1, which
    # IS loopback. Patch the loopback check to force a 403 path.
    from unittest.mock import patch
    with patch("dashboard.server._is_loopback_request", return_value=False):
        r = client.get("/api/skip_reasons")
    assert r.status_code == 403
    assert "loopback" in r.get_json()["error"].lower()
