import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402
from monitoring import strategy_forecast as sf  # noqa: E402


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


def _seed_signal(conn, *, strategy_id, symbol, bar_ts,
                 signal_type="long_entry", bar_interval="1d", close=100.0):
    return db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts, signal_type=signal_type,
        close=close, bar_interval=bar_interval,
    )


def _seed_trade(conn, *, strategy_id, symbol, entry_ts, return_pct,
                bar_ts=None, bar_interval="1d"):
    sid = _seed_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts or entry_ts, bar_interval=bar_interval,
    )
    db.open_outcome(conn, signal_id=sid, entry_ts=entry_ts,
                    entry_price=100.0)
    db.close_outcome(
        conn, signal_id=sid, exit_ts=entry_ts,
        exit_price=100.0 * (1 + return_pct / 100),
        exit_reason="long_exit_signal", bars_held=1,
    )
    return sid


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def test_confidence_high():
    assert sf._confidence(40, 120) == "high"


def test_confidence_medium():
    assert sf._confidence(15, 60) == "medium"


def test_confidence_low_few_trades():
    assert sf._confidence(2, 200) == "low"


def test_confidence_low_short_window():
    assert sf._confidence(100, 10) == "low"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_summary_empty_when_no_data():
    text = sf._build_summary(None, None)
    assert text == "(no historical fires)"


def test_summary_includes_fires_per_month():
    text = sf._build_summary(12.0, 0.5)
    assert "12 fires/month" in text
    assert "+0.50%" in text


def test_summary_low_frequency_keeps_precision():
    text = sf._build_summary(0.3, None)
    assert "0.30" in text


def test_summary_only_fires_when_no_returns():
    text = sf._build_summary(8.0, None)
    assert "8 fires/month" in text
    assert "median" not in text


def test_summary_only_returns_when_no_fires():
    text = sf._build_summary(None, 1.25)
    assert "median +1.25%" in text


# ---------------------------------------------------------------------------
# fetch_signal_dates / fetch_closed_returns
# ---------------------------------------------------------------------------

def test_fetch_signal_dates_dedupes_and_sorts(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_signal(conn, strategy_id="s1", symbol="GDX",
                  bar_ts="2026-05-15")
    _seed_signal(conn, strategy_id="s1", symbol="KRE",
                  bar_ts="2026-05-15")
    _seed_signal(conn, strategy_id="s1", symbol="GDX",
                  bar_ts="2026-05-01")
    dates = sf.fetch_signal_dates(conn, "s1")
    assert [d.isoformat() for d in dates] == ["2026-05-01", "2026-05-15"]
    conn.close()


def test_fetch_signal_dates_filters_by_strategy(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s2"}})
    _seed_signal(conn, strategy_id="s1", symbol="GDX",
                  bar_ts="2026-05-15")
    _seed_signal(conn, strategy_id="s2", symbol="GDX",
                  bar_ts="2026-05-15")
    assert len(sf.fetch_signal_dates(conn, "s1")) == 1
    conn.close()


def test_fetch_signal_dates_skips_long_exit(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_signal(conn, strategy_id="s1", symbol="GDX",
                  bar_ts="2026-05-15", signal_type="long_exit")
    assert sf.fetch_signal_dates(conn, "s1") == []
    conn.close()


def test_fetch_closed_returns(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-01", return_pct=2.0)
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-02", return_pct=-1.0,
                bar_ts="2026-05-02b")
    rets = sf.fetch_closed_returns(conn, "s1")
    assert sorted(rets) == [-1.0, 2.0]
    conn.close()


# ---------------------------------------------------------------------------
# compute_forecast
# ---------------------------------------------------------------------------

def test_compute_forecast_no_data(isolated_db):
    conn = db.init_db()
    out = sf.compute_forecast(conn, "nope")
    assert out["n_signals_observed"] == 0
    assert out["n_trades"] == 0
    assert out["fires_per_month"] is None
    assert out["median_return_pct"] is None
    assert out["confidence"] == "low"
    assert out["summary"] == "(no historical fires)"
    conn.close()


def test_compute_forecast_single_signal_uses_fallback_window(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_signal(conn, strategy_id="s1", symbol="GDX",
                  bar_ts="2026-05-15")
    out = sf.compute_forecast(conn, "s1")
    assert out["n_signals_observed"] == 1
    assert out["observation_days"] == sf.DEFAULT_FALLBACK_DAYS
    assert out["fires_per_month"] is not None
    assert out["confidence"] == "low"
    conn.close()


def test_compute_forecast_fires_per_month_math(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    # 12 signals across 365.25 days → 1 fire/month exactly.
    for i in range(12):
        _seed_signal(
            conn, strategy_id="s1", symbol="GDX",
            bar_ts=f"2025-{i+1:02d}-01",
        )
    # Span the full year by adding a signal on 2026-01-01.
    # Wait — we want 12 signals across ~365 days. The 12 monthly
    # signals span Jan 2025 → Dec 2025 (≈334 days) which gives roughly
    # 1.09 fires/month. Use that approximate assertion.
    out = sf.compute_forecast(conn, "s1")
    assert out["n_signals_observed"] == 12
    assert out["fires_per_month"] is not None
    assert 0.9 <= out["fires_per_month"] <= 1.3
    conn.close()


def test_compute_forecast_median_and_mean(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    rets = [1.0, 2.0, 3.0, -1.0, 5.0]
    for i, r in enumerate(rets):
        _seed_trade(
            conn, strategy_id="s1", symbol="GDX",
            entry_ts=f"2026-05-{i+1:02d}",
            return_pct=r, bar_ts=f"2026-05-{i+1:02d}",
        )
    out = sf.compute_forecast(conn, "s1")
    assert out["median_return_pct"] == pytest.approx(2.0)
    assert out["mean_return_pct"] == pytest.approx(2.0)
    assert out["win_rate"] == pytest.approx(0.8)
    conn.close()


def test_compute_forecast_high_confidence_threshold(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    # 35 trades, each on a distinct ISO bar_ts spanning ~12 months
    # (Jan 2025 day 1 .. Dec 2025 day 11) → comfortably high confidence.
    for i in range(35):
        month = (i % 12) + 1
        day = (i // 12) + 1  # 1..3 across months
        iso = f"2025-{month:02d}-{day:02d}"
        _seed_trade(
            conn, strategy_id="s1", symbol="GDX",
            entry_ts=iso, return_pct=0.5, bar_ts=iso,
        )
    out = sf.compute_forecast(conn, "s1")
    assert out["confidence"] == "high"
    assert out["n_trades"] == 35
    conn.close()


def test_compute_forecast_summary_phrasing(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    # Seed enough signals for ~12 fires/month over 30 days.
    for day in range(1, 13):
        _seed_signal(conn, strategy_id="s1", symbol="GDX",
                      bar_ts=f"2026-05-{day:02d}")
    # And a few outcomes for median.
    for i in range(5):
        _seed_trade(conn, strategy_id="s1", symbol="GDX",
                    entry_ts=f"2026-04-{i+1:02d}",
                    return_pct=0.5, bar_ts=f"2026-04-{i+1:02d}b")
    out = sf.compute_forecast(conn, "s1")
    # Spec phrasing: "expected: ~12 fires/month, median +0.5%/trade".
    assert out["summary"].startswith("expected:")
    assert "fires/month" in out["summary"]
    assert "median" in out["summary"]
    conn.close()


def test_compute_forecast_excludes_non_1d(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_signal(conn, strategy_id="s1", symbol="GDX",
                  bar_ts="2026-05-15",
                  bar_interval="1d-intraday")
    out = sf.compute_forecast(conn, "s1")
    assert out["n_signals_observed"] == 0
    conn.close()


# ---------------------------------------------------------------------------
# /api/strategy_forecast/<sid> endpoint
# ---------------------------------------------------------------------------

def test_endpoint_unknown_strategy_returns_empty(client):
    body = client.get("/api/strategy_forecast/does-not-exist").get_json()
    assert body["strategy_id"] == "does-not-exist"
    assert body["n_signals_observed"] == 0
    assert body["summary"] == "(no historical fires)"


def test_endpoint_returns_summary_for_real_strategy(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_signal(conn, strategy_id="s1", symbol="GDX",
                  bar_ts="2026-04-01")
    _seed_signal(conn, strategy_id="s1", symbol="GDX",
                  bar_ts="2026-05-01")
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-04-15", return_pct=1.5,
                bar_ts="2026-04-15b")
    conn.close()
    body = client.get("/api/strategy_forecast/s1").get_json()
    assert body["strategy_id"] == "s1"
    assert body["n_signals_observed"] >= 2
    assert body["fires_per_month"] is not None
    assert "fires/month" in body["summary"]


# ---------------------------------------------------------------------------
# /api/state strategy_edge.forecast inclusion
# ---------------------------------------------------------------------------

def test_state_strategy_edge_includes_forecast(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-01", return_pct=2.0)
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-02", return_pct=-1.0,
                bar_ts="2026-05-02b")
    conn.close()
    body = client.get("/api/state").get_json()
    edges = body["strategy_edge"]
    assert edges and edges[0]["strategy_id"] == "s1"
    fc = edges[0].get("forecast")
    assert fc is not None
    assert "summary" in fc
    assert fc["fires_per_month"] is not None
