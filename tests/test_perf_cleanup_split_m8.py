"""test_perf_cleanup_split_m8.py — Sprint 3 / M8: separate performance from cleanup.

Outcomes closed by a reconcile/orphan/stale sweep (reconciled_no_position,
stale_intraday_flatten_missed, ...) are CLEANUP bookkeeping — they record
whatever mark was last available, not the strategy's edge. Counting them in
expectancy/win-rate/Sharpe poisons every gate that decides whether a strategy
keeps trading.

M8: strategy stats (eligibility gate + strategy_health) are computed over FRESH
closes only; the report shows a fresh-vs-cleanup split. These tests drive the
REAL eligibility gate (auto_trader._is_eligible) and strategy_health helpers and
prove cleanup closes are excluded. FAILS on pre-M8 code, which counted them.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


def _close(conn, sid, sym, ret_pct, reason, *, i, interval="1d"):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    s = db.record_signal(conn, strategy_id=sid, symbol=sym,
                         bar_ts=f"2026-05-{i:02d}", signal_type="long_entry",
                         close=100.0, bar_interval=interval)
    db.open_outcome(conn, signal_id=s, entry_ts=f"2026-05-{i:02d}",
                    entry_price=100.0)
    db.close_outcome(conn, signal_id=s, exit_ts=f"2026-05-{i+1:02d}",
                     exit_price=100.0 * (1 + ret_pct / 100),
                     exit_reason=reason, bars_held=1)
    return s


def test_eligibility_ignores_cleanup_closes(conn):
    """A strategy with 2 fresh losers + 10 fake-positive cleanup closes must be
    judged on the 2 fresh losers (negative edge), NOT the cleanup-inflated set.
    Pre-M8 the 10 cleanup closes dragged n up and the mean positive."""
    sid = "trend-donchian-breakout-20"
    # 2 fresh losing trades.
    _close(conn, sid, "AES", -3.0, "long_exit_signal", i=1)
    _close(conn, sid, "AES", -2.0, "long_exit_signal", i=3)
    # 10 cleanup closes that look like 0% flats (reconcile bookkeeping).
    for k in range(10):
        _close(conn, sid, "QQQ", 0.0, "reconciled_no_position", i=5 + k)

    settings = {"min_outcomes": 5, "min_mean_ret_pct": 0.0,
                "min_sharpe_ish": 0.0}
    ok, stats = at._is_eligible(conn, sid, settings)
    # FRESH n is only 2 -> below min_outcomes(5) -> ineligible (no grace).
    assert stats["n"] == 2, f"stats counted cleanup closes: n={stats['n']}"
    assert ok is False


def test_strategy_health_returns_fresh_only(conn):
    """closed_returns_in_class excludes cleanup closes."""
    sid = "intraday-orbo-5m"
    _close(conn, sid, "NVDA", 1.0, "long_exit_signal", i=1, interval="5m")
    _close(conn, sid, "NVDA", -1.0, "eod_close", i=3, interval="5m")
    _close(conn, sid, "NVDA", 0.0, "stale_intraday_flatten_missed", i=5,
           interval="5m")
    _close(conn, sid, "NVDA", 0.0, "reconciled_no_position", i=7, interval="5m")

    rets = sh.closed_returns_in_class(conn, sid, bar_interval="5m")
    assert sorted(rets) == [-1.0, 1.0], (
        f"cleanup closes leaked into health stats: {rets}")


def test_live_divergence_returns_fresh_only(conn):
    """live_returns_for_strategy (auto-pause input) excludes cleanup closes."""
    sid = "trend-donchian-breakout-20"
    # fresh live close (paper_trades-backed)
    s1 = _close(conn, sid, "SPY", 2.0, "long_exit_signal", i=1)
    s2 = _close(conn, sid, "SPY", 0.0, "reconciled_no_position", i=3)
    for sx in (s1, s2):
        conn.execute(
            "INSERT INTO paper_trades "
            "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
            " status, submitted_at) VALUES (?, ?, ?, 'SPY', 'buy', 1, "
            " 'filled', '2026-05-01')",
            (f"b-{sx}", sx, sid),
        )
    conn.commit()
    rets = sh._live_outcomes_for_strategy(conn, sid, limit=50)
    assert rets == [2.0], f"cleanup close leaked into live-divergence: {rets}"
