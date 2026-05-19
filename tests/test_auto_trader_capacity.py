"""5.5.4.2 — capacity-aware order submission tests."""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    # Seed declarations so trend-a / trend-b are grace_period-enabled
    from monitoring import config as cfg_mod
    decls = [
        {"id": "trend-a", "strategy_class": "trend",
         "compute": "compute_donchian_breakout_20",
         "active_in_regimes": ["trending_up"], "grace_period": True},
        {"id": "trend-b", "strategy_class": "trend",
         "compute": "compute_donchian_breakout_20",
         "active_in_regimes": ["trending_up"], "grace_period": True},
    ]
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES", decls, raising=False)
    # Force regime to trending_up so trend strategies aren't regime-skipped
    from monitoring import regime_router as rr
    monkeypatch.setattr(rr, "latest_regime", lambda c: "trending_up")
    yield test_db


# ---------------------------------------------------------------------------
# _coerce_max_new_entries_per_day
# ---------------------------------------------------------------------------


def test_coerce_max_new_entries_default():
    assert at._coerce_max_new_entries_per_day(None) == at.DEFAULT_MAX_NEW_ENTRIES_PER_DAY


def test_coerce_max_new_entries_positive_passes_through():
    assert at._coerce_max_new_entries_per_day(10) == 10
    assert at._coerce_max_new_entries_per_day("3") == 3


def test_coerce_max_new_entries_zero_disables():
    assert at._coerce_max_new_entries_per_day(0) == 0


def test_coerce_max_new_entries_negative_disables():
    assert at._coerce_max_new_entries_per_day(-1) == 0


def test_coerce_max_new_entries_garbage_falls_back():
    assert at._coerce_max_new_entries_per_day("nope") == at.DEFAULT_MAX_NEW_ENTRIES_PER_DAY


# ---------------------------------------------------------------------------
# _reorder_signals_by_rank
# ---------------------------------------------------------------------------


def _seed_strategies_and_signals(asof_iso, strategy_symbol_pairs):
    """Helper: seed strategy rows + long_entry signals; return signal IDs."""
    conn = db.init_db()
    seeded_strategies = set()
    sigs = []
    for sid, sym in strategy_symbol_pairs:
        if sid not in seeded_strategies:
            db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
            seeded_strategies.add(sid)
        signal_id = db.record_signal(
            conn, strategy_id=sid, symbol=sym,
            bar_ts=asof_iso, signal_type="long_entry",
            close=100.0, bar_interval="1d",
        )
        sigs.append(signal_id)
    return conn, sigs


def test_reorder_signals_by_rank_puts_high_scoring_entries_first(isolated_db):
    asof = "2026-05-19"
    conn, _ = _seed_strategies_and_signals(asof, [
        ("trend-a", "TINY"),
        ("trend-a", "AAPL"),
    ])
    # AAPL has bigger dollar volume → should rank higher
    db.upsert_liquidity_snapshot(
        conn, symbol="AAPL", as_of_date=asof,
        avg_dollar_volume_20d=2_000_000_000.0,
    )
    db.upsert_liquidity_snapshot(
        conn, symbol="TINY", as_of_date=asof,
        avg_dollar_volume_20d=50_000_000.0,
    )

    rows = conn.execute(
        "SELECT id, strategy_id, symbol, signal_type FROM signals ORDER BY id"
    ).fetchall()
    decls = [{"id": "trend-a", "strategy_class": "trend",
              "active_in_regimes": ["trending_up"]}]

    reordered = at._reorder_signals_by_rank(
        rows, regime="trending_up", tracked_strategies=decls, conn=conn,
    )
    syms = [r["symbol"] for r in reordered]
    assert syms[0] == "AAPL"
    assert syms[1] == "TINY"


def test_reorder_signals_preserves_exits_at_start(isolated_db):
    asof = "2026-05-19"
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "trend-a"}})
    db.record_signal(conn, strategy_id="trend-a", symbol="AAPL",
                      bar_ts=asof, signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="trend-a", symbol="MSFT",
                      bar_ts=asof, signal_type="long_exit",
                      close=100.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="trend-a", symbol="NVDA",
                      bar_ts=asof, signal_type="long_entry",
                      close=100.0, bar_interval="1d")

    rows = conn.execute(
        "SELECT id, strategy_id, symbol, signal_type FROM signals ORDER BY id"
    ).fetchall()

    reordered = at._reorder_signals_by_rank(
        rows, regime="trending_up",
        tracked_strategies=[{"id": "trend-a"}],
        conn=conn,
    )
    # MSFT exit should be first
    assert reordered[0]["symbol"] == "MSFT"
    assert reordered[0]["signal_type"] == "long_exit"
    # AAPL + NVDA entries follow (tie-broken alpha by symbol)
    assert reordered[1]["symbol"] in ("AAPL", "NVDA")
    assert reordered[2]["symbol"] in ("AAPL", "NVDA")


def test_reorder_signals_empty_list(isolated_db):
    conn = db.init_db()
    assert at._reorder_signals_by_rank([], regime="mixed",
                                        tracked_strategies=[],
                                        conn=conn) == []


# ---------------------------------------------------------------------------
# End-to-end capacity cap behavior in process_signals
# ---------------------------------------------------------------------------


def _seed_3_signals(asof: str):
    """Seed strategy + 3 entry signals on the same date."""
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "trend-a"}})
    db.upsert_strategy(conn, {"extra": {"strategy_id": "trend-b"}})
    for sid, sym in [("trend-a", "AAPL"), ("trend-a", "MSFT"),
                     ("trend-b", "NVDA")]:
        db.record_signal(conn, strategy_id=sid, symbol=sym,
                          bar_ts=asof, signal_type="long_entry",
                          close=100.0, bar_interval="1d")
    return conn


def _settings_grace(max_new_entries: int = 0):
    """Settings that pass all eligibility (grace-period implicit)."""
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000,
        "max_new_entries_per_day": max_new_entries,
        "grace_period_for_untested": True,
        "min_outcomes_grace_threshold": 30,
    }


def test_capacity_cap_disabled_submits_all(isolated_db):
    """max_new_entries_per_day=0 → all entries process normally."""
    conn = _seed_3_signals("2026-05-19")
    try:
        result = at.process_signals(
            conn, asof=date(2026, 5, 19),
            settings=_settings_grace(max_new_entries=0),
        )
        actions = result["actions"]
        # 3 entries — all become DRY_BUY (grace period passes)
        dry_buys = [a for a in actions if a["action"] == "DRY_BUY"]
        skip_capacity = [a for a in actions if a["action"] == "SKIP_CAPACITY"]
        assert len(dry_buys) == 3
        assert len(skip_capacity) == 0
    finally:
        conn.close()


def test_capacity_cap_skips_after_n_entries(isolated_db):
    """max_new_entries_per_day=2 → first 2 entries, 3rd is SKIP_CAPACITY."""
    conn = _seed_3_signals("2026-05-19")
    try:
        result = at.process_signals(
            conn, asof=date(2026, 5, 19),
            settings=_settings_grace(max_new_entries=2),
        )
        actions = result["actions"]
        dry_buys = [a for a in actions if a["action"] == "DRY_BUY"]
        skip_capacity = [a for a in actions if a["action"] == "SKIP_CAPACITY"]
        assert len(dry_buys) == 2
        assert len(skip_capacity) == 1
        sk = skip_capacity[0]
        assert sk["max_new_entries_per_day"] == 2
        assert sk["entries_submitted_this_run"] == 2
        assert "capacity reached" in sk["reason"]
    finally:
        conn.close()


def test_capacity_cap_max_1_only_top_ranked_submitted(isolated_db):
    """With cap=1 and a clear liquidity ranking, ONLY the top-ranked symbol
    should submit; others get SKIP_CAPACITY."""
    asof = "2026-05-19"
    conn = _seed_3_signals(asof)
    # Make AAPL dramatically more liquid than the others → ranks highest
    db.upsert_liquidity_snapshot(
        conn, symbol="AAPL", as_of_date=asof,
        avg_dollar_volume_20d=5_000_000_000.0,
    )
    db.upsert_liquidity_snapshot(
        conn, symbol="MSFT", as_of_date=asof,
        avg_dollar_volume_20d=50_000_000.0,
    )
    db.upsert_liquidity_snapshot(
        conn, symbol="NVDA", as_of_date=asof,
        avg_dollar_volume_20d=50_000_000.0,
    )
    try:
        result = at.process_signals(
            conn, asof=date(2026, 5, 19),
            settings=_settings_grace(max_new_entries=1),
        )
        dry_buys = [a for a in result["actions"] if a["action"] == "DRY_BUY"]
        skips = [a for a in result["actions"] if a["action"] == "SKIP_CAPACITY"]
        assert len(dry_buys) == 1
        assert dry_buys[0]["symbol"] == "AAPL"
        assert len(skips) == 2
    finally:
        conn.close()


def test_capacity_cap_does_not_block_exits(isolated_db):
    """Exits are not capacity-gated — they must still process to close positions."""
    asof = "2026-05-19"
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "trend-a"}})
    db.record_signal(conn, strategy_id="trend-a", symbol="AAPL",
                      bar_ts=asof, signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="trend-a", symbol="MSFT",
                      bar_ts=asof, signal_type="long_exit",
                      close=100.0, bar_interval="1d")
    try:
        result = at.process_signals(
            conn, asof=date(2026, 5, 19),
            settings=_settings_grace(max_new_entries=1),
        )
        # AAPL entry submits, MSFT exit does NOT count against the cap.
        actions = result["actions"]
        exits = [a for a in actions
                 if a["action"] in ("DRY_SELL", "SELL", "SKIP_NO_POSITION")]
        assert len(exits) == 1
        assert exits[0]["symbol"] == "MSFT"
    finally:
        conn.close()


def test_capacity_cap_skip_reason_logged_with_signal_id(isolated_db):
    """The SKIP_CAPACITY action must carry signal_id for transparency."""
    conn = _seed_3_signals("2026-05-19")
    try:
        result = at.process_signals(
            conn, asof=date(2026, 5, 19),
            settings=_settings_grace(max_new_entries=1),
        )
        skips = [a for a in result["actions"] if a["action"] == "SKIP_CAPACITY"]
        for s in skips:
            assert s.get("signal_id") is not None
            assert s.get("strategy_id")
            assert s.get("symbol")
    finally:
        conn.close()
