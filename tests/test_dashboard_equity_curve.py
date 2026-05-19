import sys
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


def _seed_closed_trades(strategy_id, trades):
    """trades: list of (entry_ts, exit_ts, entry_price, exit_price)"""
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    for entry_ts, exit_ts, entry_price, exit_price in trades:
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="GDX",
            bar_ts=entry_ts, signal_type="long_entry",
            close=entry_price, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=entry_ts,
                        entry_price=entry_price)
        db.close_outcome(conn, signal_id=sid, exit_ts=exit_ts,
                         exit_price=exit_price, exit_reason="long_exit_signal",
                         bars_held=1)
    conn.close()


def _compute_expected(trade_pcts, sizing=srv.EQUITY_CURVE_PER_TRADE_SIZE_PCT / 100.0):
    """Mirror the server's compound math so tests stay in sync with the
    EQUITY_CURVE_PER_TRADE_SIZE_PCT constant rather than hard-coded values."""
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    eq_pcts = []
    dds = []
    for r in trade_pcts:
        equity *= (1.0 + sizing * r / 100.0)
        peak = max(peak, equity)
        dd = (equity - peak) / peak * 100.0 if peak > 0 else 0.0
        max_dd = min(max_dd, dd)
        eq_pcts.append((equity - 1.0) * 100.0)
        dds.append(dd)
    total_return = (equity - 1.0) * 100.0
    return eq_pcts, dds, total_return, max_dd


# ---------------------------------------------------------------------------
# Empty / unknown
# ---------------------------------------------------------------------------

def test_equity_curve_empty_for_unknown_strategy(client):
    rv = client.get("/api/equity_curve/nonexistent")
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["strategy_id"] == "nonexistent"
    assert body["points"] == []
    assert body["n_trades"] == 0
    assert body["total_return_pct"] == 0.0
    assert body["max_drawdown_pct"] == 0.0
    assert body["cagr_pct"] is None


def test_equity_curve_empty_for_strategy_with_only_open_outcomes(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "open-only"}})
    sid = db.record_signal(
        conn, strategy_id="open-only", symbol="GDX",
        bar_ts="2026-04-01", signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )
    db.open_outcome(conn, signal_id=sid, entry_ts="2026-04-01",
                    entry_price=100.0)
    conn.close()
    rv = client.get("/api/equity_curve/open-only")
    body = rv.get_json()
    assert body["points"] == []
    assert body["n_trades"] == 0


# ---------------------------------------------------------------------------
# Single & multi-trade math
# ---------------------------------------------------------------------------

def test_equity_curve_single_trade(client, isolated_db):
    _seed_closed_trades("solo", [
        ("2026-04-01", "2026-04-05", 100.0, 110.0),  # +10%
    ])
    body = client.get("/api/equity_curve/solo").get_json()
    expected_eq, _, expected_total, _ = _compute_expected([10.0])
    assert body["n_trades"] == 1
    assert len(body["points"]) == 1
    assert body["points"][0]["date"] == "2026-04-05"
    assert body["points"][0]["equity_pct"] == pytest.approx(expected_eq[0], abs=1e-3)
    assert body["points"][0]["trade_pct"] == pytest.approx(10.0)
    assert body["points"][0]["drawdown_pct"] == pytest.approx(0.0)
    assert body["total_return_pct"] == pytest.approx(expected_total, abs=1e-3)
    assert body["max_drawdown_pct"] == pytest.approx(0.0)


def test_equity_curve_multi_trade_cumulative_math(client, isolated_db):
    _seed_closed_trades("multi", [
        ("2026-01-01", "2026-01-05", 100.0, 110.0),  # +10
        ("2026-02-01", "2026-02-05", 100.0, 105.0),  # +5
        ("2026-03-01", "2026-03-05", 100.0, 90.0),   # -10
        ("2026-04-01", "2026-04-05", 100.0, 102.0),  # +2
    ])
    body = client.get("/api/equity_curve/multi").get_json()
    expected_eq, _, expected_total, _ = _compute_expected([10.0, 5.0, -10.0, 2.0])
    assert body["n_trades"] == 4
    eqs = [p["equity_pct"] for p in body["points"]]
    for actual, expected in zip(eqs, expected_eq):
        assert actual == pytest.approx(expected, abs=1e-3)
    assert body["total_return_pct"] == pytest.approx(expected_total, abs=1e-3)


def test_equity_curve_max_drawdown_correct(client, isolated_db):
    _seed_closed_trades("dd-strat", [
        ("2026-01-01", "2026-01-05", 100.0, 120.0),  # +20  → peak
        ("2026-02-01", "2026-02-05", 100.0, 95.0),   # -5
        ("2026-03-01", "2026-03-05", 100.0, 92.0),   # -8   → deepest
        ("2026-04-01", "2026-04-05", 100.0, 105.0),  # +5
    ])
    body = client.get("/api/equity_curve/dd-strat").get_json()
    _, expected_dds, _, expected_max_dd = _compute_expected([20.0, -5.0, -8.0, 5.0])
    assert body["max_drawdown_pct"] == pytest.approx(expected_max_dd, abs=1e-3)
    actual_dds = [p["drawdown_pct"] for p in body["points"]]
    for actual, expected in zip(actual_dds, expected_dds):
        assert actual == pytest.approx(expected, abs=1e-3)
    assert body["points"][0]["drawdown_pct"] == pytest.approx(0.0)


def test_equity_curve_drawdown_bounded_above_minus_100(client, isolated_db):
    # A catastrophic -100% trade would mathematically zero equity. Bounded
    # drawdown should never exceed -100% in the curve.
    _seed_closed_trades("blowup", [
        ("2026-01-01", "2026-01-05", 100.0, 1.0),  # -99%
    ])
    body = client.get("/api/equity_curve/blowup").get_json()
    assert body["max_drawdown_pct"] >= -100.0


# ---------------------------------------------------------------------------
# Ordering & filtering
# ---------------------------------------------------------------------------

def test_equity_curve_orders_by_exit_ts_ascending(client, isolated_db):
    # Insert out of chronological order; result must be sorted by exit_ts.
    _seed_closed_trades("ordered", [
        ("2026-03-01", "2026-03-05", 100.0, 105.0),  # +5
        ("2026-01-01", "2026-01-05", 100.0, 110.0),  # +10
        ("2026-02-01", "2026-02-05", 100.0, 95.0),   # -5
    ])
    body = client.get("/api/equity_curve/ordered").get_json()
    dates = [p["date"] for p in body["points"]]
    assert dates == sorted(dates)
    # Chronological order: +10, -5, +5
    expected_eq, _, _, _ = _compute_expected([10.0, -5.0, 5.0])
    eqs = [p["equity_pct"] for p in body["points"]]
    for actual, expected in zip(eqs, expected_eq):
        assert actual == pytest.approx(expected, abs=1e-3)


def test_equity_curve_excludes_non_1d_intervals(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "filter-test"}})
    sid1 = db.record_signal(
        conn, strategy_id="filter-test", symbol="GDX",
        bar_ts="2026-04-01", signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )
    db.open_outcome(conn, signal_id=sid1, entry_ts="2026-04-01",
                    entry_price=100.0)
    db.close_outcome(conn, signal_id=sid1, exit_ts="2026-04-05",
                     exit_price=110.0, exit_reason="long_exit_signal",
                     bars_held=1)
    sid2 = db.record_signal(
        conn, strategy_id="filter-test", symbol="GDX",
        bar_ts="2026-04-10", signal_type="long_entry",
        close=100.0, bar_interval="1d-intraday",
    )
    db.open_outcome(conn, signal_id=sid2, entry_ts="2026-04-10",
                    entry_price=100.0)
    db.close_outcome(conn, signal_id=sid2, exit_ts="2026-04-12",
                     exit_price=95.0, exit_reason="long_exit_signal",
                     bars_held=1)
    conn.close()
    body = client.get("/api/equity_curve/filter-test").get_json()
    expected_eq, _, expected_total, _ = _compute_expected([10.0])
    assert body["n_trades"] == 1
    assert body["total_return_pct"] == pytest.approx(expected_total, abs=1e-3)


def test_equity_curve_isolates_strategies(client, isolated_db):
    _seed_closed_trades("alpha", [
        ("2026-04-01", "2026-04-05", 100.0, 110.0),
    ])
    _seed_closed_trades("beta", [
        ("2026-04-02", "2026-04-06", 100.0, 90.0),
    ])
    alpha = client.get("/api/equity_curve/alpha").get_json()
    beta = client.get("/api/equity_curve/beta").get_json()
    expected_pos, _, _, _ = _compute_expected([10.0])
    expected_neg, _, _, _ = _compute_expected([-10.0])
    assert alpha["n_trades"] == 1
    assert beta["n_trades"] == 1
    assert alpha["total_return_pct"] == pytest.approx(expected_pos[0], abs=1e-3)
    assert beta["total_return_pct"] == pytest.approx(expected_neg[0], abs=1e-3)


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------

def test_equity_curve_point_shape(client, isolated_db):
    _seed_closed_trades("shape-test", [
        ("2026-04-01", "2026-04-05", 100.0, 110.0),
    ])
    body = client.get("/api/equity_curve/shape-test").get_json()
    pt = body["points"][0]
    assert set(pt.keys()) == {"date", "equity_pct", "drawdown_pct", "trade_pct"}
    # Top-level shape keys
    assert "total_return_pct" in body
    assert "cagr_pct" in body
    assert "per_trade_size_pct" in body
    assert "period_days" in body
    assert "sum_of_trades_pct" in body


def test_equity_curve_research_html_includes_card_skeleton(client):
    rv = client.get("/research")
    assert rv.status_code == 200
    text = rv.get_data(as_text=True)
    assert "equity curves" in text.lower()
    assert 'id="equity-curves"' in text
    assert 'id="curve-modal"' in text
