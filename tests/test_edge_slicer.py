import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402
from monitoring import edge_slicer as es  # noqa: E402


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


def _seed_trade(conn, *, strategy_id, entry_ts, exit_ts,
                entry_price=100.0, exit_price=110.0,
                bar_interval="1d"):
    sid = db.record_signal(
        conn, strategy_id=strategy_id, symbol="GDX",
        bar_ts=entry_ts, signal_type="long_entry",
        close=entry_price, bar_interval=bar_interval,
    )
    db.open_outcome(conn, signal_id=sid, entry_ts=entry_ts,
                    entry_price=entry_price)
    db.close_outcome(conn, signal_id=sid, exit_ts=exit_ts,
                     exit_price=exit_price, exit_reason="long_exit_signal",
                     bars_held=1)


# ---------------------------------------------------------------------------
# _parse_iso_date
# ---------------------------------------------------------------------------

def test_parse_iso_date_basic():
    assert es._parse_iso_date("2026-05-15") == date(2026, 5, 15)
    assert es._parse_iso_date("2026-05-15T12:34:56") == date(2026, 5, 15)
    assert es._parse_iso_date(None) is None
    assert es._parse_iso_date("") is None
    assert es._parse_iso_date("not a date") is None


# ---------------------------------------------------------------------------
# _safe_stats
# ---------------------------------------------------------------------------

def test_safe_stats_empty():
    s = es._safe_stats([])
    assert s["n"] == 0
    assert s["mean"] == 0.0
    assert s["win_rate"] == 0.0


def test_safe_stats_basic():
    s = es._safe_stats([2.0, -1.0, 3.0, 4.0])
    assert s["n"] == 4
    assert s["mean"] == pytest.approx(2.0)
    assert s["win_rate"] == pytest.approx(0.75)
    assert s["min"] == pytest.approx(-1.0)
    assert s["max"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# quartile_thresholds + vix_quartile_label
# ---------------------------------------------------------------------------

def test_quartile_thresholds_empty():
    assert es.quartile_thresholds([]) == (0.0, 0.0, 0.0)


def test_quartile_thresholds_uniform():
    # 1..100 → q1=25.75, q2=50.5, q3=75.25 (linear interp).
    vals = list(range(1, 101))
    q1, q2, q3 = es.quartile_thresholds(vals)
    assert q1 == pytest.approx(25.75)
    assert q2 == pytest.approx(50.5)
    assert q3 == pytest.approx(75.25)


def test_vix_quartile_label():
    thr = (15.0, 20.0, 28.0)
    assert es.vix_quartile_label(10.0, thr) == "Q1 (low vol)"
    assert es.vix_quartile_label(15.0, thr) == "Q1 (low vol)"
    assert es.vix_quartile_label(17.0, thr) == "Q2"
    assert es.vix_quartile_label(24.0, thr) == "Q3"
    assert es.vix_quartile_label(40.0, thr) == "Q4 (high vol)"


# ---------------------------------------------------------------------------
# slice_by_dow
# ---------------------------------------------------------------------------

def test_slice_by_dow_buckets_correctly():
    # 2026-05-11 is a Monday, 2026-05-12 Tuesday, etc.
    trades = [
        {"strategy_id": "a", "entry_ts": "2026-05-11", "return_pct": 5.0},  # Mon
        {"strategy_id": "a", "entry_ts": "2026-05-18", "return_pct": 3.0},  # Mon
        {"strategy_id": "a", "entry_ts": "2026-05-12", "return_pct": -2.0},  # Tue
        {"strategy_id": "b", "entry_ts": "2026-05-13", "return_pct": 7.0},  # Wed
    ]
    out = es.slice_by_dow(trades)
    by_key = {(r["strategy_id"], r["slice"]): r for r in out}
    assert by_key[("a", "Mon")]["n"] == 2
    assert by_key[("a", "Mon")]["mean"] == pytest.approx(4.0)
    assert by_key[("a", "Tue")]["n"] == 1
    assert by_key[("b", "Wed")]["n"] == 1


def test_slice_by_dow_skips_unparseable_dates():
    trades = [
        {"strategy_id": "a", "entry_ts": None, "return_pct": 1.0},
        {"strategy_id": "a", "entry_ts": "garbage", "return_pct": 2.0},
        {"strategy_id": "a", "entry_ts": "2026-05-11", "return_pct": 3.0},
    ]
    out = es.slice_by_dow(trades)
    assert len(out) == 1
    assert out[0]["slice"] == "Mon"


def test_slice_by_dow_sorts_by_weekday_order():
    trades = [
        {"strategy_id": "a", "entry_ts": "2026-05-15", "return_pct": 1.0},  # Fri
        {"strategy_id": "a", "entry_ts": "2026-05-11", "return_pct": 1.0},  # Mon
        {"strategy_id": "a", "entry_ts": "2026-05-13", "return_pct": 1.0},  # Wed
    ]
    out = es.slice_by_dow(trades)
    slices = [r["slice"] for r in out]
    assert slices == ["Mon", "Wed", "Fri"]


# ---------------------------------------------------------------------------
# slice_by_regime
# ---------------------------------------------------------------------------

def test_slice_by_regime_uses_regime_map():
    trades = [
        {"strategy_id": "a", "entry_ts": "2026-05-11", "return_pct": 5.0},
        {"strategy_id": "a", "entry_ts": "2026-05-12", "return_pct": -2.0},
        {"strategy_id": "a", "entry_ts": "2026-05-13", "return_pct": 1.0},
    ]
    regime_by_date = {
        "2026-05-11": "trending_up",
        "2026-05-12": "trending_up",
        "2026-05-13": "trending_down",
    }
    out = es.slice_by_regime(trades, regime_by_date)
    by_slice = {r["slice"]: r for r in out}
    assert by_slice["trending_up"]["n"] == 2
    assert by_slice["trending_up"]["mean"] == pytest.approx(1.5)
    assert by_slice["trending_down"]["n"] == 1


def test_slice_by_regime_labels_unknown_dates():
    trades = [
        {"strategy_id": "a", "entry_ts": "2026-05-11", "return_pct": 5.0},
    ]
    out = es.slice_by_regime(trades, {})
    assert out[0]["slice"] == "(unknown)"


# ---------------------------------------------------------------------------
# slice_by_vix
# ---------------------------------------------------------------------------

def test_slice_by_vix_returns_empty_without_data():
    trades = [
        {"strategy_id": "a", "entry_ts": "2026-05-11", "return_pct": 5.0},
    ]
    assert es.slice_by_vix(trades, {}) == []


def test_slice_by_vix_buckets_into_quartiles():
    vix_by_date = {
        "2026-04-01": 12.0,  # low
        "2026-04-02": 14.0,
        "2026-04-03": 18.0,
        "2026-04-04": 22.0,
        "2026-04-05": 28.0,
        "2026-04-06": 35.0,  # high
    }
    trades = [
        {"strategy_id": "a", "entry_ts": "2026-04-01", "return_pct": 1.0},  # Q1
        {"strategy_id": "a", "entry_ts": "2026-04-06", "return_pct": -3.0},  # Q4
    ]
    out = es.slice_by_vix(trades, vix_by_date)
    by_slice = {r["slice"]: r for r in out}
    assert "Q1 (low vol)" in by_slice
    assert "Q4 (high vol)" in by_slice
    assert by_slice["Q1 (low vol)"]["n"] == 1
    assert by_slice["Q4 (high vol)"]["n"] == 1


# ---------------------------------------------------------------------------
# fetch_* helpers
# ---------------------------------------------------------------------------

def test_fetch_closed_outcomes_returns_only_closed_1d(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "test"}})
    _seed_trade(conn, strategy_id="test",
                entry_ts="2026-05-11", exit_ts="2026-05-12")
    # An intraday outcome — should be filtered out.
    _seed_trade(conn, strategy_id="test",
                entry_ts="2026-05-13", exit_ts="2026-05-14",
                bar_interval="1d-intraday")
    rows = es.fetch_closed_outcomes(conn)
    assert len(rows) == 1
    assert rows[0]["strategy_id"] == "test"
    conn.close()


def test_fetch_regime_by_date_basic(isolated_db):
    conn = db.init_db()
    db.record_daily_report(
        conn, report_date="2026-05-11", market_regime="trending_up",
        importance=2, fires_count=0, watchlist_count=0,
        notable_movers_count=0, tags=[], symbols_watched=[],
    )
    db.record_daily_report(
        conn, report_date="2026-05-12", market_regime="trending_down",
        importance=2, fires_count=0, watchlist_count=0,
        notable_movers_count=0, tags=[], symbols_watched=[],
    )
    out = es.fetch_regime_by_date(conn)
    assert out["2026-05-11"] == "trending_up"
    assert out["2026-05-12"] == "trending_down"
    conn.close()


def test_fetch_vix_by_date_returns_empty_when_table_missing(isolated_db):
    conn = db.init_db()
    out = es.fetch_vix_by_date(conn)
    assert out == {}
    conn.close()


def test_fetch_vix_by_date_reads_macro_table(isolated_db):
    conn = db.init_db()
    db.upsert_macro_value(conn, series_id="VIXCLS",
                          bar_date="2026-05-11", value=18.2)
    db.upsert_macro_value(conn, series_id="VIXCLS",
                          bar_date="2026-05-12", value=22.5)
    db.upsert_macro_value(conn, series_id="T10Y2Y",
                          bar_date="2026-05-11", value=0.34)
    out = es.fetch_vix_by_date(conn)
    assert out == {"2026-05-11": 18.2, "2026-05-12": 22.5}
    conn.close()


# ---------------------------------------------------------------------------
# compute_edge_slices (orchestration)
# ---------------------------------------------------------------------------

def test_compute_edge_slices_empty(isolated_db):
    conn = db.init_db()
    out = es.compute_edge_slices(conn)
    assert out["n_trades_total"] == 0
    assert out["by_dow"] == []
    assert out["by_regime"] == []
    assert out["by_vix"] == []
    assert out["vix_unavailable"] is True
    assert out["regime_unavailable"] is True
    conn.close()


def test_compute_edge_slices_uses_real_data(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "test"}})
    # Two Monday trades (2026-05-11 and 2026-05-18) + one Wednesday.
    _seed_trade(conn, strategy_id="test",
                entry_ts="2026-05-11", exit_ts="2026-05-12",
                entry_price=100.0, exit_price=105.0)
    _seed_trade(conn, strategy_id="test",
                entry_ts="2026-05-18", exit_ts="2026-05-19",
                entry_price=100.0, exit_price=103.0)
    _seed_trade(conn, strategy_id="test",
                entry_ts="2026-05-13", exit_ts="2026-05-14",
                entry_price=100.0, exit_price=98.0)
    db.record_daily_report(
        conn, report_date="2026-05-11", market_regime="bull",
        importance=2, fires_count=0, watchlist_count=0,
        notable_movers_count=0, tags=[], symbols_watched=[],
    )
    out = es.compute_edge_slices(conn)
    assert out["n_trades_total"] == 3
    dow_by_slice = {r["slice"]: r for r in out["by_dow"]}
    assert dow_by_slice["Mon"]["n"] == 2
    assert dow_by_slice["Wed"]["n"] == 1
    # The bull regime should have one trade; the rest get "(unknown)".
    regime_by_slice = {r["slice"]: r for r in out["by_regime"]}
    assert regime_by_slice["bull"]["n"] == 1
    assert regime_by_slice["(unknown)"]["n"] == 2
    conn.close()


# ---------------------------------------------------------------------------
# /api/edge_slices endpoint
# ---------------------------------------------------------------------------

def test_edge_slices_endpoint_empty(client):
    rv = client.get("/api/edge_slices")
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["n_trades_total"] == 0
    assert body["vix_unavailable"] is True
    assert body["regime_unavailable"] is True


def test_edge_slices_endpoint_populated(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_trade(conn, strategy_id="alpha",
                entry_ts="2026-05-11", exit_ts="2026-05-12",
                entry_price=100.0, exit_price=110.0)
    conn.close()
    body = client.get("/api/edge_slices").get_json()
    assert body["n_trades_total"] == 1
    assert body["by_dow"][0]["slice"] == "Mon"
    assert body["by_dow"][0]["n"] == 1


def test_index_html_includes_slices_card(client):
    rv = client.get("/")
    text = rv.get_data(as_text=True)
    assert 'id="edge-slices"' in text
    assert "edge slices" in text.lower()
