"""Regression tests for db.ensure_strategies_seeded — the helper that
prevents FK constraint failures when new strategies are added to
TRACKED_STRATEGIES. This bug bit us 3 times (trend on 2026-05-18,
intraday on 2026-05-19) before this helper was added."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def test_ensure_seeded_inserts_missing(isolated_db):
    conn = db.init_db()
    tracked = [
        {"id": "new-strat-1", "strategy_class": "trend",
         "bar_interval": "1d", "active_on": ["SPY"], "compute": "compute_x"},
        {"id": "new-strat-2", "strategy_class": "mean_reversion",
         "bar_interval": "15m", "active_on": ["QQQ"], "compute": "compute_y"},
    ]
    new_ids = db.ensure_strategies_seeded(conn, tracked)
    assert set(new_ids) == {"new-strat-1", "new-strat-2"}
    rows = {r[0] for r in conn.execute("SELECT strategy_id FROM strategies")}
    assert "new-strat-1" in rows
    assert "new-strat-2" in rows


def test_ensure_seeded_skips_existing(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "already-here", "title": "ALREADY"}})
    tracked = [{"id": "already-here", "strategy_class": "trend"}]
    new_ids = db.ensure_strategies_seeded(conn, tracked)
    assert new_ids == []  # existing row not touched, not in newly_inserted
    # Title should be unchanged (not clobbered)
    row = conn.execute("SELECT title FROM strategies WHERE strategy_id='already-here'").fetchone()
    assert row[0] == "ALREADY"


def test_ensure_seeded_safe_to_run_twice(isolated_db):
    conn = db.init_db()
    tracked = [{"id": "stable", "strategy_class": "trend", "bar_interval": "1d"}]
    first = db.ensure_strategies_seeded(conn, tracked)
    second = db.ensure_strategies_seeded(conn, tracked)
    assert first == ["stable"]
    assert second == []  # idempotent


def test_ensure_seeded_handles_malformed_entries(isolated_db):
    conn = db.init_db()
    # Mix of valid entries, missing id, and non-dict entries
    tracked = [
        {"id": "valid-strat", "strategy_class": "trend"},
        {"no_id_key": "skipped"},  # no 'id' → skipped
        "not-a-dict",               # non-dict → skipped
        None,                       # None → skipped
        {"id": ""},                 # empty id → skipped
    ]
    new_ids = db.ensure_strategies_seeded(conn, tracked)
    assert new_ids == ["valid-strat"]


def test_ensure_seeded_empty_input(isolated_db):
    conn = db.init_db()
    assert db.ensure_strategies_seeded(conn, []) == []
    assert db.ensure_strategies_seeded(conn, None) == []
