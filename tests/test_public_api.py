"""Tests for dashboard.public_api (milestone 4.4.1).

Sanitization + rate limiting + shape validation. The sanitizer is the
security boundary — sensitive fields ($ amounts, position sizes,
account IDs, raw fills, credentials) must NEVER appear in any
public-API response.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard import public_api as pa  # noqa: E402
from dashboard import server as srv  # noqa: E402
from data import db  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    yield test_db


@pytest.fixture()
def client(isolated_db):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def _seed_equity(conn, points):
    for ts, value in points:
        conn.execute(
            "INSERT INTO equity_snapshots(recorded_at, portfolio_value) "
            "VALUES (?, ?)",
            (ts, value),
        )
    conn.commit()


def _seed_outcomes(conn, strategy_id, returns):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    for i, r in enumerate(returns):
        day = (i % 28) + 1
        month = (i // 28) + 1
        iso = f"2026-{month:02d}-{day:02d}"
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="GDX",
            bar_ts=iso, signal_type="long_entry",
            close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=iso, entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"{iso}T16:00:00Z",
            exit_price=100.0 * (1 + r / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )


# ---------------------------------------------------------------------------
# sanitize_dict
# ---------------------------------------------------------------------------

def test_sanitize_strips_top_level_sensitive_keys():
    out = pa.sanitize_dict({
        "win_rate": 0.55,
        "portfolio_value": 12345.67,
        "cash": 1000.00,
        "api_key": "AKIA...",
    })
    assert out == {"win_rate": 0.55}


def test_sanitize_strips_nested_sensitive_keys():
    nested = {
        "strategies": [
            {"strategy_id": "s", "qty": 10, "win_rate": 0.5},
            {"strategy_id": "t", "fill_price": 99.5, "sharpe": 0.6},
        ],
    }
    out = pa.sanitize_dict(nested)
    assert "qty" not in out["strategies"][0]
    assert "fill_price" not in out["strategies"][1]
    assert out["strategies"][0]["win_rate"] == 0.5
    assert out["strategies"][1]["sharpe"] == 0.6


def test_sanitize_strips_alpaca_account_ids():
    out = pa.sanitize_dict({"account_number": "AC123",
                             "alpaca_order_id": "abc",
                             "win_rate": 0.5})
    assert "account_number" not in out
    assert "alpaca_order_id" not in out
    assert out["win_rate"] == 0.5


def test_sanitize_preserves_non_dict_non_list_values():
    assert pa.sanitize_dict("a string") == "a string"
    assert pa.sanitize_dict(42) == 42
    assert pa.sanitize_dict(None) is None


def test_sanitize_strips_credentials_shaped_fields():
    out = pa.sanitize_dict({
        "secret_key": "x",
        "integration_token": "y",
        "webhook_secret": "z",
        "ok": True,
    })
    assert list(out.keys()) == ["ok"]


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

def test_rate_limiter_allows_under_cap():
    clock = [0.0]
    rl = pa.RateLimiter(per_minute=3, now_fn=lambda: clock[0])
    assert rl.allow("1.2.3.4") is True
    assert rl.allow("1.2.3.4") is True
    assert rl.allow("1.2.3.4") is True


def test_rate_limiter_rejects_over_cap():
    clock = [0.0]
    rl = pa.RateLimiter(per_minute=2, now_fn=lambda: clock[0])
    rl.allow("1.2.3.4")
    rl.allow("1.2.3.4")
    assert rl.allow("1.2.3.4") is False


def test_rate_limiter_sliding_window_evicts_old():
    clock = [0.0]
    rl = pa.RateLimiter(per_minute=2, now_fn=lambda: clock[0])
    rl.allow("ip")
    rl.allow("ip")
    assert rl.allow("ip") is False
    clock[0] = 61.0  # window now expired
    assert rl.allow("ip") is True


def test_rate_limiter_per_ip_independence():
    clock = [0.0]
    rl = pa.RateLimiter(per_minute=1, now_fn=lambda: clock[0])
    assert rl.allow("a") is True
    # b's first request unaffected by a's quota.
    assert rl.allow("b") is True


# ---------------------------------------------------------------------------
# Data shapes — equity_curve
# ---------------------------------------------------------------------------

def test_equity_curve_empty(isolated_db):
    conn = db.init_db()
    try:
        out = pa.system_equity_curve(conn)
        assert out["points"] == []
        assert out["final_pct"] == 0.0
    finally:
        conn.close()


def test_equity_curve_pct_returns_no_dollars(isolated_db):
    conn = db.init_db()
    try:
        now = datetime.now(timezone.utc)
        _seed_equity(conn, [
            ((now - timedelta(days=10)).isoformat(), 10000.0),
            ((now - timedelta(days=5)).isoformat(), 10500.0),
            (now.isoformat(), 11000.0),
        ])
        out = pa.system_equity_curve(conn, days=30)
        assert len(out["points"]) == 3
        # No dollar amounts in output.
        flat = str(out)
        assert "10000" not in flat
        assert "10500" not in flat
        assert "11000" not in flat
        # Final pct should be ~10% (baseline 10k → 11k).
        assert out["final_pct"] == pytest.approx(10.0)
    finally:
        conn.close()


def test_equity_curve_drawdown_math(isolated_db):
    conn = db.init_db()
    try:
        now = datetime.now(timezone.utc)
        _seed_equity(conn, [
            ((now - timedelta(days=10)).isoformat(), 10000.0),
            ((now - timedelta(days=8)).isoformat(), 11000.0),   # peak
            ((now - timedelta(days=5)).isoformat(), 9900.0),    # 10% dd
        ])
        out = pa.system_equity_curve(conn, days=30)
        # Peak was +10%, trough was -1% — drawdown ~ -11%.
        assert out["max_drawdown_pct"] == pytest.approx(-11.0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Data shapes — per_strategy_stats
# ---------------------------------------------------------------------------

def test_per_strategy_stats_excludes_thin_samples(isolated_db):
    conn = db.init_db()
    try:
        _seed_outcomes(conn, "tiny", [1.0])  # n=1, excluded
        _seed_outcomes(conn, "ok", [0.5, 0.2, 0.4, 0.3, 0.6])
        out = pa.per_strategy_stats(conn)
        sids = {r["strategy_id"] for r in out}
        assert "tiny" not in sids
        assert "ok" in sids
    finally:
        conn.close()


def test_per_strategy_stats_has_no_dollar_fields(isolated_db):
    conn = db.init_db()
    try:
        _seed_outcomes(conn, "s", [0.5, 0.2, -0.1, 0.4, 0.3])
        out = pa.per_strategy_stats(conn)
        assert out
        row = out[0]
        # Only sharpe / win_rate / mean_pct / n_trades / strategy_id.
        forbidden = {"qty", "fill_price", "portfolio_value",
                     "buying_power", "alpaca_order_id"}
        assert forbidden.isdisjoint(row.keys())
    finally:
        conn.close()


def test_per_strategy_stats_sorted_by_sharpe_desc(isolated_db):
    conn = db.init_db()
    try:
        _seed_outcomes(conn, "weak", [0.1, 0.05, 0.0, -0.05, 0.1])
        _seed_outcomes(conn, "strong", [0.5, 0.45, 0.6, 0.55, 0.5])
        out = pa.per_strategy_stats(conn)
        assert out[0]["strategy_id"] == "strong"
        assert out[1]["strategy_id"] == "weak"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Data shapes — last_30d_pnl
# ---------------------------------------------------------------------------

def test_last_30d_pnl_empty_when_no_snapshots(isolated_db):
    conn = db.init_db()
    try:
        out = pa.last_n_days_pnl_pct(conn, days=30)
        assert out["pnl_pct"] == 0.0
        assert out["n_snapshots"] == 0
    finally:
        conn.close()


def test_last_30d_pnl_pct_math(isolated_db):
    conn = db.init_db()
    try:
        now = datetime.now(timezone.utc)
        _seed_equity(conn, [
            ((now - timedelta(days=20)).isoformat(), 10000.0),
            ((now - timedelta(days=5)).isoformat(), 10500.0),
        ])
        out = pa.last_n_days_pnl_pct(conn, days=30)
        assert out["pnl_pct"] == pytest.approx(5.0)
        assert out["n_snapshots"] == 2
    finally:
        conn.close()


def test_last_30d_pnl_ignores_dollars_in_output(isolated_db):
    conn = db.init_db()
    try:
        now = datetime.now(timezone.utc)
        _seed_equity(conn, [
            ((now - timedelta(days=10)).isoformat(), 12345.67),
            (now.isoformat(), 13456.78),
        ])
        out = pa.last_n_days_pnl_pct(conn, days=30)
        flat = str(out)
        assert "12345" not in flat
        assert "13456" not in flat
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Flask route integration
# ---------------------------------------------------------------------------

def test_route_equity_curve_returns_json(client, isolated_db):
    conn = db.init_db()
    try:
        now = datetime.now(timezone.utc)
        _seed_equity(conn, [
            ((now - timedelta(days=5)).isoformat(), 10000.0),
            (now.isoformat(), 10500.0),
        ])
    finally:
        conn.close()
    resp = client.get("/api/public/equity_curve")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "points" in body
    assert "last_updated" in body
    assert "portfolio_value" not in str(body)
    assert "cash" not in str(body)


def test_route_strategies_returns_json(client, isolated_db):
    conn = db.init_db()
    try:
        _seed_outcomes(conn, "s", [0.5, 0.2, -0.1, 0.4, 0.3])
    finally:
        conn.close()
    resp = client.get("/api/public/strategies")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "strategies" in body
    assert body["strategies"][0]["strategy_id"] == "s"


def test_route_last_30d_pnl_returns_json(client, isolated_db):
    conn = db.init_db()
    try:
        now = datetime.now(timezone.utc)
        _seed_equity(conn, [
            ((now - timedelta(days=10)).isoformat(), 10000.0),
            (now.isoformat(), 10500.0),
        ])
    finally:
        conn.close()
    resp = client.get("/api/public/last_30d_pnl")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["pnl_pct"] == pytest.approx(5.0)
    assert body["n_snapshots"] == 2


def test_route_no_auth_required(client, isolated_db):
    """No headers, no cookies — endpoint still responds 200."""
    resp = client.get("/api/public/strategies",
                      headers={})
    assert resp.status_code in (200,)


def test_route_rate_limiter_returns_429(client, isolated_db, monkeypatch):
    # Replace the per-IP RateLimiter on the live blueprint so we can
    # exhaust the quota within a test.
    aggressive = pa.RateLimiter(per_minute=2)
    # Re-register on a fresh app so we don't clobber the global one.
    from flask import Flask
    test_app = Flask(__name__)
    pa.register(test_app, db_module=db, rate_limiter=aggressive)
    test_app.config.update(TESTING=True)
    test_client = test_app.test_client()
    r1 = test_client.get("/api/public/strategies")
    r2 = test_client.get("/api/public/strategies")
    r3 = test_client.get("/api/public/strategies")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    body = r3.get_json()
    assert body["error"] == "rate_limited"
    assert body["limit_per_min"] == 2


# ---------------------------------------------------------------------------
# get_client_ip honors X-Forwarded-For
# ---------------------------------------------------------------------------

def test_get_client_ip_uses_xff_first_address():
    from flask import Flask, request
    app = Flask(__name__)
    captured = []

    @app.route("/probe")
    def probe():
        captured.append(pa.get_client_ip(request))
        return "ok"

    client = app.test_client()
    client.get("/probe", headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
    assert captured[0] == "1.1.1.1"
