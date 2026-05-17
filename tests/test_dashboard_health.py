"""Tests for /api/health (milestone 3.5.3)."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard import server as srv  # noqa: E402
from data import db  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    # Make market_is_open + alpaca calls deterministic + offline-safe.
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "get_account_summary",
                          lambda: {"status": "ACTIVE", "portfolio_value": 1000.0})
    # Re-point log paths into tmp_path so we control file ages.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(srv, "LOG_DIR", log_dir)
    monkeypatch.setattr(srv, "INTRADAY_LOG", log_dir / "intraday_alerts.log")
    monkeypatch.setattr(srv, "DATA_DIR", data_dir)
    yield tmp_path


@pytest.fixture()
def client(isolated_db):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def _touch(path: Path, *, ago_minutes: int = 0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("touch", encoding="utf-8")
    t = time.time() - ago_minutes * 60
    os.utime(path, (t, t))


def _seed_recent_daily_report(conn, *, hours_ago=2):
    """Insert a daily_reports row whose generated_at is `hours_ago`
    hours behind now."""
    gen = (datetime.now(timezone.utc).timestamp() - hours_ago * 3600)
    iso = datetime.fromtimestamp(gen, tz=timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO daily_reports(report_date, market_regime, importance, "
        "  has_notable_pattern, fires_count, watchlist_count, "
        "  notable_movers_count, tags_json, symbols_watched_json, "
        "  notion_page_id, markdown, generated_at) "
        "VALUES(?, 'choppy', 1, 0, 0, 0, 0, '[]', '[]', NULL, NULL, ?)",
        ("2026-05-16", iso),
    )
    conn.commit()


# ---------- endpoint shape ----------

def test_health_endpoint_returns_required_keys(client, isolated_db):
    rv = client.get("/api/health")
    assert rv.status_code == 200
    body = rv.get_json()
    required = {"ok", "now", "alpaca", "db", "intraday_age_min",
                "daily_report_age_h", "tunnel_age_h", "kill_switch",
                "open_positions", "degraded"}
    assert required.issubset(body.keys())


def test_health_endpoint_returns_200_even_when_degraded(client, isolated_db):
    """Acceptance: HTTP 200 always — UptimeRobot parses JSON body, not
    HTTP code. (HTTP 5xx would also alert, but we want the operator's
    monitor to see WHICH subsystem is degraded.)"""
    rv = client.get("/api/health")
    assert rv.status_code == 200
    body = rv.get_json()
    # Empty DB + no logs → many things degraded but the call still 200s.
    assert body["ok"] is False
    assert isinstance(body["degraded"], list)
    assert len(body["degraded"]) > 0


def test_health_db_ok_when_schema_present(client, isolated_db):
    body = client.get("/api/health").get_json()
    assert body["db"] == "ok"


def test_health_alpaca_ok_with_active_account(client, isolated_db):
    body = client.get("/api/health").get_json()
    assert body["alpaca"] == "ok"


def test_health_alpaca_blocked_status(client, isolated_db, monkeypatch):
    monkeypatch.setattr(srv, "get_account_summary",
                          lambda: {"status": "ACTIVE", "trading_blocked": True})
    body = client.get("/api/health").get_json()
    assert body["alpaca"] == "blocked"
    assert any("alpaca:blocked" in d for d in body["degraded"])


def test_health_alpaca_unreachable_on_exception(client, isolated_db, monkeypatch):
    def boom():
        raise RuntimeError("alpaca down")
    monkeypatch.setattr(srv, "get_account_summary", boom)
    body = client.get("/api/health").get_json()
    assert body["alpaca"] == "unreachable"


# ---------- stale-data flags ----------

def test_intraday_age_min_present_when_log_fresh(client, isolated_db):
    _touch(srv.INTRADAY_LOG, ago_minutes=5)
    body = client.get("/api/health").get_json()
    assert body["intraday_age_min"] == 5


def test_intraday_age_min_none_when_no_log(client, isolated_db):
    body = client.get("/api/health").get_json()
    assert body["intraday_age_min"] is None
    assert any("intraday:no_log" in d for d in body["degraded"])


def test_intraday_stale_flag_only_when_market_open(client, isolated_db, monkeypatch):
    _touch(srv.INTRADAY_LOG, ago_minutes=90)
    # Market closed → not flagged as stale (after-hours is fine).
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    body = client.get("/api/health").get_json()
    assert not any("intraday:stale" in d for d in body["degraded"])
    # Market open → flagged.
    monkeypatch.setattr(srv, "market_is_open", lambda: True)
    body = client.get("/api/health").get_json()
    assert any("intraday:stale_90min" in d for d in body["degraded"])


def test_tunnel_age_present_when_file_fresh(client, isolated_db):
    _touch(srv.DATA_DIR / "tunnel_url.txt", ago_minutes=10)
    body = client.get("/api/health").get_json()
    assert body["tunnel_age_h"] == round(10 / 60, 2)


def test_tunnel_stale_flag(client, isolated_db):
    _touch(srv.DATA_DIR / "tunnel_url.txt", ago_minutes=60 * 30)  # 30h
    body = client.get("/api/health").get_json()
    assert body["tunnel_age_h"] >= 24
    assert any("tunnel:stale" in d for d in body["degraded"])


def test_daily_report_age_from_db(client, isolated_db):
    conn = db.init_db()
    _seed_recent_daily_report(conn, hours_ago=4)
    conn.close()
    body = client.get("/api/health").get_json()
    assert body["daily_report_age_h"] is not None
    assert 3 < body["daily_report_age_h"] < 5


def test_daily_report_age_none_when_empty(client, isolated_db):
    body = client.get("/api/health").get_json()
    assert body["daily_report_age_h"] is None
    assert any("daily_report:none" in d for d in body["degraded"])


def test_daily_report_stale_after_36_hours(client, isolated_db):
    conn = db.init_db()
    _seed_recent_daily_report(conn, hours_ago=40)
    conn.close()
    body = client.get("/api/health").get_json()
    assert any("daily_report:stale" in d for d in body["degraded"])


# ---------- kill switch ----------

def test_kill_switch_false_by_default(client, isolated_db):
    body = client.get("/api/health").get_json()
    assert body["kill_switch"] is False


def test_kill_switch_engaged_flagged(client, isolated_db, monkeypatch):
    monkeypatch.setattr(
        srv, "_state_kill_switch",
        lambda: {"live_trading_halted": True, "reason": "test"},
    )
    body = client.get("/api/health").get_json()
    assert body["kill_switch"] is True
    assert any("kill_switch:engaged" in d for d in body["degraded"])


# ---------- open positions ----------

def test_open_positions_zero_when_empty(client, isolated_db):
    body = client.get("/api/health").get_json()
    assert body["open_positions"] == 0


def test_open_positions_counts_unmatched_buys(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    for i, sym in enumerate(("A", "B", "C")):
        sid = db.record_signal(conn, strategy_id="s1", symbol=sym,
                                bar_ts=f"2026-05-{i+1:02d}",
                                signal_type="long_entry",
                                close=100.0, bar_interval="1d")
        db.record_paper_trade(conn, {
            "alpaca_order_id": f"buy-{sym}", "signal_id": sid,
            "strategy_id": "s1", "symbol": sym, "side": "buy",
            "qty": 10, "order_type": "market",
            "submitted_at": f"2026-05-{i+1:02d}T13:30Z",
            "status": "filled", "fill_price": 100.0,
        })
    # Close one of the three.
    sid_a = conn.execute(
        "SELECT id FROM signals WHERE symbol='A' LIMIT 1"
    ).fetchone()["id"]
    db.record_paper_trade(conn, {
        "alpaca_order_id": "sell-A", "signal_id": sid_a,
        "strategy_id": "s1", "symbol": "A", "side": "sell",
        "qty": 10, "order_type": "market",
        "submitted_at": "2026-05-05T13:30Z",
        "status": "filled", "fill_price": 101.0,
    })
    conn.close()
    body = client.get("/api/health").get_json()
    assert body["open_positions"] == 2


# ---------- aggregate ok flag ----------

def test_ok_true_when_everything_green(client, isolated_db, monkeypatch):
    conn = db.init_db()
    _seed_recent_daily_report(conn, hours_ago=2)
    conn.close()
    _touch(srv.INTRADAY_LOG, ago_minutes=5)
    _touch(srv.DATA_DIR / "tunnel_url.txt", ago_minutes=60)
    body = client.get("/api/health").get_json()
    assert body["ok"] is True, f"degraded items: {body['degraded']}"
    assert body["degraded"] == []
