import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# preflight lives under scripts/, which isn't a package — import via importlib.
import importlib.util
SPEC = importlib.util.spec_from_file_location(
    "preflight", ROOT / "scripts" / "preflight.py",
)
pre = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pre)


from data import db  # noqa: E402


# ---- check_credentials ----------------------------------------------------

def _all_good_creds():
    return {
        "alpaca": {"api_key": "real_alpaca_key", "secret_key": "real_secret",
                   "paper": True},
        "polygon": {"api_key": "real_polygon"},
        "fred": {"api_key": "real_fred"},
        "notion": {"integration_token": "secret_real"},
        "telegram": {"bot_token": "real_tok", "chat_id": "999"},
        "tradingview": {"webhook_secret": "long_random_secret"},
    }


def test_credentials_pass(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps(_all_good_creds()), encoding="utf-8")
    r = pre.check_credentials(path=p)
    assert r["status"] == "PASS"


def test_credentials_missing_file(tmp_path):
    r = pre.check_credentials(path=tmp_path / "nope.json")
    assert r["status"] == "FAIL"
    assert "missing" in r["detail"]


def test_credentials_missing_section(tmp_path):
    creds = _all_good_creds()
    del creds["polygon"]
    p = tmp_path / "c.json"
    p.write_text(json.dumps(creds), encoding="utf-8")
    r = pre.check_credentials(path=p)
    assert r["status"] == "FAIL"
    assert "polygon" in r["detail"]


def test_credentials_placeholder_value(tmp_path):
    creds = _all_good_creds()
    creds["polygon"]["api_key"] = "PASTE_YOUR_POLYGON_KEY_HERE"
    p = tmp_path / "c.json"
    p.write_text(json.dumps(creds), encoding="utf-8")
    r = pre.check_credentials(path=p)
    assert r["status"] == "FAIL"
    assert "polygon.api_key" in r["detail"]


def test_credentials_empty_value(tmp_path):
    creds = _all_good_creds()
    creds["fred"]["api_key"] = ""
    p = tmp_path / "c.json"
    p.write_text(json.dumps(creds), encoding="utf-8")
    r = pre.check_credentials(path=p)
    assert r["status"] == "FAIL"


def test_credentials_ignores_underscore_keys(tmp_path):
    creds = _all_good_creds()
    creds["_readme"] = "ignore me"
    creds["alpaca"]["_note"] = "should be skipped"
    p = tmp_path / "c.json"
    p.write_text(json.dumps(creds), encoding="utf-8")
    r = pre.check_credentials(path=p)
    assert r["status"] == "PASS"


# ---- check_settings_schema -----------------------------------------------

def _all_good_settings():
    return {
        "timezone": "America/New_York",
        "market_open": "09:30",
        "market_close": "16:00",
        "dashboard_port": 8080,
        "log_level": "INFO",
        "paper_trading": True,
        "risk": {"max_position_usd": 1000},
        "auto_trade": {"enabled": True, "dry_run": False},
    }


def test_settings_schema_pass(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(_all_good_settings()), encoding="utf-8")
    r = pre.check_settings_schema(path=p)
    assert r["status"] == "PASS"


def test_settings_schema_missing_required_key(tmp_path):
    s = _all_good_settings()
    del s["risk"]
    p = tmp_path / "s.json"
    p.write_text(json.dumps(s), encoding="utf-8")
    r = pre.check_settings_schema(path=p)
    assert r["status"] == "FAIL"
    assert "risk" in r["detail"]


def test_settings_schema_wrong_type(tmp_path):
    s = _all_good_settings()
    s["paper_trading"] = "yes"
    p = tmp_path / "s.json"
    p.write_text(json.dumps(s), encoding="utf-8")
    r = pre.check_settings_schema(path=p)
    assert r["status"] == "FAIL"


def test_settings_schema_unparseable(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("not-json{", encoding="utf-8")
    r = pre.check_settings_schema(path=p)
    assert r["status"] == "FAIL"
    assert "unparseable" in r["detail"]


# ---- check_db_schema ------------------------------------------------------

def test_db_schema_pass(tmp_path):
    p = tmp_path / "trading.db"
    db.init_db(p)
    r = pre.check_db_schema(db_path=p)
    assert r["status"] == "PASS"
    assert "schema_version" in r["detail"]


def test_db_schema_mismatch(tmp_path, monkeypatch):
    """When the on-disk schema_version is older than data.db.SCHEMA_VERSION,
    preflight must flag it as FAIL. We simulate that by writing the version
    via raw SQL AFTER init_db has run."""
    p = tmp_path / "trading.db"
    conn = db.init_db(p)
    conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    r = pre.check_db_schema(db_path=p)
    assert r["status"] == "FAIL"
    assert "0" in r["detail"]


# ---- check_notion_recency -------------------------------------------------

def _seed_daily_reports(conn, n_with_notion: int, n_without: int = 0):
    from datetime import date as _date
    base = _date(2026, 5, 1)
    for i in range(n_with_notion):
        conn.execute(
            "INSERT INTO daily_reports(report_date, market_regime, importance, "
            "has_notable_pattern, fires_count, watchlist_count, "
            "notable_movers_count, notion_page_id, generated_at) "
            "VALUES(?, 'chop', 2, 0, 0, 0, 0, ?, '2026-05-01T00:00:00Z')",
            ((base + timedelta(days=i)).isoformat(), f"page-{i}"),
        )
    for i in range(n_without):
        conn.execute(
            "INSERT INTO daily_reports(report_date, market_regime, importance, "
            "has_notable_pattern, fires_count, watchlist_count, "
            "notable_movers_count, notion_page_id, generated_at) "
            "VALUES(?, 'chop', 2, 0, 0, 0, 0, NULL, '2026-05-01T00:00:00Z')",
            ((base + timedelta(days=100 + i)).isoformat(),),
        )
    conn.commit()


def test_notion_recency_pass(tmp_path):
    p = tmp_path / "trading.db"
    conn = db.init_db(p)
    _seed_daily_reports(conn, n_with_notion=3)
    r = pre.check_notion_recency(db_path=p, count=3)
    assert r["status"] == "PASS"


def test_notion_recency_too_few_rows(tmp_path):
    p = tmp_path / "trading.db"
    conn = db.init_db(p)
    _seed_daily_reports(conn, n_with_notion=1)
    r = pre.check_notion_recency(db_path=p, count=3)
    assert r["status"] == "FAIL"
    assert "only 1" in r["detail"]


def test_notion_recency_missing_page_id(tmp_path):
    p = tmp_path / "trading.db"
    conn = db.init_db(p)
    _seed_daily_reports(conn, n_with_notion=0, n_without=3)
    r = pre.check_notion_recency(db_path=p, count=3)
    assert r["status"] == "FAIL"
    assert "no notion_page_id" in r["detail"]


# ---- check_intraday_scan --------------------------------------------------

def test_intraday_scan_skipped_when_market_closed(tmp_path):
    r = pre.check_intraday_scan(
        log_dir=tmp_path, market_is_open_fn=lambda: False,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    assert r["status"] == "SKIP"


def test_intraday_scan_pass_recent(tmp_path):
    log = tmp_path / "intraday_alerts.log"
    log.write_text("tick", encoding="utf-8")
    # Set mtime to ~10 minutes ago.
    ten_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()
    os.utime(log, (ten_min_ago, ten_min_ago))
    r = pre.check_intraday_scan(
        log_dir=tmp_path, market_is_open_fn=lambda: True,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    assert r["status"] == "PASS"


def test_intraday_scan_fail_stale(tmp_path):
    log = tmp_path / "intraday_alerts.log"
    log.write_text("tick", encoding="utf-8")
    too_old = (datetime.now(timezone.utc) - timedelta(minutes=60)).timestamp()
    os.utime(log, (too_old, too_old))
    r = pre.check_intraday_scan(
        log_dir=tmp_path, market_is_open_fn=lambda: True,
        now_fn=lambda: datetime.now(timezone.utc),
        max_stale_min=30,
    )
    assert r["status"] == "FAIL"
    assert "60m ago" in r["detail"]


def test_intraday_scan_no_log_file_fails(tmp_path):
    r = pre.check_intraday_scan(
        log_dir=tmp_path, market_is_open_fn=lambda: True,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    assert r["status"] == "FAIL"


def test_intraday_scan_falls_back_to_heartbeat(tmp_path):
    hb = tmp_path / "heartbeat.log"
    hb.write_text("ok", encoding="utf-8")
    r = pre.check_intraday_scan(
        log_dir=tmp_path, market_is_open_fn=lambda: True,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    assert r["status"] == "PASS"


# ---- check_tunnel_url -----------------------------------------------------

def test_tunnel_url_pass(tmp_path):
    f = tmp_path / "tunnel_url.txt"
    f.write_text("https://abc-def.trycloudflare.com", encoding="utf-8")
    r = pre.check_tunnel_url(
        data_dir=tmp_path,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    assert r["status"] == "PASS"


def test_tunnel_url_missing(tmp_path):
    r = pre.check_tunnel_url(
        data_dir=tmp_path,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    assert r["status"] == "FAIL"
    assert "missing" in r["detail"]


def test_tunnel_url_empty_file(tmp_path):
    f = tmp_path / "tunnel_url.txt"
    f.write_text("   ", encoding="utf-8")
    r = pre.check_tunnel_url(
        data_dir=tmp_path,
        now_fn=lambda: datetime.now(timezone.utc),
    )
    assert r["status"] == "FAIL"


def test_tunnel_url_stale(tmp_path):
    f = tmp_path / "tunnel_url.txt"
    f.write_text("https://x.trycloudflare.com", encoding="utf-8")
    way_old = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    os.utime(f, (way_old, way_old))
    r = pre.check_tunnel_url(
        data_dir=tmp_path,
        now_fn=lambda: datetime.now(timezone.utc),
        max_stale_hours=24,
    )
    assert r["status"] == "FAIL"
    assert "48" in r["detail"] or "48.0" in r["detail"]


# ---- check_alpaca ---------------------------------------------------------

def test_alpaca_pass(monkeypatch):
    monkeypatch.setattr("config.utils.is_paper_mode", lambda: True)
    r = pre.check_alpaca(
        account_summary_fn=lambda: {"status": "ACTIVE", "blocked": False},
        is_paper_mode_fn=lambda: True,
    )
    assert r["status"] == "PASS"


def test_alpaca_not_paper(monkeypatch):
    r = pre.check_alpaca(
        is_paper_mode_fn=lambda: False,
        account_summary_fn=lambda: {"status": "ACTIVE", "blocked": False},
    )
    assert r["status"] == "FAIL"
    assert "paper" in r["detail"]


def test_alpaca_account_blocked():
    r = pre.check_alpaca(
        is_paper_mode_fn=lambda: True,
        account_summary_fn=lambda: {"status": "ACTIVE", "blocked": True},
    )
    assert r["status"] == "FAIL"
    assert "blocked" in r["detail"]


def test_alpaca_inactive():
    r = pre.check_alpaca(
        is_paper_mode_fn=lambda: True,
        account_summary_fn=lambda: {"status": "INACTIVE", "blocked": False},
    )
    assert r["status"] == "FAIL"
    assert "INACTIVE" in r["detail"]


def test_alpaca_lookup_exception():
    def boom():
        raise RuntimeError("connection refused")
    r = pre.check_alpaca(
        is_paper_mode_fn=lambda: True,
        account_summary_fn=boom,
    )
    assert r["status"] == "FAIL"


# ---- run_all + exit code semantics ----------------------------------------

def test_format_report_includes_summary():
    out = pre.format_report([
        {"check": "a", "status": "PASS", "detail": "ok"},
        {"check": "b", "status": "FAIL", "detail": "broken"},
        {"check": "c", "status": "SKIP", "detail": "n/a"},
    ])
    assert "PASS" in out
    assert "FAIL" in out
    assert "SKIP" in out
    assert "1 pass · 1 fail · 1 skip" in out


def test_run_all_captures_exceptions(monkeypatch):
    def boom():
        raise RuntimeError("whoops")
    monkeypatch.setattr(pre, "ALL_CHECKS", [boom])
    results = pre.run_all()
    assert len(results) == 1
    assert results[0]["status"] == "FAIL"
    assert "whoops" in results[0]["detail"]
