import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import signal_ranker as sr  # noqa: E402


# ---------------------------------------------------------------------------
# Component multipliers
# ---------------------------------------------------------------------------


def test_regime_multiplier_matches_active_regime():
    # KNOWN_REGIMES in regime_router: trending_up, trending_down, low_vol, choppy, mixed
    meta = {"id": "t1", "active_in_regimes": ["trending_up", "low_vol"]}
    assert sr._regime_multiplier(meta, "trending_up") == sr.REGIME_ALIGN_MULT
    assert sr._regime_multiplier(meta, "low_vol") == sr.REGIME_ALIGN_MULT
    assert sr._regime_multiplier(meta, "choppy") == 1.0


def test_regime_multiplier_undeclared_means_active_in_all():
    meta = {"id": "t1"}  # no active_in_regimes
    assert sr._regime_multiplier(meta, "bull") == sr.REGIME_ALIGN_MULT
    assert sr._regime_multiplier(meta, "choppy") == sr.REGIME_ALIGN_MULT


def test_regime_multiplier_none_meta():
    assert sr._regime_multiplier(None, "bull") == 1.0


def test_volume_multiplier_fires_on_high_rvol():
    # 20 bars of vol=1M, last bar = 2M → 2x rvol → mult 1.3
    closes = [100.0] * 21
    volumes = [1_000_000] * 20 + [2_000_000]
    df = pd.DataFrame({"close": closes, "volume": volumes})
    assert sr._volume_multiplier(df) == sr.VOLUME_CONFIRM_MULT


def test_volume_multiplier_skips_on_normal_volume():
    df = pd.DataFrame({
        "close": [100.0] * 21,
        "volume": [1_000_000] * 20 + [1_100_000],  # 1.1x — not enough
    })
    assert sr._volume_multiplier(df) == 1.0


def test_volume_multiplier_handles_short_df():
    df = pd.DataFrame({"close": [100.0] * 10, "volume": [1e6] * 10})
    assert sr._volume_multiplier(df) == 1.0


def test_volume_multiplier_handles_none():
    assert sr._volume_multiplier(None) == 1.0


def test_volume_multiplier_handles_zero_avg():
    df = pd.DataFrame({
        "close": [100.0] * 21,
        "volume": [0] * 20 + [1_000_000],
    })
    assert sr._volume_multiplier(df) == 1.0


def test_edge_multiplier_bands():
    assert sr._edge_multiplier(None) == sr.EDGE_DEFAULT
    assert sr._edge_multiplier(-0.5) == sr.EDGE_DEFAULT
    assert sr._edge_multiplier(0.0) == sr.EDGE_DEFAULT
    assert sr._edge_multiplier(0.3) == 1.1
    assert sr._edge_multiplier(0.5) == 1.1   # boundary exclusive
    assert sr._edge_multiplier(0.7) == 1.25
    assert sr._edge_multiplier(1.0) == 1.25  # boundary exclusive
    assert sr._edge_multiplier(1.5) == 1.5


def test_liquidity_multiplier_bands():
    assert sr._liquidity_multiplier(None) == sr.LIQUIDITY_DEFAULT
    assert sr._liquidity_multiplier(0.0) == sr.LIQUIDITY_DEFAULT
    assert sr._liquidity_multiplier(50_000_000) == 1.0
    assert sr._liquidity_multiplier(100_000_000) == 1.1   # boundary inclusive
    assert sr._liquidity_multiplier(250_000_000) == 1.1
    assert sr._liquidity_multiplier(500_000_000) == 1.2
    assert sr._liquidity_multiplier(2_000_000_000) == 1.2


# ---------------------------------------------------------------------------
# rank_signals composition
# ---------------------------------------------------------------------------


def test_rank_signals_orders_by_score_desc():
    fires = [
        {"strategy_id": "trend-a", "symbol": "AAPL"},
        {"strategy_id": "trend-a", "symbol": "MSFT"},
    ]
    decls = [{"id": "trend-a", "active_in_regimes": ["trending_up"]}]
    sharpe = {"trend-a": 0.8}
    dvol = {"AAPL": 600_000_000, "MSFT": 150_000_000}

    ranked = sr.rank_signals(
        fires, regime="trending_up",
        strategy_decls=decls,
        sharpe_by_strategy=sharpe,
        dollar_volume_by_symbol=dvol,
    )
    # AAPL: 1.5 (regime) * 1.0 (vol, no bars) * 1.25 (sharpe band) * 1.2 (liq) = 2.25
    # MSFT: 1.5 * 1.0 * 1.25 * 1.1 = 2.0625
    assert ranked[0]["symbol"] == "AAPL"
    assert ranked[1]["symbol"] == "MSFT"
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[0]["score"] == pytest.approx(2.25)
    assert ranked[1]["score"] == pytest.approx(2.0625)


def test_rank_signals_score_breakdown_present():
    fires = [{"strategy_id": "x", "symbol": "AAPL"}]
    out = sr.rank_signals(fires, regime="bull")
    assert "score_breakdown" in out[0]
    assert set(out[0]["score_breakdown"].keys()) == {
        "regime", "volume", "edge", "liquidity",
    }


def test_rank_signals_ties_broken_by_symbol_then_strategy():
    fires = [
        {"strategy_id": "z-strat", "symbol": "MSFT"},
        {"strategy_id": "a-strat", "symbol": "MSFT"},
        {"strategy_id": "z-strat", "symbol": "AAPL"},
        {"strategy_id": "a-strat", "symbol": "AAPL"},
    ]
    # All same score → tie-break alpha by symbol, then by strategy
    ranked = sr.rank_signals(fires, regime="mixed")
    order = [(r["symbol"], r["strategy_id"]) for r in ranked]
    assert order == [
        ("AAPL", "a-strat"),
        ("AAPL", "z-strat"),
        ("MSFT", "a-strat"),
        ("MSFT", "z-strat"),
    ]


def test_rank_signals_empty_input():
    assert sr.rank_signals([]) == []


def test_rank_signals_handles_missing_strategy_meta():
    fires = [{"strategy_id": "unknown", "symbol": "AAPL"}]
    ranked = sr.rank_signals(fires, regime="bull")
    # No decl → no regime alignment bonus (1.0), defaults all → score = 1.0
    assert ranked[0]["score"] == pytest.approx(1.0)


def test_rank_signals_volume_bars_lift_score():
    fires = [{"strategy_id": "t", "symbol": "AAPL"}]
    bars = {"AAPL": pd.DataFrame({
        "close": [100.0] * 21,
        "volume": [1_000_000] * 20 + [3_000_000],
    })}
    without_vol = sr.rank_signals(fires, regime="mixed")[0]
    with_vol = sr.rank_signals(fires, regime="mixed", bars_by_symbol=bars)[0]
    assert with_vol["score"] > without_vol["score"]
    assert with_vol["score"] / without_vol["score"] == pytest.approx(sr.VOLUME_CONFIRM_MULT)


def test_rank_signals_input_dicts_not_mutated():
    fires = [{"strategy_id": "t", "symbol": "AAPL"}]
    original = dict(fires[0])
    sr.rank_signals(fires, regime="bull")
    assert fires[0] == original


# ---------------------------------------------------------------------------
# DB lookups
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def test_dollar_volume_lookup_from_db(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_liquidity_snapshot(
            conn, symbol="AAPL", as_of_date="2026-05-19",
            avg_dollar_volume_20d=10e9,
        )
        db.upsert_liquidity_snapshot(
            conn, symbol="MSFT", as_of_date="2026-05-19",
            avg_dollar_volume_20d=5e9,
        )
        out = sr.dollar_volume_lookup_from_db(["AAPL", "MSFT", "MISSING"], conn=conn)
        assert out["AAPL"] == pytest.approx(10e9)
        assert out["MSFT"] == pytest.approx(5e9)
        assert "MISSING" not in out
    finally:
        conn.close()


def test_sharpe_lookup_returns_empty_when_no_outcomes(isolated_db):
    conn = db.init_db()
    try:
        out = sr.sharpe_lookup_from_db(["trend-a"], conn=conn)
        assert out == {}
    finally:
        conn.close()
