import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import cross_validation as cv  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_signal(conn, strategy_id, symbol, bar_ts, bar_interval, signal_type):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts, signal_type=signal_type,
        close=100.0, bar_interval=bar_interval,
    )


# ---------------------------------------------------------------------------
# collect_signals_in_window
# ---------------------------------------------------------------------------

def test_collect_only_returns_three_known_intervals(isolated_db):
    conn = db.init_db()
    try:
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d", "long_entry")
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1h", "long_entry")
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d-intraday", "long_entry")
        out = cv.collect_signals_in_window(conn, asof=date(2026, 5, 17),
                                            window_days=7)
        intervals = {r["bar_interval"] for r in out}
        assert intervals == {"1d", "1d-intraday"}
    finally:
        conn.close()


def test_collect_excludes_outside_window(isolated_db):
    conn = db.init_db()
    try:
        _seed_signal(conn, "alpha", "A", "2026-05-01", "1d", "long_entry")
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d", "long_entry")
        out = cv.collect_signals_in_window(conn, asof=date(2026, 5, 17),
                                            window_days=7)
        assert len(out) == 1
        assert out[0]["bar_ts"] == "2026-05-12"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# group_by_tuple + find_disagreements
# ---------------------------------------------------------------------------

def test_no_disagreement_when_only_one_source(isolated_db):
    conn = db.init_db()
    try:
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d", "long_entry")
        rows = cv.collect_signals_in_window(
            conn, asof=date(2026, 5, 17), window_days=7,
        )
    finally:
        conn.close()
    grouped = cv.group_by_tuple(rows)
    assert cv.find_disagreements(grouped) == []


def test_no_disagreement_when_all_agree(isolated_db):
    conn = db.init_db()
    try:
        for iv in ("1d", "1d-intraday", "tv-webhook"):
            _seed_signal(conn, "alpha", "A", "2026-05-12", iv, "long_entry")
        rows = cv.collect_signals_in_window(
            conn, asof=date(2026, 5, 17), window_days=7,
        )
    finally:
        conn.close()
    grouped = cv.group_by_tuple(rows)
    assert cv.find_disagreements(grouped) == []


def test_disagreement_when_two_sources_differ(isolated_db):
    conn = db.init_db()
    try:
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d", "long_entry")
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d-intraday", "long_exit")
        rows = cv.collect_signals_in_window(
            conn, asof=date(2026, 5, 17), window_days=7,
        )
    finally:
        conn.close()
    grouped = cv.group_by_tuple(rows)
    diss = cv.find_disagreements(grouped)
    assert len(diss) == 1
    d = diss[0]
    assert d["strategy_id"] == "alpha"
    assert d["symbol"] == "A"
    assert set(d["diff"]) == {"long_entry", "long_exit"}
    assert d["sources"]["1d"] == ["long_entry"]
    assert d["sources"]["1d-intraday"] == ["long_exit"]


def test_disagreement_when_one_source_missing_type(isolated_db):
    """Intraday + TV say long_entry; EOD says nothing → no group entry for 1d.
    With ≥2 sources observed and matching signal_types, that's NOT a
    disagreement under this definition (we can't claim disagreement from
    absence). The function only flags when ≥2 sources fire AND differ."""
    conn = db.init_db()
    try:
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d-intraday", "long_entry")
        _seed_signal(conn, "alpha", "A", "2026-05-12", "tv-webhook", "long_entry")
        rows = cv.collect_signals_in_window(
            conn, asof=date(2026, 5, 17), window_days=7,
        )
    finally:
        conn.close()
    grouped = cv.group_by_tuple(rows)
    # Both fired the same type → no disagreement.
    assert cv.find_disagreements(grouped) == []


def test_disagreement_sources_breakdown_complete(isolated_db):
    conn = db.init_db()
    try:
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d", "long_entry")
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d-intraday", "long_entry")
        _seed_signal(conn, "alpha", "A", "2026-05-12", "tv-webhook", "long_exit")
        rows = cv.collect_signals_in_window(
            conn, asof=date(2026, 5, 17), window_days=7,
        )
    finally:
        conn.close()
    grouped = cv.group_by_tuple(rows)
    diss = cv.find_disagreements(grouped)
    assert len(diss) == 1
    sources = diss[0]["sources"]
    assert "1d" in sources and "1d-intraday" in sources and "tv-webhook" in sources


# ---------------------------------------------------------------------------
# compute_cross_validation
# ---------------------------------------------------------------------------

def test_compute_cross_validation_empty(isolated_db):
    conn = db.init_db()
    try:
        rollup = cv.compute_cross_validation(
            conn, asof=date(2026, 5, 17), window_days=7,
        )
        assert rollup["n_signals"] == 0
        assert rollup["n_tuples"] == 0
        assert rollup["n_disagreements"] == 0
    finally:
        conn.close()


def test_compute_cross_validation_counts(isolated_db):
    conn = db.init_db()
    try:
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d", "long_entry")
        _seed_signal(conn, "alpha", "A", "2026-05-12", "1d-intraday", "long_exit")
        _seed_signal(conn, "beta", "B", "2026-05-13", "1d", "long_entry")
        rollup = cv.compute_cross_validation(
            conn, asof=date(2026, 5, 17), window_days=7,
        )
        assert rollup["n_signals"] == 3
        assert rollup["n_tuples"] == 2
        assert rollup["n_disagreements"] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------

def test_render_markdown_no_disagreements():
    rollup = {
        "window_start": "2026-05-11", "window_end": "2026-05-17",
        "n_signals": 4, "n_tuples": 3, "n_disagreements": 0,
        "disagreements": [], "evaluated_at": "",
    }
    md = cv.render_markdown(rollup)
    assert "Cross-validation" in md
    assert "No source disagreements" in md


def test_render_markdown_lists_disagreements():
    rollup = {
        "window_start": "2026-05-11", "window_end": "2026-05-17",
        "n_signals": 5, "n_tuples": 2, "n_disagreements": 1,
        "disagreements": [{
            "strategy_id": "alpha", "symbol": "A", "bar_ts": "2026-05-12",
            "sources": {"1d": ["long_entry"], "1d-intraday": ["long_exit"]},
            "disagreement_type": "long_entry, long_exit",
            "diff": ["long_entry", "long_exit"],
        }],
        "evaluated_at": "",
    }
    md = cv.render_markdown(rollup)
    assert "alpha" in md
    assert "long_entry" in md
    assert "long_exit" in md


# ---------------------------------------------------------------------------
# post_to_notion
# ---------------------------------------------------------------------------

def test_post_to_notion_noop_when_empty():
    rollup = {"disagreements": [], "window_start": "x", "window_end": "y"}
    assert cv.post_to_notion(rollup) is None


def test_post_to_notion_posts_one_per_disagreement(monkeypatch):
    calls = []

    def fake_post_pattern(**kwargs):
        calls.append(kwargs)
        return {"id": f"p-{len(calls)}"}

    monkeypatch.setattr(
        "monitoring.notion_writer.post_pattern", fake_post_pattern,
    )
    rollup = {
        "window_start": "2026-05-11", "window_end": "2026-05-17",
        "disagreements": [
            {"strategy_id": "alpha", "symbol": "A", "bar_ts": "2026-05-12",
             "sources": {"1d": ["long_entry"], "1d-intraday": ["long_exit"]},
             "disagreement_type": "x", "diff": ["long_entry", "long_exit"]},
            {"strategy_id": "beta", "symbol": "B", "bar_ts": "2026-05-13",
             "sources": {"1d": ["long_entry"], "tv-webhook": ["long_exit"]},
             "disagreement_type": "y", "diff": ["long_entry", "long_exit"]},
        ],
    }
    cv.post_to_notion(rollup, database_id="db-xx")
    assert len(calls) == 2
    titles = [c["title"] for c in calls]
    assert any("alpha" in t for t in titles)
    assert any("beta" in t for t in titles)
    assert all(c["database_id"] == "db-xx" for c in calls)
    assert all(c["pattern_type"] == "cross-validation" for c in calls)
