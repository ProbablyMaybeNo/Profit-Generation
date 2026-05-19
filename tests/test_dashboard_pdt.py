"""
test_dashboard_pdt.py — 5.4.2: dashboard Monitor card PDT counter.

Covers:
  - /api/state surfaces a `pdt` key with the rollup shape
  - empty DB → zero counts, threshold filled
  - round-trip math reflected through to the API
  - paper_unlimited flag flips based on account_value
  - card HTML present in dashboard/index.html
"""

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
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    yield test_db


@pytest.fixture()
def client(isolated_db):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def _insert_filled_trade(conn, *, symbol, side, filled_at, order_id):
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, symbol, side, qty, filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (order_id, symbol, side, 1.0, filled_at, "filled", filled_at),
    )
    conn.commit()


def test_state_includes_pdt_key(client):
    rv = client.get("/api/state")
    assert rv.status_code == 200
    s = rv.get_json()
    assert "pdt" in s
    p = s["pdt"]
    assert "today" in p
    assert "five_day" in p
    assert "threshold" in p
    assert "equity_threshold" in p
    assert "paper_unlimited" in p


def test_pdt_zeros_on_empty_db(client):
    rv = client.get("/api/state")
    p = rv.get_json()["pdt"]
    assert p["today"] == 0
    assert p["five_day"] == 0
    assert p["threshold"] == 3
    assert p["equity_threshold"] == 25_000.0


def test_pdt_paper_unlimited_when_account_above_threshold(client, monkeypatch):
    monkeypatch.setattr(
        srv, "_safe_account",
        lambda: {"portfolio_value": 100_000.0},
    )
    rv = client.get("/api/state")
    p = rv.get_json()["pdt"]
    assert p["paper_unlimited"] is True
    assert p["account_value"] == 100_000.0


def test_pdt_not_paper_unlimited_when_below_threshold(client, monkeypatch):
    monkeypatch.setattr(
        srv, "_safe_account",
        lambda: {"portfolio_value": 10_000.0},
    )
    rv = client.get("/api/state")
    p = rv.get_json()["pdt"]
    assert p["paper_unlimited"] is False
    assert p["account_value"] == 10_000.0


def test_pdt_counts_reflect_paper_trades(client, isolated_db):
    """Insert a same-day buy+sell — today=1 and five_day=1."""
    today_iso = date.today().isoformat()
    conn = db.init_db()
    _insert_filled_trade(conn, symbol="SPY", side="buy",
                         filled_at=f"{today_iso}T14:00:00",
                         order_id="b1")
    _insert_filled_trade(conn, symbol="SPY", side="sell",
                         filled_at=f"{today_iso}T15:00:00",
                         order_id="s1")
    rv = client.get("/api/state")
    p = rv.get_json()["pdt"]
    assert p["today"] == 1
    assert p["five_day"] == 1


def test_pdt_card_present_in_index_html():
    """Sanity: the card markup actually exists in the dashboard HTML."""
    idx = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert 'id="pdt-card"' in idx
    assert 'id="pdt-counter"' in idx
    assert "renderPDT" in idx
