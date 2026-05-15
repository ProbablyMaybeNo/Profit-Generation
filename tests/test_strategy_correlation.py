import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402
from monitoring import strategy_correlation as sc  # noqa: E402


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


_BAR_TS_COUNTER = [0]


def _seed_trade(conn, *, strategy_id, exit_ts, return_pct,
                bar_interval="1d", symbol="GDX", entry_ts=None):
    # Each trade gets a unique bar_ts so record_signal doesn't dedupe.
    _BAR_TS_COUNTER[0] += 1
    # Use a synthetic date in the distant past as bar_ts to keep it
    # unique without colliding with exit_ts.
    bar_ts = f"2020-01-{(_BAR_TS_COUNTER[0] % 28) + 1:02d}T{_BAR_TS_COUNTER[0]:05d}"
    sid = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts, signal_type="long_entry",
        close=100.0, bar_interval=bar_interval,
    )
    db.open_outcome(conn, signal_id=sid, entry_ts=entry_ts or exit_ts,
                    entry_price=100.0)
    db.close_outcome(conn, signal_id=sid, exit_ts=exit_ts,
                     exit_price=100.0 * (1 + return_pct / 100),
                     exit_reason="long_exit_signal", bars_held=1)


# ---------------------------------------------------------------------------
# pearson()
# ---------------------------------------------------------------------------

def test_pearson_identical_series_is_one():
    a = [1.0, 2.0, 3.0, 4.0]
    assert sc.pearson(a, a) == pytest.approx(1.0)


def test_pearson_anti_correlated_is_minus_one():
    a = [1.0, 2.0, 3.0, 4.0]
    b = [4.0, 3.0, 2.0, 1.0]
    assert sc.pearson(a, b) == pytest.approx(-1.0)


def test_pearson_orthogonal_random_is_near_zero():
    a = [1.0, -1.0, 1.0, -1.0]
    b = [1.0, 1.0, -1.0, -1.0]
    # mean(a) = mean(b) = 0; sum of products = 0
    assert sc.pearson(a, b) == pytest.approx(0.0)


def test_pearson_empty_inputs():
    assert sc.pearson([], []) == 0.0


def test_pearson_length_mismatch():
    assert sc.pearson([1.0, 2.0], [1.0]) == 0.0


def test_pearson_zero_variance_returns_zero():
    # b is constant — variance 0 → degenerate.
    a = [1.0, 2.0, 3.0]
    b = [5.0, 5.0, 5.0]
    assert sc.pearson(a, b) == 0.0


# ---------------------------------------------------------------------------
# aligned_series
# ---------------------------------------------------------------------------

def test_aligned_series_unions_dates():
    pl = {
        "a": {"2026-01-01": 1.0, "2026-01-03": 2.0},
        "b": {"2026-01-02": 3.0, "2026-01-03": 4.0},
    }
    strategies, dates, series = sc.aligned_series(pl)
    assert strategies == ["a", "b"]
    assert dates == ["2026-01-01", "2026-01-02", "2026-01-03"]
    # Missing dates filled with 0.
    assert series["a"] == [1.0, 0.0, 2.0]
    assert series["b"] == [0.0, 3.0, 4.0]


def test_aligned_series_empty():
    strategies, dates, series = sc.aligned_series({})
    assert strategies == []
    assert dates == []
    assert series == {}


# ---------------------------------------------------------------------------
# build_correlation_matrix
# ---------------------------------------------------------------------------

def test_correlation_identity_for_single_strategy():
    pl = {"a": {"2026-01-01": 1.0, "2026-01-02": 2.0}}
    result = sc.build_correlation_matrix(pl)
    assert result["n_strategies"] == 1
    assert result["matrix"] == [[1.0]]
    assert result["redundant_pairs"] == []


def test_correlation_identity_diagonals():
    pl = {
        "a": {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": -1.0},
        "b": {"2026-01-01": -1.0, "2026-01-02": 3.0, "2026-01-03": 2.0},
    }
    result = sc.build_correlation_matrix(pl)
    n = result["n_strategies"]
    for i in range(n):
        assert result["matrix"][i][i] == pytest.approx(1.0)


def test_correlation_is_symmetric():
    pl = {
        "a": {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": -1.0},
        "b": {"2026-01-01": -1.0, "2026-01-02": 3.0, "2026-01-03": 2.0},
    }
    result = sc.build_correlation_matrix(pl)
    m = result["matrix"]
    assert m[0][1] == m[1][0]


def test_correlation_perfectly_aligned_strategies_is_one():
    # Same series → corr 1.0
    pl = {
        "a": {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": -1.0},
        "b": {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": -1.0},
    }
    result = sc.build_correlation_matrix(pl)
    assert result["matrix"][0][1] == pytest.approx(1.0)


def test_correlation_anti_correlated_strategies():
    pl = {
        "a": {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": -1.0},
        "b": {"2026-01-01": -1.0, "2026-01-02": -2.0, "2026-01-03": 1.0},
    }
    result = sc.build_correlation_matrix(pl)
    assert result["matrix"][0][1] == pytest.approx(-1.0)


def test_correlation_flags_redundant_pairs():
    pl = {
        "a": {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": -1.0},
        "b": {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": -1.0},
    }
    result = sc.build_correlation_matrix(pl)
    assert len(result["redundant_pairs"]) == 1
    assert result["redundant_pairs"][0]["a"] == "a"
    assert result["redundant_pairs"][0]["b"] == "b"


def test_correlation_no_redundant_pairs_for_uncorrelated():
    pl = {
        "a": {"2026-01-01": 1.0, "2026-01-02": -1.0,
              "2026-01-03": 1.0, "2026-01-04": -1.0},
        "b": {"2026-01-01": 1.0, "2026-01-02": 1.0,
              "2026-01-03": -1.0, "2026-01-04": -1.0},
    }
    result = sc.build_correlation_matrix(pl)
    # corr ≈ 0 → no redundancy
    assert result["redundant_pairs"] == []


def test_correlation_empty_input():
    result = sc.build_correlation_matrix({})
    assert result["n_strategies"] == 0
    assert result["matrix"] == []
    assert result["strategies"] == []


def test_correlation_degenerate_zero_variance_series():
    # a has variance > 0; b has constant series → corr = 0.0
    pl = {
        "a": {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": -1.0},
        "b": {"2026-01-01": 5.0, "2026-01-02": 5.0, "2026-01-03": 5.0},
    }
    result = sc.build_correlation_matrix(pl)
    assert result["matrix"][0][1] == 0.0


# ---------------------------------------------------------------------------
# fetch_pl_by_strategy_and_date
# ---------------------------------------------------------------------------

def test_fetch_pl_aggregates_multiple_trades_on_same_day(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "a"}})
    _seed_trade(conn, strategy_id="a", exit_ts="2026-05-11", return_pct=2.0)
    _seed_trade(conn, strategy_id="a", exit_ts="2026-05-11", return_pct=3.0)
    pl = sc.fetch_pl_by_strategy_and_date(conn)
    assert pl["a"]["2026-05-11"] == pytest.approx(5.0)
    conn.close()


def test_fetch_pl_excludes_non_1d(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "a"}})
    _seed_trade(conn, strategy_id="a", exit_ts="2026-05-11", return_pct=2.0)
    _seed_trade(conn, strategy_id="a", exit_ts="2026-05-12",
                return_pct=3.0, bar_interval="1d-intraday")
    pl = sc.fetch_pl_by_strategy_and_date(conn)
    assert "2026-05-12" not in pl.get("a", {})
    conn.close()


# ---------------------------------------------------------------------------
# /api/strategy_correlation endpoint
# ---------------------------------------------------------------------------

def test_endpoint_empty_when_no_trades(client):
    body = client.get("/api/strategy_correlation").get_json()
    assert body["n_strategies"] == 0
    assert body["matrix"] == []


def test_endpoint_single_strategy_yields_1x1(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "solo"}})
    _seed_trade(conn, strategy_id="solo", exit_ts="2026-05-11", return_pct=1.0)
    conn.close()
    body = client.get("/api/strategy_correlation").get_json()
    assert body["n_strategies"] == 1
    assert body["matrix"] == [[1.0]]


def test_endpoint_two_correlated_strategies(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "a"}})
    db.upsert_strategy(conn, {"extra": {"strategy_id": "b"}})
    for d, r in [("2026-05-11", 1.0), ("2026-05-12", 2.0),
                 ("2026-05-13", -1.0)]:
        _seed_trade(conn, strategy_id="a", exit_ts=d, return_pct=r)
        _seed_trade(conn, strategy_id="b", exit_ts=d, return_pct=r)
    conn.close()
    body = client.get("/api/strategy_correlation").get_json()
    assert body["n_strategies"] == 2
    assert body["matrix"][0][1] == pytest.approx(1.0, abs=0.01)
    assert len(body["redundant_pairs"]) == 1


def test_index_html_includes_correlation_card(client):
    text = client.get("/").get_data(as_text=True)
    assert 'id="strategy-correlation"' in text
    assert "strategy correlation" in text.lower()
