"""Tests for monitoring.live_vs_backtest (milestone 3.6.2).

Weekly divergence report: for each strategy with >= 1 closed live outcome
in the trailing window, compare live mean return vs the strategy's
backtest mean return. Flag at < 50% of backtest.
"""

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import live_vs_backtest as lvb  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_strategy_with_backtest(conn, strategy_id, *, mean_pct):
    """Seed a strategies row with a single test_run whose per-trade mean
    equals `mean_pct` (trades=100, total = mean*100)."""
    record = {
        "extra": {
            "strategy_id": strategy_id,
            "test_runs": [
                {"instrument": "GDX", "trades": 100,
                 "total_return_pct": mean_pct * 100,
                 "verdict": "PASS"},
            ],
        },
    }
    db.upsert_strategy(conn, record)


def _seed_live_outcome(conn, *, strategy_id, symbol, entry_iso, exit_iso,
                      return_pct, alpaca_order_id):
    """Seed signal + paper_trades (buy) + outcome (closed) so the row is
    counted as a LIVE outcome by live_vs_backtest._live_outcomes_in_window."""
    sid = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=entry_iso, signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": alpaca_order_id,
        "signal_id": sid,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": "buy",
        "qty": 10,
        "order_type": "market",
        "submitted_at": entry_iso,
        "filled_at": entry_iso,
        "fill_price": 100.0,
        "status": "filled",
    })
    db.open_outcome(conn, signal_id=sid, entry_ts=entry_iso,
                    entry_price=100.0)
    # We pass return_pct directly via the price diff so close_outcome
    # recomputes it consistently. exit_price = 100 * (1 + return_pct/100)
    exit_price = 100.0 * (1.0 + return_pct / 100.0)
    db.close_outcome(
        conn, signal_id=sid, exit_ts=exit_iso,
        exit_price=exit_price, exit_reason="long_exit_signal", bars_held=1,
    )
    return sid


# ---------------------------------------------------------------------------
# _classify
# ---------------------------------------------------------------------------

def test_classify_warn_below_50pct():
    assert lvb._classify(1.0, 0.49) == "warn"


def test_classify_watch_between_50_and_80pct():
    assert lvb._classify(1.0, 0.50) == "watch"
    assert lvb._classify(1.0, 0.79) == "watch"


def test_classify_ok_at_or_above_80pct():
    assert lvb._classify(1.0, 0.80) == "ok"
    assert lvb._classify(1.0, 1.5) == "ok"


def test_classify_info_when_no_backtest_baseline():
    assert lvb._classify(None, None) == "info"
    assert lvb._classify(0.0, None) == "info"
    assert lvb._classify(-0.5, None) == "info"


# ---------------------------------------------------------------------------
# compute_divergence
# ---------------------------------------------------------------------------

def test_compute_divergence_empty_window(isolated_db):
    conn = db.init_db()
    try:
        rollup = lvb.compute_divergence(conn, asof=date(2026, 5, 17),
                                         window_days=7)
        assert rollup["window_start"] == "2026-05-10"
        assert rollup["window_end"] == "2026-05-17"
        assert rollup["rows"] == []
        assert rollup["n_strategies"] == 0
        assert rollup["n_warn"] == 0
        assert rollup["n_trades_total"] == 0
    finally:
        conn.close()


def test_compute_divergence_flags_warn_below_half(isolated_db):
    """Backtest 1%, live mean 0.3% → ratio 0.3 → warn."""
    conn = db.init_db()
    try:
        _seed_strategy_with_backtest(conn, "alpha", mean_pct=1.0)
        for i, ret in enumerate([0.2, 0.4, 0.3]):
            _seed_live_outcome(
                conn, strategy_id="alpha", symbol="GDX",
                entry_iso=f"2026-05-1{2 + i}", exit_iso=f"2026-05-1{3 + i}",
                return_pct=ret, alpaca_order_id=f"a{i}",
            )
        rollup = lvb.compute_divergence(conn, asof=date(2026, 5, 17),
                                         window_days=7)
        assert rollup["n_strategies"] == 1
        assert rollup["n_warn"] == 1
        row = rollup["rows"][0]
        assert row["strategy_id"] == "alpha"
        assert row["n_live"] == 3
        assert row["live_mean_pct"] == pytest.approx(0.3, abs=0.01)
        assert row["backtest_mean_pct"] == pytest.approx(1.0)
        assert row["ratio"] == pytest.approx(0.3, abs=0.01)
        assert row["flag"] == "warn"
    finally:
        conn.close()


def test_compute_divergence_ok_when_tracking(isolated_db):
    """Backtest 1%, live mean 0.9% → ratio 0.9 → ok."""
    conn = db.init_db()
    try:
        _seed_strategy_with_backtest(conn, "ok_strat", mean_pct=1.0)
        _seed_live_outcome(
            conn, strategy_id="ok_strat", symbol="GDX",
            entry_iso="2026-05-13", exit_iso="2026-05-14",
            return_pct=0.9, alpaca_order_id="o1",
        )
        rollup = lvb.compute_divergence(conn, asof=date(2026, 5, 17),
                                         window_days=7)
        assert rollup["n_warn"] == 0
        assert rollup["rows"][0]["flag"] == "ok"
    finally:
        conn.close()


def test_compute_divergence_watch_band(isolated_db):
    """Live 0.6% vs backtest 1% → ratio 0.6 → watch."""
    conn = db.init_db()
    try:
        _seed_strategy_with_backtest(conn, "watch_strat", mean_pct=1.0)
        _seed_live_outcome(
            conn, strategy_id="watch_strat", symbol="GDX",
            entry_iso="2026-05-13", exit_iso="2026-05-14",
            return_pct=0.6, alpaca_order_id="w1",
        )
        rollup = lvb.compute_divergence(conn, asof=date(2026, 5, 17),
                                         window_days=7)
        assert rollup["rows"][0]["flag"] == "watch"
        assert rollup["n_warn"] == 0
    finally:
        conn.close()


def test_compute_divergence_info_when_no_backtest_baseline(isolated_db):
    """Strategy has live outcomes but no test_runs → flag=info, no warn."""
    conn = db.init_db()
    try:
        # No upsert_strategy → no row at all. record_signal will fail FK
        # if FKs were ON, but they're not enforced in init_db by default.
        # Use upsert_strategy with empty test_runs instead.
        db.upsert_strategy(conn, {"extra": {"strategy_id": "no_bt",
                                            "test_runs": []}})
        _seed_live_outcome(
            conn, strategy_id="no_bt", symbol="GDX",
            entry_iso="2026-05-13", exit_iso="2026-05-14",
            return_pct=0.5, alpaca_order_id="x1",
        )
        rollup = lvb.compute_divergence(conn, asof=date(2026, 5, 17),
                                         window_days=7)
        assert rollup["rows"][0]["flag"] == "info"
        assert rollup["rows"][0]["backtest_mean_pct"] is None
        assert rollup["rows"][0]["ratio"] is None
        assert rollup["n_warn"] == 0
    finally:
        conn.close()


def test_compute_divergence_excludes_outside_window(isolated_db):
    """Outcomes whose exit_ts is outside the window must be excluded."""
    conn = db.init_db()
    try:
        _seed_strategy_with_backtest(conn, "alpha", mean_pct=1.0)
        # In-window
        _seed_live_outcome(
            conn, strategy_id="alpha", symbol="GDX",
            entry_iso="2026-05-15", exit_iso="2026-05-16",
            return_pct=0.5, alpaca_order_id="in1",
        )
        # Out-of-window (3 weeks earlier)
        _seed_live_outcome(
            conn, strategy_id="alpha", symbol="GDX",
            entry_iso="2026-04-20", exit_iso="2026-04-21",
            return_pct=2.0, alpaca_order_id="out1",
        )
        rollup = lvb.compute_divergence(conn, asof=date(2026, 5, 17),
                                         window_days=7)
        # Only the in-window outcome counted: mean = 0.5 (not 1.25 if both)
        assert rollup["rows"][0]["n_live"] == 1
        assert rollup["rows"][0]["live_mean_pct"] == pytest.approx(0.5)
    finally:
        conn.close()


def test_compute_divergence_excludes_backtest_only_outcomes(isolated_db):
    """Outcomes WITHOUT a paper_trades buy row must not count (those are
    pure backtest outcomes, not live)."""
    conn = db.init_db()
    try:
        _seed_strategy_with_backtest(conn, "alpha", mean_pct=1.0)
        # Signal + closed outcome but NO paper_trades row → exclude.
        sid = db.record_signal(
            conn, strategy_id="alpha", symbol="GDX",
            bar_ts="2026-05-13", signal_type="long_entry",
            close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts="2026-05-13",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts="2026-05-14",
            exit_price=102.0, exit_reason="long_exit_signal", bars_held=1,
        )
        rollup = lvb.compute_divergence(conn, asof=date(2026, 5, 17),
                                         window_days=7)
        assert rollup["rows"] == []
        assert rollup["n_trades_total"] == 0
    finally:
        conn.close()


def test_compute_divergence_sorts_warn_first(isolated_db):
    """Multiple strategies in one window: warn rows before ok rows;
    inside warn the lowest ratio wins the top slot."""
    conn = db.init_db()
    try:
        # warn (ratio 0.2)
        _seed_strategy_with_backtest(conn, "worst", mean_pct=1.0)
        _seed_live_outcome(conn, strategy_id="worst", symbol="GDX",
                            entry_iso="2026-05-13", exit_iso="2026-05-14",
                            return_pct=0.2, alpaca_order_id="w1")
        # warn (ratio 0.4) — between worst and bad
        _seed_strategy_with_backtest(conn, "bad", mean_pct=1.0)
        _seed_live_outcome(conn, strategy_id="bad", symbol="GDX",
                            entry_iso="2026-05-13", exit_iso="2026-05-14",
                            return_pct=0.4, alpaca_order_id="b1")
        # ok (ratio 0.9)
        _seed_strategy_with_backtest(conn, "good", mean_pct=1.0)
        _seed_live_outcome(conn, strategy_id="good", symbol="GDX",
                            entry_iso="2026-05-13", exit_iso="2026-05-14",
                            return_pct=0.9, alpaca_order_id="g1")
        rollup = lvb.compute_divergence(conn, asof=date(2026, 5, 17),
                                         window_days=7)
        assert [r["strategy_id"] for r in rollup["rows"]] == \
            ["worst", "bad", "good"]
        assert rollup["n_warn"] == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------

def test_render_markdown_empty_window():
    rollup = {
        "window_start": "2026-05-10",
        "window_end": "2026-05-17",
        "asof_iso": "2026-05-17T18:00:00+00:00",
        "rows": [],
        "n_strategies": 0,
        "n_warn": 0,
        "n_trades_total": 0,
    }
    md = lvb.render_markdown(rollup)
    assert "Live-vs-Backtest" in md
    assert "No closed live outcomes" in md


def test_render_markdown_warn_section_present():
    rollup = {
        "window_start": "2026-05-10",
        "window_end": "2026-05-17",
        "asof_iso": "2026-05-17T18:00:00+00:00",
        "rows": [
            {"strategy_id": "alpha", "n_live": 3, "live_mean_pct": 0.3,
             "backtest_mean_pct": 1.0, "ratio": 0.3, "flag": "warn"},
        ],
        "n_strategies": 1,
        "n_warn": 1,
        "n_trades_total": 3,
    }
    md = lvb.render_markdown(rollup)
    assert "⚠️" in md
    assert "1 strategy(ies) running below 50%" in md
    assert "`alpha`" in md
    assert "warn" in md
    assert "0.30" in md or "0.3" in md  # ratio cell


def test_render_markdown_includes_thresholds_legend():
    rollup = {
        "window_start": "2026-05-10",
        "window_end": "2026-05-17",
        "asof_iso": "2026-05-17T18:00:00+00:00",
        "rows": [
            {"strategy_id": "alpha", "n_live": 1, "live_mean_pct": 0.9,
             "backtest_mean_pct": 1.0, "ratio": 0.9, "flag": "ok"},
        ],
        "n_strategies": 1,
        "n_warn": 0,
        "n_trades_total": 1,
    }
    md = lvb.render_markdown(rollup)
    assert "Thresholds" in md
    assert "0.50" in md  # warn threshold
    assert "0.80" in md  # watch threshold


# ---------------------------------------------------------------------------
# build_report (integration with build_report convenience wrapper)
# ---------------------------------------------------------------------------

def test_build_report_round_trip(isolated_db):
    """build_report → compute + render in one call, no posting."""
    conn = db.init_db()
    try:
        _seed_strategy_with_backtest(conn, "alpha", mean_pct=1.0)
        _seed_live_outcome(conn, strategy_id="alpha", symbol="GDX",
                            entry_iso="2026-05-13", exit_iso="2026-05-14",
                            return_pct=0.3, alpaca_order_id="o1")
    finally:
        conn.close()
    out = lvb.build_report(asof=date(2026, 5, 17), window_days=7)
    assert out["rollup"]["n_strategies"] == 1
    assert out["rollup"]["n_warn"] == 1
    assert "alpha" in out["markdown"]


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def test_cli_dry_run_prints_markdown(isolated_db, monkeypatch, capsys):
    """--dry-run prints markdown to stdout, never touches Notion."""
    conn = db.init_db()
    try:
        _seed_strategy_with_backtest(conn, "alpha", mean_pct=1.0)
        _seed_live_outcome(conn, strategy_id="alpha", symbol="GDX",
                            entry_iso="2026-05-13", exit_iso="2026-05-14",
                            return_pct=0.4, alpaca_order_id="o1")
    finally:
        conn.close()
    monkeypatch.setattr(sys, "argv",
                        ["live_vs_backtest.py", "--asof", "2026-05-17",
                         "--dry-run"])
    lvb.main()
    out = capsys.readouterr().out
    assert "Live-vs-Backtest" in out
    assert "alpha" in out
