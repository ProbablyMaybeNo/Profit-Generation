"""Tests for scripts/score_live_candidates (milestone 4.1.1).

Live-promotion scorer: ranks every strategy with closed live outcomes
and flags `READY_FOR_LIVE` candidates that cleared all thresholds.
Pure surfacing — never flips a live flag.
"""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# scripts/ isn't a package — load via importlib like test_preflight.
SPEC = importlib.util.spec_from_file_location(
    "score_live_candidates", ROOT / "scripts" / "score_live_candidates.py",
)
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)

from data import db  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_live_outcome(conn, *, strategy_id, return_pct, idx):
    """Seed signal + paper_trades (buy) + closed outcome so the row counts
    as LIVE under score_live_candidates._live_outcomes_by_strategy."""
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    day = (idx % 28) + 1
    month = (idx // 28) + 1
    iso = f"2026-{month:02d}-{day:02d}"
    next_iso = f"2026-{month:02d}-{day:02d}T16:00:00Z"
    sid = db.record_signal(
        conn, strategy_id=strategy_id, symbol="GDX",
        bar_ts=iso, signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": f"order-{strategy_id}-{idx}",
        "signal_id": sid,
        "strategy_id": strategy_id,
        "symbol": "GDX",
        "side": "buy",
        "qty": 1,
        "order_type": "market",
        "submitted_at": iso,
        "filled_at": iso,
        "fill_price": 100.0,
        "status": "filled",
    })
    db.open_outcome(conn, signal_id=sid, entry_ts=iso, entry_price=100.0)
    db.close_outcome(
        conn, signal_id=sid, exit_ts=next_iso,
        exit_price=100.0 * (1.0 + return_pct / 100.0),
        exit_reason="long_exit_signal", bars_held=1,
    )
    return sid


def _seed_strategy_with_returns(conn, strategy_id, returns):
    for i, r in enumerate(returns):
        _seed_live_outcome(conn, strategy_id=strategy_id, return_pct=r, idx=i)


# ---------------------------------------------------------------------------
# _sharpe_ish
# ---------------------------------------------------------------------------

def test_sharpe_ish_empty():
    assert sc._sharpe_ish([]) == 0.0


def test_sharpe_ish_singleton():
    assert sc._sharpe_ish([1.0]) == 0.0


def test_sharpe_ish_zero_stdev():
    assert sc._sharpe_ish([1.0, 1.0, 1.0, 1.0]) == 0.0


def test_sharpe_ish_positive():
    s = sc._sharpe_ish([1.0, 1.5, 0.5, 1.2, 0.8])
    assert s > 0


# ---------------------------------------------------------------------------
# evaluate_strategy — scoring math + threshold gating
# ---------------------------------------------------------------------------

def test_evaluate_score_formula():
    """score == mean × sqrt(n) × sharpe — exact, not approximate."""
    rets = [1.0, 1.5, 0.5, 1.2, 0.8, 1.1, 0.9, 1.3]
    n = len(rets)
    mean = sum(rets) / n
    sharpe = sc._sharpe_ish(rets)
    expected = mean * math.sqrt(n) * sharpe

    row = sc.evaluate_strategy(
        "s", rets, already_live=False,
        min_outcomes=1, min_sharpe=-1.0, min_mean_ret=-1.0,
    )
    assert row["score"] == pytest.approx(round(expected, 4))


def test_evaluate_zero_returns():
    row = sc.evaluate_strategy("empty", [], already_live=False)
    assert row["n"] == 0
    assert row["score"] == 0.0
    assert row["verdict"] == sc.SKIP_THIN


def test_evaluate_ready_for_live_when_all_thresholds_clear():
    rets = [0.8] * 60  # n=60, mean=+0.8%, sharpe=very high (zero std)
    # Force a non-zero stdev so sharpe is real but still > 0.4.
    rets = [0.8 + (0.05 * ((i % 5) - 2)) for i in range(60)]
    row = sc.evaluate_strategy(
        "winner", rets, already_live=False,
    )
    assert row["verdict"] == sc.READY_TAG
    assert row["n"] == 60
    assert row["mean_ret_pct"] > 0


def test_evaluate_skips_when_already_live():
    rets = [0.8 + (0.05 * ((i % 5) - 2)) for i in range(60)]
    row = sc.evaluate_strategy(
        "winner", rets, already_live=True,
    )
    assert row["verdict"] == sc.SKIP_ALREADY_LIVE
    # Still computes the numbers so they show in the table.
    assert row["n"] == 60


def test_evaluate_skips_below_min_outcomes():
    rets = [1.0] * 30  # too few
    row = sc.evaluate_strategy(
        "thin", rets, already_live=False, min_outcomes=50,
    )
    assert row["verdict"] == sc.SKIP_THIN
    assert "30" in row["reason"]


def test_evaluate_skips_negative_mean():
    rets = [-0.1 + (0.01 * ((i % 3) - 1)) for i in range(60)]
    row = sc.evaluate_strategy(
        "loser", rets, already_live=False,
    )
    assert row["verdict"] == sc.SKIP_NEGATIVE


def test_evaluate_skips_low_sharpe():
    # Mean clearly positive (~+0.5%) but stdev huge → sharpe tiny.
    rets = ([5.0, -4.0] * 30)  # n=60, mean=+0.5, sharpe ~ small
    row = sc.evaluate_strategy(
        "noisy", rets, already_live=False, min_sharpe=0.4,
    )
    assert row["verdict"] == sc.SKIP_LOW_SHARPE


# ---------------------------------------------------------------------------
# score_candidates — DB integration + sort + dedupe-against-live
# ---------------------------------------------------------------------------

def test_score_candidates_no_outcomes(isolated_db):
    conn = db.init_db()
    try:
        rows = sc.score_candidates(conn, settings={"auto_trade": {}})
        assert rows == []
    finally:
        conn.close()


def test_score_candidates_sorted_by_score(isolated_db):
    conn = db.init_db()
    try:
        # winner: high mean, low noise → high score
        _seed_strategy_with_returns(
            conn, "winner",
            [0.5 + (0.05 * ((i % 5) - 2)) for i in range(55)],
        )
        # mid: smaller mean
        _seed_strategy_with_returns(
            conn, "mid",
            [0.1 + (0.05 * ((i % 5) - 2)) for i in range(55)],
        )
        rows = sc.score_candidates(conn, settings={"auto_trade": {}})
        assert len(rows) == 2
        assert rows[0]["strategy_id"] == "winner"
        assert rows[1]["strategy_id"] == "mid"
        assert rows[0]["score"] > rows[1]["score"]
    finally:
        conn.close()


def test_score_candidates_dedupes_already_live(isolated_db):
    conn = db.init_db()
    try:
        _seed_strategy_with_returns(
            conn, "winner",
            [0.5 + (0.05 * ((i % 5) - 2)) for i in range(55)],
        )
        settings = {"auto_trade": {"live_strategies": ["winner"]}}
        rows = sc.score_candidates(conn, settings=settings)
        assert len(rows) == 1
        assert rows[0]["verdict"] == sc.SKIP_ALREADY_LIVE
    finally:
        conn.close()


def test_score_candidates_flags_ready_for_live(isolated_db):
    conn = db.init_db()
    try:
        _seed_strategy_with_returns(
            conn, "winner",
            [0.5 + (0.05 * ((i % 5) - 2)) for i in range(55)],
        )
        rows = sc.score_candidates(conn, settings={"auto_trade": {}})
        assert rows[0]["verdict"] == sc.READY_TAG
    finally:
        conn.close()


def test_score_candidates_respects_threshold_args(isolated_db):
    conn = db.init_db()
    try:
        _seed_strategy_with_returns(
            conn, "winner",
            [0.5 + (0.05 * ((i % 5) - 2)) for i in range(55)],
        )
        # Crank sharpe threshold way up — should now skip.
        rows = sc.score_candidates(
            conn, settings={"auto_trade": {}},
            min_sharpe=100.0,
        )
        assert rows[0]["verdict"] == sc.SKIP_LOW_SHARPE
    finally:
        conn.close()


def test_score_candidates_excludes_pure_validator_runs(isolated_db):
    """A signal with no paper_trades row must NOT count as live."""
    conn = db.init_db()
    try:
        db.upsert_strategy(conn, {"extra": {"strategy_id": "bt_only"}})
        for i in range(55):
            day = (i % 28) + 1
            month = (i // 28) + 1
            iso = f"2026-{month:02d}-{day:02d}"
            sid = db.record_signal(
                conn, strategy_id="bt_only", symbol="GDX",
                bar_ts=iso, signal_type="long_entry",
                close=100.0, bar_interval="1d",
            )
            assert sid is not None, "duplicate bar_ts collision in fixture"
            db.open_outcome(conn, signal_id=sid, entry_ts=iso,
                             entry_price=100.0)
            db.close_outcome(
                conn, signal_id=sid, exit_ts=f"{iso}T16:00:00Z",
                exit_price=101.0, exit_reason="long_exit_signal",
                bars_held=1,
            )
        rows = sc.score_candidates(conn, settings={"auto_trade": {}})
        assert rows == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# format_report + render_markdown
# ---------------------------------------------------------------------------

def test_format_report_empty():
    out = sc.format_report([])
    assert "no candidates" in out.lower()


def test_format_report_includes_threshold_summary():
    rows = [{
        "strategy_id": "s",
        "n": 60,
        "mean_ret_pct": 0.5,
        "sharpe": 0.5,
        "score": 1.0,
        "verdict": sc.READY_TAG,
        "reason": "ok",
        "already_live": False,
    }]
    out = sc.format_report(rows)
    assert "READY_FOR_LIVE" in out
    assert "Thresholds" in out
    assert "human flip" in out


def test_render_markdown_table_shape():
    rows = [{
        "strategy_id": "s",
        "n": 60,
        "mean_ret_pct": 0.5,
        "sharpe": 0.5,
        "score": 1.0,
        "verdict": sc.READY_TAG,
        "reason": "ok",
        "already_live": False,
    }]
    md = sc.render_markdown(rows)
    assert "| strategy |" in md
    assert "| `s` |" in md
    assert "READY_FOR_LIVE" in md
