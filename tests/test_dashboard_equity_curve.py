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
    assert body["final_pct"] == 0.0
    assert body["max_drawdown_pct"] == 0.0


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
    assert body["n_trades"] == 1
    assert len(body["points"]) == 1
    assert body["points"][0]["date"] == "2026-04-05"
    assert body["points"][0]["cum_pct"] == pytest.approx(10.0)
    assert body["points"][0]["trade_pct"] == pytest.approx(10.0)
    assert body["points"][0]["drawdown_pct"] == pytest.approx(0.0)
    assert body["final_pct"] == pytest.approx(10.0)
    assert body["max_drawdown_pct"] == pytest.approx(0.0)


def test_equity_curve_multi_trade_cumulative_math(client, isolated_db):
    _seed_closed_trades("multi", [
        ("2026-01-01", "2026-01-05", 100.0, 110.0),  # +10.0
        ("2026-02-01", "2026-02-05", 100.0, 105.0),  # +5.0  → cum 15.0
        ("2026-03-01", "2026-03-05", 100.0, 90.0),   # -10.0 → cum 5.0
        ("2026-04-01", "2026-04-05", 100.0, 102.0),  # +2.0  → cum 7.0
    ])
    body = client.get("/api/equity_curve/multi").get_json()
    assert body["n_trades"] == 4
    cums = [p["cum_pct"] for p in body["points"]]
    assert cums[0] == pytest.approx(10.0)
    assert cums[1] == pytest.approx(15.0)
    assert cums[2] == pytest.approx(5.0)
    assert cums[3] == pytest.approx(7.0)
    assert body["final_pct"] == pytest.approx(7.0)


def test_equity_curve_max_drawdown_correct(client, isolated_db):
    _seed_closed_trades("dd-strat", [
        ("2026-01-01", "2026-01-05", 100.0, 120.0),  # +20  → peak 20
        ("2026-02-01", "2026-02-05", 100.0, 95.0),   # -5   → cum 15, dd -5
        ("2026-03-01", "2026-03-05", 100.0, 92.0),   # -8   → cum 7,  dd -13 (largest)
        ("2026-04-01", "2026-04-05", 100.0, 105.0),  # +5   → cum 12, dd -8
    ])
    body = client.get("/api/equity_curve/dd-strat").get_json()
    # max_drawdown is the deepest negative excursion below peak
    assert body["max_drawdown_pct"] == pytest.approx(-13.0)
    # drawdown at trade 3 (index 2) should be -13
    assert body["points"][2]["drawdown_pct"] == pytest.approx(-13.0)
    # drawdown at peak is 0
    assert body["points"][0]["drawdown_pct"] == pytest.approx(0.0)


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
    # cumulative should follow chronological order: 10 → 5 → 10
    cums = [p["cum_pct"] for p in body["points"]]
    assert cums[0] == pytest.approx(10.0)
    assert cums[1] == pytest.approx(5.0)
    assert cums[2] == pytest.approx(10.0)


def test_equity_curve_excludes_non_1d_intervals(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "filter-test"}})
    # 1d trade — should be included
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
    # intraday trade — should be excluded
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
    assert body["n_trades"] == 1
    assert body["final_pct"] == pytest.approx(10.0)


def test_equity_curve_isolates_strategies(client, isolated_db):
    _seed_closed_trades("alpha", [
        ("2026-04-01", "2026-04-05", 100.0, 110.0),
    ])
    _seed_closed_trades("beta", [
        ("2026-04-02", "2026-04-06", 100.0, 90.0),
    ])
    alpha = client.get("/api/equity_curve/alpha").get_json()
    beta = client.get("/api/equity_curve/beta").get_json()
    assert alpha["n_trades"] == 1
    assert beta["n_trades"] == 1
    assert alpha["final_pct"] == pytest.approx(10.0)
    assert beta["final_pct"] == pytest.approx(-10.0)


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------

def test_equity_curve_point_shape(client, isolated_db):
    _seed_closed_trades("shape-test", [
        ("2026-04-01", "2026-04-05", 100.0, 110.0),
    ])
    body = client.get("/api/equity_curve/shape-test").get_json()
    pt = body["points"][0]
    assert set(pt.keys()) == {"date", "cum_pct", "drawdown_pct", "trade_pct"}


def test_equity_curve_index_html_includes_card_skeleton(client):
    rv = client.get("/")
    assert rv.status_code == 200
    text = rv.get_data(as_text=True)
    # Card title + container div must be present so the JS hook works.
    assert "equity curves" in text.lower()
    assert 'id="equity-curves"' in text
    assert 'id="curve-modal"' in text
