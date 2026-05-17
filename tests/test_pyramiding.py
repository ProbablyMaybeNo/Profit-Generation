"""Tests for monitoring.pyramiding (milestone 4.6.2).

Tier sequencing, size math, regime veto, max-N cap, declaration gating.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import pyramiding as py  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


# ---------------------------------------------------------------------------
# Tier math
# ---------------------------------------------------------------------------

def test_tier_progression_default_schedule():
    """Initial=100 → [100, 50, 25, 13] (rounded)."""
    out = py.tier_progression(initial_qty=100)
    # 12.5 rounds to bank-round 12 in py3 (round-half-to-even).
    assert out == [100, 50, 25, 12]


def test_tier_progression_custom_schedule():
    out = py.tier_progression(initial_qty=80,
                                tier_schedule=[1.0, 0.5, 0.25],
                                max_tiers=4)
    assert out == [80, 40, 20]


def test_next_addon_size_first_pyramid():
    """current_tier=0 (only the initial entry exists) → tier 1 = 50."""
    qty = py.next_addon_size(initial_qty=100, current_tier=0)
    assert qty == 50


def test_next_addon_size_second_pyramid():
    qty = py.next_addon_size(initial_qty=100, current_tier=1)
    assert qty == 25


def test_next_addon_size_max_tier_returns_none():
    """max_tiers=4 means tiers 0,1,2,3 allowed — next from 3 → None."""
    qty = py.next_addon_size(initial_qty=100, current_tier=3, max_tiers=4)
    assert qty is None


def test_next_addon_size_schedule_exhausted():
    qty = py.next_addon_size(initial_qty=100, current_tier=5,
                              tier_schedule=[1.0, 0.5], max_tiers=10)
    assert qty is None


def test_next_addon_size_rounds_to_zero_returns_none():
    """initial_qty=1 → tier 2 would be 0.25 → rounds to 0 → None."""
    qty = py.next_addon_size(initial_qty=1, current_tier=1)
    assert qty is None


# ---------------------------------------------------------------------------
# is_pyramidable + regime
# ---------------------------------------------------------------------------

def test_is_pyramidable_default_false():
    assert py.is_pyramidable(None) is False
    assert py.is_pyramidable({}) is False
    assert py.is_pyramidable({"pyramidable": False}) is False


def test_is_pyramidable_true_when_declared():
    assert py.is_pyramidable({"pyramidable": True}) is True


def test_regime_allows_addon_only_for_trend_class_in_friendly_regime():
    assert py.regime_allows_addon("bull", strategy_class="trend") is True
    assert py.regime_allows_addon("trend", strategy_class="trend") is True
    assert py.regime_allows_addon("chop", strategy_class="trend") is False
    assert py.regime_allows_addon("bear", strategy_class="trend") is False


def test_regime_veto_for_mean_reversion_class():
    """Mean-reversion strategies must NEVER pyramid regardless of regime."""
    assert py.regime_allows_addon(
        "bull", strategy_class="mean_reversion",
    ) is False
    assert py.regime_allows_addon(
        "trend", strategy_class="mean_reversion",
    ) is False


def test_regime_handles_missing_regime():
    assert py.regime_allows_addon(None, strategy_class="trend") is False
    assert py.regime_allows_addon("", strategy_class="trend") is False


# ---------------------------------------------------------------------------
# current_tier + record_addon_tier — DB integration
# ---------------------------------------------------------------------------

def _seed_buy(conn, *, strategy_id, symbol, tier=None, status="filled"):
    cur = conn.execute(
        "INSERT INTO paper_trades(strategy_id, symbol, side, qty, "
        " status, pyramid_tier, submitted_at) "
        "VALUES (?, ?, 'buy', 10, ?, ?, '2026-05-17T10:00:00Z')",
        (strategy_id, symbol, status, tier),
    )
    conn.commit()
    return cur.lastrowid


def test_current_tier_zero_when_no_addons(isolated_db):
    conn = db.init_db()
    try:
        _seed_buy(conn, strategy_id="s", symbol="X")  # tier NULL
        assert py.current_tier(conn, strategy_id="s", symbol="X") == 0
    finally:
        conn.close()


def test_current_tier_returns_max_pyramid_tier(isolated_db):
    conn = db.init_db()
    try:
        _seed_buy(conn, strategy_id="s", symbol="X")
        _seed_buy(conn, strategy_id="s", symbol="X", tier=1)
        _seed_buy(conn, strategy_id="s", symbol="X", tier=2)
        assert py.current_tier(conn, strategy_id="s", symbol="X") == 2
    finally:
        conn.close()


def test_current_tier_isolates_by_strategy_and_symbol(isolated_db):
    conn = db.init_db()
    try:
        _seed_buy(conn, strategy_id="s", symbol="X", tier=2)
        _seed_buy(conn, strategy_id="s", symbol="Y", tier=0)
        _seed_buy(conn, strategy_id="t", symbol="X", tier=1)
        assert py.current_tier(conn, strategy_id="s", symbol="X") == 2
        assert py.current_tier(conn, strategy_id="s", symbol="Y") == 0
        assert py.current_tier(conn, strategy_id="t", symbol="X") == 1
    finally:
        conn.close()


def test_current_tier_excludes_canceled_trades(isolated_db):
    conn = db.init_db()
    try:
        _seed_buy(conn, strategy_id="s", symbol="X", tier=2, status="canceled")
        # No live buys — tier == 0.
        assert py.current_tier(conn, strategy_id="s", symbol="X") == 0
    finally:
        conn.close()


def test_record_addon_tier_updates_row(isolated_db):
    conn = db.init_db()
    try:
        pid = _seed_buy(conn, strategy_id="s", symbol="X")
        assert py.record_addon_tier(
            conn, paper_trade_id=pid, tier=1
        ) is True
        row = conn.execute(
            "SELECT pyramid_tier FROM paper_trades WHERE id=?",
            (pid,),
        ).fetchone()
        assert row["pyramid_tier"] == 1
    finally:
        conn.close()


def test_record_addon_tier_returns_false_for_missing_row(isolated_db):
    conn = db.init_db()
    try:
        assert py.record_addon_tier(
            conn, paper_trade_id=99999, tier=1,
        ) is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# evaluate_addon — end-to-end eligibility chain
# ---------------------------------------------------------------------------

def test_evaluate_addon_happy_path(isolated_db):
    conn = db.init_db()
    try:
        _seed_buy(conn, strategy_id="trend1", symbol="GDX")
        out = py.evaluate_addon(
            conn, strategy_id="trend1", symbol="GDX",
            initial_qty=100, regime="bull",
            declaration={"pyramidable": True},
            strategy_class="trend",
        )
        assert out["action"] == "ADDON"
        assert out["tier"] == 1
        assert out["qty"] == 50  # 100 * 0.5
    finally:
        conn.close()


def test_evaluate_addon_veto_not_pyramidable(isolated_db):
    conn = db.init_db()
    try:
        out = py.evaluate_addon(
            conn, strategy_id="meanrev", symbol="X",
            initial_qty=100, regime="bull",
            declaration={"pyramidable": False},
            strategy_class="mean_reversion",
        )
        assert out["action"] == "VETO_NOT_PYRAMIDABLE"
    finally:
        conn.close()


def test_evaluate_addon_veto_regime(isolated_db):
    conn = db.init_db()
    try:
        out = py.evaluate_addon(
            conn, strategy_id="trend1", symbol="X",
            initial_qty=100, regime="chop",
            declaration={"pyramidable": True},
            strategy_class="trend",
        )
        assert out["action"] == "VETO_REGIME"
    finally:
        conn.close()


def test_evaluate_addon_veto_max_tiers(isolated_db):
    conn = db.init_db()
    try:
        _seed_buy(conn, strategy_id="trend1", symbol="X", tier=3)
        out = py.evaluate_addon(
            conn, strategy_id="trend1", symbol="X",
            initial_qty=100, regime="bull",
            declaration={"pyramidable": True},
            strategy_class="trend",
        )
        assert out["action"] == "VETO_MAX_TIERS"
    finally:
        conn.close()


def test_evaluate_addon_mean_reversion_class_never_addons(isolated_db):
    """Even with pyramidable=true (which should never be declared on
    mean-rev), the strategy_class veto wins."""
    conn = db.init_db()
    try:
        out = py.evaluate_addon(
            conn, strategy_id="meanrev", symbol="X",
            initial_qty=100, regime="bull",
            declaration={"pyramidable": True},
            strategy_class="mean_reversion",
        )
        assert out["action"] == "VETO_REGIME"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema check — paper_trades.pyramid_tier column exists
# ---------------------------------------------------------------------------

def test_paper_trades_has_pyramid_tier_column(isolated_db):
    conn = db.init_db()
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(paper_trades)"
        ).fetchall()}
        assert "pyramid_tier" in cols
    finally:
        conn.close()
