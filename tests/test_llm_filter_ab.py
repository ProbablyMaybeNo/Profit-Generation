"""7.1.2 — LLM filter A/B aggregation.

Validates:
  - aggregate() math on hand-computed fixture (allow/skip/downsize → pnl deltas).
  - Sample-size gate at 50 — verdict_available flips at threshold.
  - fetch_paired_outcomes joins shadow rows to closed outcomes.
  - summary() top-level entrypoint returns overall + per-strategy.
  - Dashboard /api/llm_filter_ab route serves the expected shape.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard import server as srv  # noqa: E402
from data import db  # noqa: E402
from monitoring import llm_filter as llmf  # noqa: E402
from monitoring import llm_filter_ab as lab  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", db_path)
    monkeypatch.setattr(srv.db, "DB_FILE", db_path)
    c = db.init_db(db_path)
    c.close()
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as cl:
        yield cl


def _seed_shadow_and_outcome(conn, *, strategy_id, symbol, bar_ts,
                              verdict, return_pct):
    """Seed signal → outcome → llm_filter shadow row, all joined."""
    db.ensure_strategies_seeded(conn, [{"id": strategy_id}])
    sig_id = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts, signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )
    if sig_id is None:
        return
    db.open_outcome(conn, signal_id=sig_id, entry_ts=bar_ts,
                    entry_price=100.0)
    db.close_outcome(
        conn, signal_id=sig_id, exit_ts=bar_ts,
        exit_price=100.0 * (1.0 + return_pct / 100.0),
        exit_reason="test",
    )
    # Shadow row.
    llmf._ensure_shadow_table(conn)
    with conn:
        conn.execute(
            "INSERT INTO paper_trades_llm_filter "
            "(recorded_at, strategy_id, symbol, bar_ts, signal_type, "
            " side, close, verdict, confidence, rationale, factors_json, "
            " model) VALUES (?, ?, ?, ?, ?, 'long', 100.0, ?, 0.8, 'r', "
            "'[]', 'test-model')",
            ("2026-05-22T10:00:00+00:00", strategy_id, symbol, bar_ts,
             "long_entry", verdict),
        )


# ---------------------------------------------------------------------------
# Aggregate math
# ---------------------------------------------------------------------------

def test_aggregate_allow_only_matches_live():
    rows = [
        {"verdict": "allow", "return_pct": 2.0, "strategy_id": "a"},
        {"verdict": "allow", "return_pct": -1.0, "strategy_id": "a"},
        {"verdict": "allow", "return_pct": 3.0, "strategy_id": "a"},
    ]
    agg = lab.aggregate(rows, min_sample=1)
    assert agg["live_total_pct"] == pytest.approx(4.0)
    assert agg["shadow_total_pct"] == pytest.approx(4.0)
    assert agg["delta_pct"] == pytest.approx(0.0)
    assert agg["verdict_counts"]["allow"] == 3


def test_aggregate_skip_zeros_shadow_pnl():
    rows = [
        {"verdict": "skip", "return_pct": -5.0, "strategy_id": "a"},
        {"verdict": "skip", "return_pct": -3.0, "strategy_id": "a"},
        {"verdict": "allow", "return_pct": 2.0, "strategy_id": "a"},
    ]
    agg = lab.aggregate(rows, min_sample=1)
    # Live: -5 + -3 + 2 = -6
    # Shadow: 0 + 0 + 2 = +2
    assert agg["live_total_pct"] == pytest.approx(-6.0)
    assert agg["shadow_total_pct"] == pytest.approx(2.0)
    assert agg["delta_pct"] == pytest.approx(8.0)
    assert agg["verdict_counts"]["skip"] == 2
    assert agg["verdict_counts"]["allow"] == 1


def test_aggregate_downsize_halves_shadow():
    rows = [
        {"verdict": "downsize", "return_pct": 4.0, "strategy_id": "a"},
        {"verdict": "downsize", "return_pct": -6.0, "strategy_id": "a"},
    ]
    agg = lab.aggregate(rows, min_sample=1)
    # Live: 4 + -6 = -2
    # Shadow: 2 + -3 = -1
    assert agg["live_total_pct"] == pytest.approx(-2.0)
    assert agg["shadow_total_pct"] == pytest.approx(-1.0)
    assert agg["delta_pct"] == pytest.approx(1.0)


def test_sample_size_gate_at_50():
    """Below the threshold → verdict_available False."""
    rows = [
        {"verdict": "allow", "return_pct": 1.0, "strategy_id": "a"}
    ] * 49
    agg = lab.aggregate(rows, min_sample=50)
    assert agg["verdict_available"] is False
    # And exactly at the threshold → True.
    rows.append({"verdict": "allow", "return_pct": 1.0, "strategy_id": "a"})
    agg2 = lab.aggregate(rows, min_sample=50)
    assert agg2["verdict_available"] is True


def test_aggregate_failure_mode_counted():
    rows = [
        {"verdict": "allow", "return_pct": 1.0, "strategy_id": "a",
         "failure_mode": "no_api_key"},
        {"verdict": "allow", "return_pct": 2.0, "strategy_id": "a",
         "failure_mode": None},
    ]
    agg = lab.aggregate(rows, min_sample=1)
    assert agg["verdict_counts"]["fail_open"] == 1


def test_aggregate_empty_inputs():
    agg = lab.aggregate([])
    assert agg["n"] == 0
    assert agg["verdict_available"] is False
    assert agg["live_total_pct"] == 0.0
    assert agg["shadow_total_pct"] == 0.0


# ---------------------------------------------------------------------------
# Win rates
# ---------------------------------------------------------------------------

def test_win_rates_computed_correctly():
    rows = [
        {"verdict": "allow", "return_pct": 1.0, "strategy_id": "a"},
        {"verdict": "allow", "return_pct": -2.0, "strategy_id": "a"},
        {"verdict": "skip", "return_pct": -5.0, "strategy_id": "a"},
        {"verdict": "skip", "return_pct": 3.0, "strategy_id": "a"},
    ]
    agg = lab.aggregate(rows, min_sample=1)
    # Live: 2 wins out of 4 = 0.5
    assert agg["live_win_rate"] == pytest.approx(0.5)
    # Shadow taken: only 2 allow rows (1 win, 1 loss) → 0.5
    assert agg["shadow_taken_n"] == 2
    assert agg["shadow_win_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Sharpe delta
# ---------------------------------------------------------------------------

def test_sharpe_delta_none_when_too_few_samples():
    rows = [{"verdict": "allow", "return_pct": 1.0, "strategy_id": "a"}]
    agg = lab.aggregate(rows, min_sample=1)
    assert agg["sharpe_delta"] is None


def test_sharpe_delta_computed_when_sample_sufficient():
    rows = [
        {"verdict": "skip", "return_pct": -5.0, "strategy_id": "a"},
        {"verdict": "allow", "return_pct": 2.0, "strategy_id": "a"},
        {"verdict": "allow", "return_pct": 3.0, "strategy_id": "a"},
        {"verdict": "skip", "return_pct": -4.0, "strategy_id": "a"},
    ]
    agg = lab.aggregate(rows, min_sample=1)
    # Shadow has lower variance (skip rows count as 0) AND higher mean — Sharpe
    # delta should be positive.
    assert agg["sharpe_delta"] is not None


# ---------------------------------------------------------------------------
# fetch_paired_outcomes — join logic
# ---------------------------------------------------------------------------

def test_fetch_paired_outcomes_returns_only_closed(conn):
    _seed_shadow_and_outcome(
        conn, strategy_id="a", symbol="SPY", bar_ts="2026-05-22",
        verdict="allow", return_pct=2.0,
    )
    _seed_shadow_and_outcome(
        conn, strategy_id="a", symbol="QQQ", bar_ts="2026-05-23",
        verdict="skip", return_pct=-3.0,
    )
    rows = lab.fetch_paired_outcomes(conn)
    assert len(rows) == 2
    verdicts = sorted(r["verdict"] for r in rows)
    assert verdicts == ["allow", "skip"]


def test_fetch_paired_outcomes_strategy_filter(conn):
    _seed_shadow_and_outcome(
        conn, strategy_id="a", symbol="SPY", bar_ts="2026-05-22",
        verdict="allow", return_pct=2.0,
    )
    _seed_shadow_and_outcome(
        conn, strategy_id="b", symbol="QQQ", bar_ts="2026-05-23",
        verdict="skip", return_pct=-3.0,
    )
    rows_a = lab.fetch_paired_outcomes(conn, strategy_id="a")
    assert len(rows_a) == 1
    assert rows_a[0]["strategy_id"] == "a"


# ---------------------------------------------------------------------------
# summary / summary_by_strategy entry points
# ---------------------------------------------------------------------------

def test_summary_returns_overall(conn):
    _seed_shadow_and_outcome(
        conn, strategy_id="a", symbol="SPY", bar_ts="2026-05-22",
        verdict="skip", return_pct=-5.0,
    )
    out = lab.summary(conn, min_sample=1)
    assert out["n"] == 1
    assert out["delta_pct"] == pytest.approx(5.0)


def test_summary_by_strategy_breaks_down(conn):
    _seed_shadow_and_outcome(
        conn, strategy_id="a", symbol="SPY", bar_ts="2026-05-22",
        verdict="skip", return_pct=-5.0,
    )
    _seed_shadow_and_outcome(
        conn, strategy_id="b", symbol="QQQ", bar_ts="2026-05-23",
        verdict="allow", return_pct=2.0,
    )
    out = lab.summary_by_strategy(conn, min_sample=1)
    sids = {r["strategy_id"] for r in out}
    assert sids == {"a", "b"}


# ---------------------------------------------------------------------------
# Dashboard route
# ---------------------------------------------------------------------------

def test_dashboard_llm_filter_ab_route_returns_shape(client):
    r = client.get("/api/llm_filter_ab")
    assert r.status_code == 200
    body = r.get_json()
    assert "overall" in body
    assert "by_strategy" in body
    assert body["overall"]["n"] == 0
    assert body["overall"]["verdict_available"] is False
    assert body["by_strategy"] == []


def test_dashboard_llm_filter_ab_route_renders_with_data(client, tmp_path):
    conn = db.init_db(tmp_path / "trading.db")
    _seed_shadow_and_outcome(
        conn, strategy_id="a", symbol="SPY", bar_ts="2026-05-22",
        verdict="skip", return_pct=-5.0,
    )
    conn.close()
    r = client.get("/api/llm_filter_ab")
    body = r.get_json()
    assert body["overall"]["n"] == 1
    # min_sample is 50, so verdict still unavailable.
    assert body["overall"]["verdict_available"] is False
    assert len(body["by_strategy"]) == 1
