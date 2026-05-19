"""Tests for monitoring.fill_latency (milestone 3.6.3).

Per-strategy fill-time latency rollup driven by paper_trades.submitted_at
vs paper_trades.filled_at. Outliers = latency > OUTLIER_THRESHOLD_S (5min).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402
from monitoring import fill_latency as fl  # noqa: E402


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


def _seed_filled_pair(conn, *, strategy_id, symbol, alpaca_order_id,
                     submitted_at, filled_at, side="buy", status="filled"):
    db.record_paper_trade(conn, {
        "alpaca_order_id": alpaca_order_id,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": side,
        "qty": 10,
        "order_type": "market",
        "submitted_at": submitted_at,
        "filled_at": filled_at,
        "fill_price": 100.0,
        "status": status,
    })


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------

def test_parse_iso_basic():
    dt = fl._parse_iso("2026-05-17T15:30:00")
    assert dt is not None
    assert dt.year == 2026 and dt.minute == 30


def test_parse_iso_with_z_suffix():
    """Z suffix (UTC) should be tolerated and converted to +00:00."""
    dt = fl._parse_iso("2026-05-17T15:30:00Z")
    assert dt is not None


def test_parse_iso_empty_and_none():
    assert fl._parse_iso(None) is None
    assert fl._parse_iso("") is None
    assert fl._parse_iso("   ") is None


def test_parse_iso_garbage_returns_none():
    assert fl._parse_iso("not a timestamp") is None
    assert fl._parse_iso(12345) is None


# ---------------------------------------------------------------------------
# _median / _percentile
# ---------------------------------------------------------------------------

def test_median_odd_count():
    assert fl._median([3.0, 1.0, 2.0]) == pytest.approx(2.0)


def test_median_even_count():
    assert fl._median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)


def test_median_empty():
    assert fl._median([]) == 0.0


def test_percentile_p95():
    # 20 values 1..20; p95 nearest-rank → index round(0.95*19)=18 → value 19
    vals = [float(i) for i in range(1, 21)]
    assert fl._percentile(vals, 95.0) == pytest.approx(19.0)


def test_percentile_single_value():
    assert fl._percentile([42.0], 95.0) == 42.0


def test_percentile_empty():
    assert fl._percentile([], 95.0) == 0.0


# ---------------------------------------------------------------------------
# fetch_latencies
# ---------------------------------------------------------------------------

def test_fetch_latencies_empty(isolated_db):
    conn = db.init_db()
    assert fl.fetch_latencies(conn) == {}
    conn.close()


def test_fetch_latencies_basic(isolated_db):
    conn = db.init_db()
    _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                     alpaca_order_id="a1",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at="2026-05-17T15:30:10")  # 10s
    out = fl.fetch_latencies(conn)
    assert "alpha" in out
    assert out["alpha"] == [pytest.approx(10.0)]
    conn.close()


def test_fetch_latencies_excludes_missing_timestamps(isolated_db):
    conn = db.init_db()
    _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                     alpaca_order_id="missing_fill",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at=None)
    _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                     alpaca_order_id="missing_sub",
                     submitted_at=None,
                     filled_at="2026-05-17T15:30:10")
    out = fl.fetch_latencies(conn)
    assert out == {}
    conn.close()


def test_fetch_latencies_excludes_negative_clock_skew(isolated_db):
    conn = db.init_db()
    _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                     alpaca_order_id="skew1",
                     submitted_at="2026-05-17T15:30:10",
                     filled_at="2026-05-17T15:30:00")  # filled BEFORE submit
    out = fl.fetch_latencies(conn)
    assert out == {}
    conn.close()


# ---------------------------------------------------------------------------
# compute_fill_latency
# ---------------------------------------------------------------------------

def test_compute_fill_latency_empty(isolated_db):
    conn = db.init_db()
    out = fl.compute_fill_latency(conn)
    assert out["rows"] == []
    assert out["n_strategies"] == 0
    assert out["n_trades_total"] == 0
    assert out["overall_median_s"] is None
    assert out["outlier_threshold_s"] == 300
    conn.close()


def test_compute_fill_latency_basic(isolated_db):
    """Two fills with known deltas → median = 5s, p95 = 10s, no outliers."""
    conn = db.init_db()
    _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                     alpaca_order_id="a1",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at="2026-05-17T15:30:00")  # 0s
    _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                     alpaca_order_id="a2",
                     submitted_at="2026-05-17T15:31:00",
                     filled_at="2026-05-17T15:31:10")  # 10s
    out = fl.compute_fill_latency(conn)
    assert out["n_strategies"] == 1
    row = out["rows"][0]
    assert row["strategy_id"] == "alpha"
    assert row["n"] == 2
    assert row["median_s"] == pytest.approx(5.0)
    assert row["outliers"] == 0
    conn.close()


def test_compute_fill_latency_flags_outliers(isolated_db):
    """One fill over 5 minutes → counted as outlier."""
    conn = db.init_db()
    _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                     alpaca_order_id="fast1",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at="2026-05-17T15:30:05")  # 5s
    _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                     alpaca_order_id="slow1",
                     submitted_at="2026-05-17T15:35:00",
                     filled_at="2026-05-17T15:42:00")  # 7min = 420s
    out = fl.compute_fill_latency(conn)
    row = out["rows"][0]
    assert row["n"] == 2
    assert row["outliers"] == 1
    assert row["outlier_pct"] == pytest.approx(50.0)
    conn.close()


def test_compute_fill_latency_sorts_by_median_desc(isolated_db):
    """Higher median → top of the list."""
    conn = db.init_db()
    # fast strategy: 5s
    _seed_filled_pair(conn, strategy_id="fast", symbol="GDX",
                     alpaca_order_id="f1",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at="2026-05-17T15:30:05")
    # slow strategy: 60s
    _seed_filled_pair(conn, strategy_id="slow", symbol="GDX",
                     alpaca_order_id="s1",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at="2026-05-17T15:31:00")
    out = fl.compute_fill_latency(conn)
    assert [r["strategy_id"] for r in out["rows"]] == ["slow", "fast"]
    conn.close()


def test_compute_fill_latency_overall_median(isolated_db):
    conn = db.init_db()
    _seed_filled_pair(conn, strategy_id="a", symbol="GDX",
                     alpaca_order_id="a1",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at="2026-05-17T15:30:10")
    _seed_filled_pair(conn, strategy_id="b", symbol="GDX",
                     alpaca_order_id="b1",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at="2026-05-17T15:30:30")
    _seed_filled_pair(conn, strategy_id="c", symbol="GDX",
                     alpaca_order_id="c1",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at="2026-05-17T15:30:20")
    out = fl.compute_fill_latency(conn)
    # Latencies: 10, 30, 20 → median = 20
    assert out["overall_median_s"] == pytest.approx(20.0)
    assert out["n_trades_total"] == 3


def test_compute_fill_latency_p95_calculation(isolated_db):
    """20 fills, latencies 1..20 → p95 = 19 (nearest-rank)."""
    conn = db.init_db()
    for i in range(1, 21):
        sub = "2026-05-17T15:30:00"
        fill_minute = 30 + (i // 60)
        fill_second = i % 60
        fill_ts = f"2026-05-17T15:{fill_minute:02d}:{fill_second:02d}"
        _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                         alpaca_order_id=f"o{i}",
                         submitted_at=sub,
                         filled_at=fill_ts)
    out = fl.compute_fill_latency(conn)
    row = out["rows"][0]
    assert row["n"] == 20
    assert row["p95_s"] == pytest.approx(19.0)


# ---------------------------------------------------------------------------
# /api/fill_latency endpoint
# ---------------------------------------------------------------------------

def test_endpoint_empty(client):
    body = client.get("/api/fill_latency").get_json()
    assert body["n_strategies"] == 0
    assert body["rows"] == []
    assert body["outlier_threshold_s"] == 300


def test_endpoint_returns_rows(client, isolated_db):
    conn = db.init_db()
    _seed_filled_pair(conn, strategy_id="alpha", symbol="GDX",
                     alpaca_order_id="o1",
                     submitted_at="2026-05-17T15:30:00",
                     filled_at="2026-05-17T15:30:30")
    conn.close()
    body = client.get("/api/fill_latency").get_json()
    assert body["n_strategies"] == 1
    assert body["rows"][0]["strategy_id"] == "alpha"
    assert body["rows"][0]["median_s"] == pytest.approx(30.0)


def test_index_html_includes_fill_latency_card(client):
    text = client.get("/research").get_data(as_text=True)
    assert 'id="fill-latency"' in text
    assert "fill latency" in text.lower()
