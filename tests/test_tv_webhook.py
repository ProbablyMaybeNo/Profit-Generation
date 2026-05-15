import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import tv_webhook  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setenv("TV_WEBHOOK_SECRET", "topsecret")
    app = tv_webhook.create_app()
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture()
def client_no_secret(isolated_db, monkeypatch):
    monkeypatch.delenv("TV_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(tv_webhook, "load_credentials",
                        lambda key: (_ for _ in ()).throw(FileNotFoundError("missing")))
    app = tv_webhook.create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_health(client):
    rv = client.get("/health")
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "ok"


def test_webhook_rejects_wrong_secret(client):
    rv = client.post("/webhook", json={"secret": "nope", "ticker": "GDX",
                                       "action": "buy", "price": 93.95,
                                       "strategy": "test-strat"})
    assert rv.status_code == 401


def test_webhook_accepts_secret_in_body(client, isolated_db):
    rv = client.post("/webhook", json={"secret": "topsecret", "ticker": "AMEX:GDX",
                                       "action": "buy", "price": 93.95,
                                       "strategy": "botnet101-buy-5day-low",
                                       "time": "2026-05-14T19:30:00Z"})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "recorded"
    assert body["symbol"] == "GDX"
    assert body["signal_type"] == "long_entry"
    conn = db.connect(isolated_db)
    rows = conn.execute(
        "SELECT * FROM signals WHERE bar_interval = 'tv-webhook'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["close"] == 93.95


def test_webhook_accepts_secret_in_header(client):
    rv = client.post(
        "/webhook",
        headers={"X-Webhook-Secret": "topsecret"},
        json={"ticker": "GDX", "action": "buy", "price": 93.95,
              "strategy": "test-strat", "time": "2026-05-14T19:30:00Z"},
    )
    assert rv.status_code == 200


def test_webhook_strips_exchange_prefix(client, isolated_db):
    client.post("/webhook", json={"secret": "topsecret", "ticker": "BINANCE:BTCUSDT",
                                  "action": "sell", "price": 65000.0,
                                  "strategy": "tv-test", "time": "2026-05-14T19:30:00Z"})
    conn = db.connect(isolated_db)
    rows = conn.execute("SELECT symbol, signal_type FROM signals "
                        "WHERE bar_interval = 'tv-webhook'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["signal_type"] == "long_exit"


def test_webhook_action_mapping(client, isolated_db):
    payloads = [
        ("buy", "long_entry"), ("long", "long_entry"), ("entry", "long_entry"),
        ("sell", "long_exit"), ("close", "long_exit"), ("exit", "long_exit"),
    ]
    for i, (action, expected) in enumerate(payloads):
        rv = client.post("/webhook", json={
            "secret": "topsecret", "ticker": f"SYM{i}",
            "action": action, "price": 10.0, "strategy": f"strat-{i}",
            "time": f"2026-05-14T19:3{i}:00Z",
        })
        assert rv.status_code == 200, rv.get_json()
        assert rv.get_json()["signal_type"] == expected


def test_webhook_dedupes_via_unique_constraint(client, isolated_db):
    payload = {"secret": "topsecret", "ticker": "GDX", "action": "buy",
               "price": 93.95, "strategy": "test-strat",
               "time": "2026-05-14T19:30:00Z"}
    a = client.post("/webhook", json=payload).get_json()
    b = client.post("/webhook", json=payload).get_json()
    assert a["status"] == "recorded"
    assert b["status"] == "duplicate"
    conn = db.connect(isolated_db)
    n = conn.execute("SELECT COUNT(*) FROM signals "
                     "WHERE bar_interval='tv-webhook'").fetchone()[0]
    conn.close()
    assert n == 1


def test_webhook_auto_creates_strategy(client, isolated_db):
    client.post("/webhook", json={"secret": "topsecret", "ticker": "GDX",
                                  "action": "buy", "price": 93.95,
                                  "strategy": "brand-new-strat-from-tv",
                                  "time": "2026-05-14T19:30:00Z"})
    conn = db.connect(isolated_db)
    row = conn.execute(
        "SELECT methodology_family, current_verdict FROM strategies WHERE strategy_id=?",
        ("brand-new-strat-from-tv",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["methodology_family"] == "tradingview-alert"
    assert row["current_verdict"] == "UNTESTED"


def test_webhook_400_on_missing_ticker(client):
    rv = client.post("/webhook", json={"secret": "topsecret", "action": "buy",
                                       "price": 1.0, "strategy": "x"})
    assert rv.status_code == 400
    assert "ticker" in rv.get_json()["error"]


def test_webhook_400_on_unknown_action(client):
    rv = client.post("/webhook", json={"secret": "topsecret", "ticker": "X",
                                       "action": "nonsense", "price": 1.0,
                                       "strategy": "x"})
    assert rv.status_code == 400
    assert "action" in rv.get_json()["error"]


def test_webhook_400_on_non_numeric_price(client):
    rv = client.post("/webhook", json={"secret": "topsecret", "ticker": "X",
                                       "action": "buy", "price": "not-a-number",
                                       "strategy": "x"})
    assert rv.status_code == 400


def test_webhook_400_on_non_object_body(client):
    rv = client.post("/webhook", json=["not", "an", "object"])
    assert rv.status_code == 400


def test_no_secret_configured_accepts_anonymous(client_no_secret, isolated_db):
    """Documented behavior: missing secret = open receiver. Logs a warning at startup."""
    rv = client_no_secret.post("/webhook", json={"ticker": "X", "action": "buy",
                                                 "price": 1.0, "strategy": "y",
                                                 "time": "2026-05-14T19:30:00Z"})
    assert rv.status_code == 200


def test_recent_endpoint_returns_signals(client, isolated_db):
    for i in range(3):
        client.post("/webhook", json={
            "secret": "topsecret", "ticker": f"SYM{i}", "action": "buy",
            "price": 10.0 + i, "strategy": "x",
            "time": f"2026-05-14T19:3{i}:00Z",
        })
    rv = client.get("/recent")
    assert rv.status_code == 200
    items = rv.get_json()
    assert len(items) == 3
    assert {it["symbol"] for it in items} == {"SYM0", "SYM1", "SYM2"}
