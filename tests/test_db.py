import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = db.init_db(tmp_path / "trading.db")
    yield c
    c.close()


def test_init_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    expected = {
        "strategies", "signals", "snapshots", "daily_reports",
        "news", "outcomes", "paper_trades", "patterns", "meta",
    }
    assert expected.issubset(names)


def test_meta_schema_version_set(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert row["value"] == db.SCHEMA_VERSION


def test_upsert_strategy_idempotent(conn):
    record = {
        "url": "https://example/x",
        "title": "Test strat",
        "tags": ["a", "b"],
        "extra": {
            "strategy_id": "test-1",
            "methodology_family": "mean-rev",
            "current_verdict": "PASS",
            "verdict_summary": "v1",
            "instruments": ["SPY"],
            "first_logged_iso": "2026-04-26",
        },
    }
    db.upsert_strategy(conn, record)
    db.upsert_strategy(conn, record)
    rows = conn.execute("SELECT * FROM strategies").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["strategy_id"] == "test-1"
    assert r["current_verdict"] == "PASS"
    assert json.loads(r["instruments_json"]) == ["SPY"]
    assert json.loads(r["tags_json"]) == ["a", "b"]


def test_upsert_strategy_updates_verdict(conn):
    rec = {"extra": {"strategy_id": "s1", "current_verdict": "MARGINAL"}}
    db.upsert_strategy(conn, rec)
    rec["extra"]["current_verdict"] = "FAIL"
    db.upsert_strategy(conn, rec)
    row = conn.execute("SELECT current_verdict FROM strategies WHERE strategy_id='s1'").fetchone()
    assert row["current_verdict"] == "FAIL"


def test_set_strategy_active_on(conn):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s2"}})
    db.set_strategy_active_on(conn, "s2", ["XLE", "GDX"], compute_fn="compute_5day_low")
    row = conn.execute(
        "SELECT active_on_json, compute_fn FROM strategies WHERE strategy_id='s2'"
    ).fetchone()
    assert json.loads(row["active_on_json"]) == ["XLE", "GDX"]
    assert row["compute_fn"] == "compute_5day_low"


def test_record_signal_unique(conn):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s3"}})
    sid_a = db.record_signal(
        conn, strategy_id="s3", symbol="GDX", bar_ts="2026-05-13",
        signal_type="long_entry", close=42.5,
    )
    sid_b = db.record_signal(
        conn, strategy_id="s3", symbol="GDX", bar_ts="2026-05-13",
        signal_type="long_entry", close=42.5,
    )
    assert sid_a is not None
    assert sid_b is None
    rows = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
    assert rows["n"] == 1


def test_signals_distinguish_intervals(conn):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s4"}})
    db.record_signal(conn, strategy_id="s4", symbol="SPY", bar_ts="2026-05-13",
                     signal_type="long_entry", bar_interval="1d")
    db.record_signal(conn, strategy_id="s4", symbol="SPY", bar_ts="2026-05-13T15:30:00",
                     signal_type="long_entry", bar_interval="1m")
    n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert n == 2


def test_snapshot_upsert(conn):
    row = {"symbol": "SPY", "asset_class": "major_index", "bar_date": "2026-05-13",
           "close": 500.0, "ret_1d_pct": 0.5, "ret_5d_pct": 1.2, "ret_20d_pct": 3.1,
           "rvol_vs_20d": 1.1, "dist_sma20_pct": 0.4}
    db.record_snapshot_row(conn, "2026-05-13", row)
    row["close"] = 501.0
    db.record_snapshot_row(conn, "2026-05-13", row)
    rows = conn.execute("SELECT * FROM snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["close"] == 501.0


def test_record_daily_report_round_trip(conn):
    s1 = db.record_daily_report(
        conn,
        report_date="2026-05-13",
        market_regime="choppy",
        importance=3,
        fires_count=2,
        watchlist_count=10,
        notable_movers_count=4,
        tags=["gap-up"],
        symbols_watched=["SPY", "QQQ"],
        markdown="# hi",
    )
    s2 = db.record_daily_report(
        conn,
        report_date="2026-05-13",
        market_regime="trending_up",
        importance=4,
        fires_count=3,
        watchlist_count=10,
        notable_movers_count=5,
        tags=["gap-up", "high-volume"],
        symbols_watched=["SPY", "QQQ", "IWM"],
    )
    assert s1 == "inserted"
    assert s2 == "updated"
    rows = conn.execute("SELECT * FROM daily_reports").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["market_regime"] == "trending_up"
    assert r["importance"] == 4
    assert r["fires_count"] == 3
    assert r["markdown"] == "# hi"
    assert json.loads(r["tags_json"]) == ["gap-up", "high-volume"]


def test_record_daily_report_skips_downgrade(conn):
    """Defensive guard: a re-run with fewer fires must not clobber the high-water row."""
    db.record_daily_report(
        conn, report_date="2026-05-15", market_regime="trending_up",
        importance=4, fires_count=8, watchlist_count=13, notable_movers_count=10,
        tags=["high-volume"], symbols_watched=list("ABCDEFGHIJKLM"),
    )
    status = db.record_daily_report(
        conn, report_date="2026-05-15", market_regime="mixed",
        importance=1, fires_count=0, watchlist_count=0, notable_movers_count=0,
        tags=["low-volume"], symbols_watched=[],
    )
    assert status == "skipped_downgrade"
    r = conn.execute("SELECT * FROM daily_reports WHERE report_date='2026-05-15'").fetchone()
    assert r["fires_count"] == 8
    assert r["importance"] == 4
    assert r["market_regime"] == "trending_up"


def test_record_daily_report_force_overrides_downgrade_guard(conn):
    db.record_daily_report(
        conn, report_date="2026-05-15", market_regime="trending_up",
        importance=4, fires_count=8, watchlist_count=13, notable_movers_count=10,
        tags=[], symbols_watched=list("ABCDEFGHIJKLM"),
    )
    status = db.record_daily_report(
        conn, report_date="2026-05-15", market_regime="mixed",
        importance=1, fires_count=0, watchlist_count=0, notable_movers_count=0,
        tags=[], symbols_watched=[], force=True,
    )
    assert status == "updated"
    r = conn.execute("SELECT * FROM daily_reports WHERE report_date='2026-05-15'").fetchone()
    assert r["fires_count"] == 0
    assert r["importance"] == 1


def test_record_daily_report_allows_upgrade(conn):
    """A re-run with MORE fires should still overwrite (no force needed)."""
    db.record_daily_report(
        conn, report_date="2026-05-15", market_regime="mixed",
        importance=2, fires_count=2, watchlist_count=13, notable_movers_count=3,
        tags=[], symbols_watched=list("ABCDEFGHIJKLM"),
    )
    status = db.record_daily_report(
        conn, report_date="2026-05-15", market_regime="trending_up",
        importance=4, fires_count=8, watchlist_count=13, notable_movers_count=10,
        tags=[], symbols_watched=list("ABCDEFGHIJKLM"),
    )
    assert status == "updated"
    r = conn.execute("SELECT * FROM daily_reports WHERE report_date='2026-05-15'").fetchone()
    assert r["fires_count"] == 8


def test_news_dedupe_per_symbol(conn):
    item = {"polygon_id": "abc", "published_utc": "2026-05-13T12:00:00Z",
            "symbol": "GDX", "title": "Gold pops", "url": "https://x"}
    a = db.insert_news(conn, item)
    b = db.insert_news(conn, item)
    assert a is not None
    assert b is None
    item2 = {**item, "symbol": "XME"}
    c = db.insert_news(conn, item2)
    assert c is not None
    assert conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 2


def test_outcome_open_then_close(conn):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s5"}})
    sid = db.record_signal(
        conn, strategy_id="s5", symbol="XLE", bar_ts="2026-05-12",
        signal_type="long_entry", close=85.0,
    )
    db.open_outcome(conn, signal_id=sid, entry_ts="2026-05-12T20:00:00Z", entry_price=85.0)
    db.close_outcome(
        conn, signal_id=sid, exit_ts="2026-05-13T20:00:00Z",
        exit_price=86.7, exit_reason="long_exit_signal", bars_held=1,
    )
    row = conn.execute("SELECT * FROM outcomes WHERE signal_id=?", (sid,)).fetchone()
    assert row["status"] == "closed"
    assert row["exit_reason"] == "long_exit_signal"
    assert abs(row["return_pct"] - (1.7 / 85.0 * 100.0)) < 1e-9


def test_close_outcome_requires_open(conn):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s6"}})
    sid = db.record_signal(
        conn, strategy_id="s6", symbol="KRE", bar_ts="2026-05-13",
        signal_type="long_entry", close=50.0,
    )
    with pytest.raises(ValueError):
        db.close_outcome(conn, signal_id=sid, exit_ts="x", exit_price=51.0,
                         exit_reason="manual")


def test_pattern_upsert_increments_count(conn):
    p = {"name": "Friday GDX strength", "importance": 2, "description": "first"}
    db.upsert_pattern(conn, p)
    db.upsert_pattern(conn, {"name": "Friday GDX strength"})
    row = conn.execute("SELECT * FROM patterns WHERE name='Friday GDX strength'").fetchone()
    assert row["observed_count"] == 2
    assert row["description"] == "first"
    assert row["importance"] == 2


def test_query_recent_signals_filters(conn):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s7"}})
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s8"}})
    db.record_signal(conn, strategy_id="s7", symbol="GDX", bar_ts="2026-05-12",
                     signal_type="long_entry")
    db.record_signal(conn, strategy_id="s7", symbol="XME", bar_ts="2026-05-13",
                     signal_type="long_entry")
    db.record_signal(conn, strategy_id="s8", symbol="GDX", bar_ts="2026-05-13",
                     signal_type="long_entry")
    by_sym = db.query_recent_signals(conn, symbol="GDX")
    assert {r["strategy_id"] for r in by_sym} == {"s7", "s8"}
    by_strat = db.query_recent_signals(conn, strategy_id="s7")
    assert all(r["strategy_id"] == "s7" for r in by_strat)
    assert len(by_strat) == 2


def test_paper_trade_upsert(conn):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s9"}})
    sid = db.record_signal(
        conn, strategy_id="s9", symbol="SPY", bar_ts="2026-05-13",
        signal_type="long_entry", close=500.0,
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": "ord-1", "signal_id": sid, "strategy_id": "s9",
        "symbol": "SPY", "side": "buy", "qty": 10.0, "order_type": "market",
        "submitted_at": "2026-05-13T13:31:00Z", "status": "accepted",
    })
    db.record_paper_trade(conn, {
        "alpaca_order_id": "ord-1", "symbol": "SPY", "side": "buy", "qty": 10.0,
        "order_type": "market", "filled_at": "2026-05-13T13:31:02Z",
        "fill_price": 500.05, "status": "filled",
    })
    row = conn.execute("SELECT * FROM paper_trades WHERE alpaca_order_id='ord-1'").fetchone()
    assert row["status"] == "filled"
    assert row["fill_price"] == 500.05
    assert row["signal_id"] == sid
    assert row["submitted_at"] == "2026-05-13T13:31:00Z"
