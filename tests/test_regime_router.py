"""Tests for monitoring.regime_router and its auto_trader integration."""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import regime_router as rr  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_daily_report(conn, *, report_date: str, market_regime: str):
    db.record_daily_report(
        conn,
        report_date=report_date,
        market_regime=market_regime,
        importance=1,
        fires_count=0, watchlist_count=0, notable_movers_count=0,
        tags=[], symbols_watched=[],
        has_notable_pattern=False, force=True,
    )


def _seed_winner(conn, *, returns):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    for i, ret in enumerate(returns):
        sid = db.record_signal(
            conn, strategy_id="winner", symbol="X",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )


def _winner_settings(**overrides):
    s = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 1, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.0,
        "max_position_usd": 1000,
    }
    s.update(overrides)
    return s


# ---------- latest_regime ----------

def test_latest_regime_defaults_to_mixed_when_empty(isolated_db):
    conn = db.init_db()
    assert rr.latest_regime(conn) == "mixed"


def test_latest_regime_reads_most_recent_row(isolated_db):
    conn = db.init_db()
    _seed_daily_report(conn, report_date="2026-05-10", market_regime="choppy")
    _seed_daily_report(conn, report_date="2026-05-14", market_regime="trending_up")
    _seed_daily_report(conn, report_date="2026-05-12", market_regime="low_vol")
    assert rr.latest_regime(conn) == "trending_up"


def test_latest_regime_skips_null_rows(isolated_db):
    conn = db.init_db()
    _seed_daily_report(conn, report_date="2026-05-10", market_regime="choppy")
    # Insert a more recent row with NULL regime via raw SQL.
    conn.execute(
        "INSERT INTO daily_reports(report_date, market_regime, importance, "
        " has_notable_pattern, fires_count, watchlist_count, "
        " notable_movers_count, tags_json, symbols_watched_json, "
        " notion_page_id, markdown, generated_at) "
        "VALUES('2026-05-14', NULL, 1, 0, 0, 0, 0, '[]', '[]', NULL, NULL, "
        "  '2026-05-14T20:00:00+00:00')"
    )
    conn.commit()
    assert rr.latest_regime(conn) == "choppy"


def test_latest_regime_unknown_value_falls_back(isolated_db):
    conn = db.init_db()
    _seed_daily_report(conn, report_date="2026-05-14",
                        market_regime="bananas")
    assert rr.latest_regime(conn) == "mixed"


# ---------- _coerce_regimes ----------

def test_coerce_regimes_none_returns_none():
    assert rr._coerce_regimes(None) is None


def test_coerce_regimes_empty_list_returns_none():
    assert rr._coerce_regimes([]) is None


def test_coerce_regimes_string_input_promoted_to_list():
    out = rr._coerce_regimes("choppy")
    assert out == frozenset({"choppy"})


def test_coerce_regimes_drops_unknown():
    out = rr._coerce_regimes(["choppy", "trending_up", "made_up"])
    assert out == frozenset({"choppy", "trending_up"})


def test_coerce_regimes_all_unknown_falls_back_to_none():
    """All-unknown is treated as undeclared (active in ALL) rather than
    silently locking the strategy out forever."""
    assert rr._coerce_regimes(["xxx", "yyy"]) is None


def test_coerce_regimes_invalid_type_falls_back_to_none():
    assert rr._coerce_regimes(42) is None
    assert rr._coerce_regimes({"a": 1}) is None


# ---------- strategy_active_in_regime ----------

def test_strategy_active_when_undeclared_defaults_to_active():
    assert rr.strategy_active_in_regime({"id": "s"}, "choppy") is True


def test_strategy_active_when_declared_matches():
    meta = {"id": "s", "active_in_regimes": ["choppy", "low_vol"]}
    assert rr.strategy_active_in_regime(meta, "choppy") is True


def test_strategy_inactive_when_declared_excludes():
    meta = {"id": "s", "active_in_regimes": ["choppy"]}
    assert rr.strategy_active_in_regime(meta, "trending_up") is False


def test_strategy_active_for_non_dict_meta_defaults_true():
    assert rr.strategy_active_in_regime(None, "choppy") is True


# ---------- regime_skip ----------

def test_regime_skip_returns_none_for_unknown_strategy():
    """Strategies not in TRACKED_STRATEGIES are unaffected by the router."""
    assert rr.regime_skip("orphan", regime="choppy",
                          tracked_strategies=[]) is None


def test_regime_skip_returns_none_when_undeclared():
    tracked = [{"id": "winner"}]  # no active_in_regimes
    assert rr.regime_skip("winner", regime="trending_up",
                          tracked_strategies=tracked) is None


def test_regime_skip_returns_none_when_regime_in_allowed():
    tracked = [{"id": "winner", "active_in_regimes": ["choppy", "low_vol"]}]
    assert rr.regime_skip("winner", regime="choppy",
                          tracked_strategies=tracked) is None


def test_regime_skip_returns_dict_on_mismatch():
    tracked = [{"id": "winner", "active_in_regimes": ["choppy"]}]
    out = rr.regime_skip("winner", regime="trending_up",
                          tracked_strategies=tracked)
    assert out is not None
    assert out["current_regime"] == "trending_up"
    assert out["allowed_regimes"] == ["choppy"]
    assert "winner" in out["reason"]


# ---------- auto_trader integration ----------

def test_process_signals_skips_entry_on_regime_mismatch(isolated_db,
                                                          monkeypatch):
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    _seed_daily_report(conn, report_date="2026-05-14",
                        market_regime="trending_up")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    monkeypatch.setattr(
        "monitoring.config.TRACKED_STRATEGIES",
        [{"id": "winner", "compute": "compute_5day_low",
          "active_on": ["GDX"], "active_in_regimes": ["choppy", "low_vol"]}],
    )
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_winner_settings())
    assert res["market_regime"] == "trending_up"
    skipped = [a for a in res["actions"]
                if a.get("action") == "SKIP_REGIME_MISMATCH"]
    assert len(skipped) == 1
    assert skipped[0]["current_regime"] == "trending_up"
    assert skipped[0]["allowed_regimes"] == ["choppy", "low_vol"]
    # No DRY_BUY should sneak through.
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    assert len(buys) == 0


def test_process_signals_allows_entry_when_regime_matches(isolated_db,
                                                            monkeypatch):
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    _seed_daily_report(conn, report_date="2026-05-14",
                        market_regime="choppy")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    monkeypatch.setattr(
        "monitoring.config.TRACKED_STRATEGIES",
        [{"id": "winner", "compute": "compute_5day_low",
          "active_on": ["GDX"], "active_in_regimes": ["choppy", "low_vol"]}],
    )
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_winner_settings())
    assert res["market_regime"] == "choppy"
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    assert len(buys) == 1


def test_process_signals_undeclared_strategy_defaults_to_all_regimes(
    isolated_db, monkeypatch,
):
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    _seed_daily_report(conn, report_date="2026-05-14",
                        market_regime="trending_down")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    # Tracked but no active_in_regimes — should NOT be skipped on any regime.
    monkeypatch.setattr(
        "monitoring.config.TRACKED_STRATEGIES",
        [{"id": "winner", "compute": "compute_5day_low",
          "active_on": ["GDX"]}],
    )
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_winner_settings())
    skipped = [a for a in res["actions"]
                if a.get("action") == "SKIP_REGIME_MISMATCH"]
    assert len(skipped) == 0
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    assert len(buys) == 1


def test_process_signals_default_regime_when_no_reports(isolated_db,
                                                          monkeypatch):
    """No daily_reports → latest_regime returns 'mixed'; a strategy gated to
    'mixed' is allowed; a strategy gated AWAY from 'mixed' is skipped."""
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    monkeypatch.setattr(
        "monitoring.config.TRACKED_STRATEGIES",
        [{"id": "winner", "compute": "compute_5day_low",
          "active_on": ["GDX"], "active_in_regimes": ["trending_up"]}],
    )
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_winner_settings())
    assert res["market_regime"] == "mixed"
    skipped = [a for a in res["actions"]
                if a.get("action") == "SKIP_REGIME_MISMATCH"]
    assert len(skipped) == 1


def test_process_signals_does_not_skip_exits_on_regime_mismatch(isolated_db,
                                                                  monkeypatch):
    """Exits must process regardless of regime — open positions still need
    to be closed even if their strategy is now gated out of the regime."""
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    _seed_daily_report(conn, report_date="2026-05-14",
                        market_regime="trending_up")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_exit",
                     close=110.0, bar_interval="1d")
    monkeypatch.setattr(
        "monitoring.config.TRACKED_STRATEGIES",
        [{"id": "winner", "compute": "compute_5day_low",
          "active_on": ["GDX"], "active_in_regimes": ["choppy"]}],
    )
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_winner_settings())
    # SKIP_REGIME_MISMATCH only applies to entries; the exit reaches
    # _process_exit which returns SKIP_NO_OPEN_POSITION when there's no
    # matching open trade. Either way, no regime-skip on the exit.
    actions = [a.get("action") for a in res["actions"]]
    assert "SKIP_REGIME_MISMATCH" not in actions


# ---------------------------------------------------------------------------
# Capital allocation (milestone 4.6.4)
# ---------------------------------------------------------------------------

def test_allocation_trend_regime_favors_trend():
    out = rr.allocation_for_regime("trending_up")
    assert out["trend"] == 0.7
    assert out["mean_reversion"] == 0.3
    assert out["fallback"] is False


def test_allocation_choppy_favors_mean_reversion():
    out = rr.allocation_for_regime("choppy")
    assert out["trend"] == 0.3
    assert out["mean_reversion"] == 0.7


def test_allocation_low_vol_favors_mean_reversion():
    out = rr.allocation_for_regime("low_vol")
    assert out["trend"] == 0.3
    assert out["mean_reversion"] == 0.7


def test_allocation_mixed_is_balanced():
    out = rr.allocation_for_regime("mixed")
    assert out["trend"] == 0.5
    assert out["mean_reversion"] == 0.5


def test_allocation_low_confidence_falls_back_to_50_50():
    """Below the confidence floor, the allocator falls back to 50/50
    regardless of declared regime."""
    out = rr.allocation_for_regime("trending_up", confidence=0.4)
    assert out["trend"] == 0.5
    assert out["mean_reversion"] == 0.5
    assert out["fallback"] is True


def test_allocation_high_confidence_uses_declared_regime():
    out = rr.allocation_for_regime("trending_up", confidence=0.9)
    assert out["trend"] == 0.7
    assert out["fallback"] is False


def test_allocation_at_confidence_floor_uses_declared_regime():
    """Strictly below the floor is fallback. AT the floor is still ok."""
    out = rr.allocation_for_regime(
        "trending_up", confidence=rr.DEFAULT_CONFIDENCE_FLOOR,
    )
    assert out["fallback"] is False


def test_allocation_unknown_regime_falls_back():
    out = rr.allocation_for_regime("garbage")
    assert out["trend"] == 0.5
    assert out["mean_reversion"] == 0.5
    assert out["fallback"] is True


def test_allocation_uses_supplied_table():
    custom = {"weird": (0.99, 0.01)}
    out = rr.allocation_for_regime("weird", table=custom)
    assert out["trend"] == 0.99
    assert out["mean_reversion"] == 0.01
    assert out["fallback"] is False


# ---------------------------------------------------------------------------
# size_multiplier
# ---------------------------------------------------------------------------

def test_size_multiplier_trend_strategy_gets_trend_share():
    alloc = {"trend": 0.7, "mean_reversion": 0.3}
    assert rr.size_multiplier("trend", allocation=alloc) == 0.7


def test_size_multiplier_mean_reversion_gets_mr_share():
    alloc = {"trend": 0.7, "mean_reversion": 0.3}
    assert rr.size_multiplier("mean_reversion",
                                allocation=alloc) == 0.3


def test_size_multiplier_hyphenated_alias():
    alloc = {"trend": 0.7, "mean_reversion": 0.3}
    assert rr.size_multiplier("mean-reversion",
                                allocation=alloc) == 0.3


def test_size_multiplier_other_class_unaffected():
    """Strategy classes the allocator doesn't know about (intraday,
    crypto-only, etc.) keep their full sizing."""
    alloc = {"trend": 0.7, "mean_reversion": 0.3}
    assert rr.size_multiplier("intraday", allocation=alloc) == 1.0


# ---------------------------------------------------------------------------
# regime_to_allocation_class — dashboard hint helper
# ---------------------------------------------------------------------------

def test_regime_to_allocation_class_trend():
    assert rr.regime_to_allocation_class("trending_up") == "trend_favored"
    assert rr.regime_to_allocation_class("trending_down") == "trend_favored"


def test_regime_to_allocation_class_mean_reversion():
    assert rr.regime_to_allocation_class("choppy") == "mean_reversion_favored"
    assert rr.regime_to_allocation_class("low_vol") == "mean_reversion_favored"


def test_regime_to_allocation_class_balanced():
    assert rr.regime_to_allocation_class("mixed") == "balanced"
    assert rr.regime_to_allocation_class("garbage") == "balanced"


# ---------------------------------------------------------------------------
# Regime transitions — same logic re-run produces stable allocations
# ---------------------------------------------------------------------------

def test_allocation_stable_across_repeated_calls():
    a = rr.allocation_for_regime("trending_up", confidence=0.9)
    b = rr.allocation_for_regime("trending_up", confidence=0.9)
    assert a == b
