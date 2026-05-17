"""Tests for strategy auto-pause on live divergence (milestone 3.3.4)."""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_strategy_with_backtest(conn, *, strategy_id, mean_per_trade_pct):
    """Insert a strategies row whose raw_record_json carries a single
    validator test_run with the requested mean per-trade return."""
    # Use 100 trades so total_return_pct = mean * 100 (no floating-point grief).
    record = {
        "extra": {
            "strategy_id": strategy_id,
            "test_runs": [{
                "test_id": f"{strategy_id}-A",
                "trades": 100,
                "total_return_pct": mean_per_trade_pct * 100,
                "verdict": "PASS",
            }],
        },
    }
    db.upsert_strategy(conn, record)


def _seed_live_outcomes(conn, *, strategy_id, returns, base_day=1):
    """For each return, insert a signal + open+close outcome + paper_trades
    buy. That makes the outcome 'live' for the divergence check. Does NOT
    upsert the strategy row — caller must ensure it exists (typically via
    _seed_strategy_with_backtest so the backtest mean is preserved)."""
    # Strategy may already exist with a backtest. Insert a stub iff missing.
    existing = conn.execute(
        "SELECT 1 FROM strategies WHERE strategy_id=?", (strategy_id,),
    ).fetchone()
    if existing is None:
        db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    for i, ret in enumerate(returns):
        day = base_day + i
        iso = f"2026-04-{day:02d}" if day <= 30 else f"2026-05-{day-30:02d}"
        next_iso = (f"2026-04-{day+1:02d}" if day < 30
                    else f"2026-05-{day-29:02d}")
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
            bar_ts=iso, signal_type="long_entry",
            close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=iso, entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=next_iso,
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )
        db.record_paper_trade(conn, {
            "alpaca_order_id": f"buy-{strategy_id}-{i}",
            "signal_id": sid, "strategy_id": strategy_id, "symbol": "X",
            "side": "buy", "qty": 10, "order_type": "market",
            "submitted_at": f"{iso}T13:30:00Z", "status": "filled",
            "fill_price": 100.0,
        })


def _winner_settings(**overrides):
    s = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 1, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.0,
        "max_position_usd": 1000,
    }
    s.update(overrides)
    return s


# ---------- schema ----------

def test_paused_strategies_table_exists(isolated_db):
    conn = db.init_db()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paused_strategies'"
    ).fetchone()
    assert row is not None


# ---------- backtest_mean_return_pct ----------

def test_backtest_mean_none_for_unknown_strategy(isolated_db):
    conn = db.init_db()
    assert sh.backtest_mean_return_pct(conn, "missing") is None


def test_backtest_mean_none_when_no_test_runs(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    assert sh.backtest_mean_return_pct(conn, "s1") is None


def test_backtest_mean_computed_from_test_runs(isolated_db):
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="s1",
                                  mean_per_trade_pct=0.75)
    assert sh.backtest_mean_return_pct(conn, "s1") == pytest.approx(0.75)


def test_backtest_mean_averages_multiple_runs(isolated_db):
    conn = db.init_db()
    record = {
        "extra": {
            "strategy_id": "s1",
            "test_runs": [
                {"trades": 10, "total_return_pct": 10.0},   # 1.0%/trade
                {"trades": 20, "total_return_pct": 10.0},   # 0.5%/trade
            ],
        },
    }
    db.upsert_strategy(conn, record)
    # Average of 1.0 and 0.5 = 0.75
    assert sh.backtest_mean_return_pct(conn, "s1") == pytest.approx(0.75)


def test_backtest_mean_ignores_zero_trade_runs(isolated_db):
    conn = db.init_db()
    record = {
        "extra": {
            "strategy_id": "s1",
            "test_runs": [
                {"trades": 0, "total_return_pct": 999.0},   # ignored
                {"trades": 100, "total_return_pct": 50.0},  # 0.5%/trade
            ],
        },
    }
    db.upsert_strategy(conn, record)
    assert sh.backtest_mean_return_pct(conn, "s1") == pytest.approx(0.5)


# ---------- evaluate_live_divergence ----------

def test_evaluate_says_not_enough_live_outcomes(isolated_db):
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    _seed_live_outcomes(conn, strategy_id="winner",
                         returns=[0.1] * 5)
    result = sh.evaluate_live_divergence(conn, "winner")
    assert result["should_pause"] is False
    assert result["n_live"] == 5
    assert "5 live outcomes" in result["reason"]


def test_evaluate_pauses_when_live_below_30pct_of_backtest(isolated_db):
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    # 20 live trades each at 0.1% → mean 0.1% which is 10% of backtest 1.0%.
    _seed_live_outcomes(conn, strategy_id="winner", returns=[0.1] * 20)
    result = sh.evaluate_live_divergence(conn, "winner")
    assert result["n_live"] == 20
    assert result["live_mean_pct"] == pytest.approx(0.1)
    assert result["backtest_mean_pct"] == pytest.approx(1.0)
    assert result["ratio"] == pytest.approx(0.1)
    assert result["should_pause"] is True


def test_evaluate_does_not_pause_when_live_above_threshold(isolated_db):
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    # 20 live trades at 0.5% → 50% of backtest, well above 30% threshold.
    _seed_live_outcomes(conn, strategy_id="winner", returns=[0.5] * 20)
    result = sh.evaluate_live_divergence(conn, "winner")
    assert result["should_pause"] is False
    assert result["ratio"] == pytest.approx(0.5)


def test_evaluate_skips_when_backtest_mean_non_positive(isolated_db):
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="bad",
                                  mean_per_trade_pct=-0.5)
    _seed_live_outcomes(conn, strategy_id="bad", returns=[-2.0] * 20)
    result = sh.evaluate_live_divergence(conn, "bad")
    assert result["should_pause"] is False
    assert "non-positive" in result["reason"]


def test_evaluate_skips_when_no_backtest_record(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "no-bt"}})
    _seed_live_outcomes(conn, strategy_id="no-bt", returns=[0.0] * 20)
    result = sh.evaluate_live_divergence(conn, "no-bt")
    assert result["should_pause"] is False
    assert "no backtest mean" in result["reason"]


def test_evaluate_ignores_non_paper_outcomes(isolated_db):
    """Outcomes without a paper_trades.buy row are NOT 'live' and must
    be excluded from the divergence calc."""
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    # 20 'backtest-only' outcomes: signal + outcome rows, but no paper_trades.
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    for i in range(20):
        day = i + 1
        iso = f"2026-03-{day:02d}" if day <= 30 else f"2026-04-{day-30:02d}"
        next_iso = (f"2026-03-{day+1:02d}" if day < 30
                    else f"2026-04-{day-29:02d}")
        sid = db.record_signal(
            conn, strategy_id="winner", symbol="X",
            bar_ts=iso, signal_type="long_entry",
            close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=iso, entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=next_iso,
            exit_price=99.99, exit_reason="long_exit_signal", bars_held=1,
        )
    result = sh.evaluate_live_divergence(conn, "winner")
    assert result["n_live"] == 0
    assert result["should_pause"] is False


# ---------- pause / unpause / is_paused ----------

def test_pause_strategy_inserts_row(isolated_db):
    conn = db.init_db()
    res = sh.pause_strategy(
        conn, "winner", reason="manual pause",
        source="manual", pause_days=None,
    )
    assert res["expires_at"] is None
    row = conn.execute(
        "SELECT * FROM paused_strategies WHERE strategy_id=?", ("winner",)
    ).fetchone()
    assert row is not None
    assert row["source"] == "manual"


def test_is_paused_false_when_no_row(isolated_db):
    conn = db.init_db()
    assert sh.is_paused(conn, "winner") is False


def test_is_paused_true_indefinite(isolated_db):
    conn = db.init_db()
    sh.pause_strategy(conn, "winner", reason="r", pause_days=None)
    assert sh.is_paused(conn, "winner") is True


def test_is_paused_true_when_within_expiry(isolated_db):
    conn = db.init_db()
    sh.pause_strategy(conn, "winner", reason="r", pause_days=30)
    assert sh.is_paused(conn, "winner") is True


def test_is_paused_false_when_expired(isolated_db):
    conn = db.init_db()
    # Pause for 30 days as of an OLD date — far in the past.
    sh.pause_strategy(conn, "winner", reason="r", pause_days=30,
                       now_iso="2025-01-01T00:00:00+00:00")
    asof = "2026-05-16T00:00:00+00:00"
    assert sh.is_paused(conn, "winner", asof_iso=asof) is False


def test_unpause_removes_row(isolated_db):
    conn = db.init_db()
    sh.pause_strategy(conn, "winner", reason="r", pause_days=None)
    assert sh.unpause_strategy(conn, "winner") is True
    assert sh.is_paused(conn, "winner") is False


def test_unpause_returns_false_when_no_row(isolated_db):
    conn = db.init_db()
    assert sh.unpause_strategy(conn, "winner") is False


def test_pause_upsert_overwrites_prior_row(isolated_db):
    conn = db.init_db()
    sh.pause_strategy(conn, "winner", reason="first", pause_days=30)
    sh.pause_strategy(conn, "winner", reason="second", pause_days=60)
    row = conn.execute(
        "SELECT reason FROM paused_strategies WHERE strategy_id=?",
        ("winner",),
    ).fetchone()
    assert row["reason"] == "second"


def test_list_paused_excludes_expired_by_default(isolated_db):
    conn = db.init_db()
    sh.pause_strategy(conn, "expired", reason="r", pause_days=30,
                       now_iso="2025-01-01T00:00:00+00:00")
    sh.pause_strategy(conn, "active", reason="r", pause_days=None)
    rows = sh.list_paused(conn)
    sids = [r["strategy_id"] for r in rows]
    assert "active" in sids
    assert "expired" not in sids


def test_list_paused_include_expired(isolated_db):
    conn = db.init_db()
    sh.pause_strategy(conn, "expired", reason="r", pause_days=30,
                       now_iso="2025-01-01T00:00:00+00:00")
    rows = sh.list_paused(conn, include_expired=True)
    assert any(r["strategy_id"] == "expired" for r in rows)


# ---------- auto_pause_check ----------

def test_auto_pause_check_pauses_diverged_strategy(isolated_db):
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    _seed_live_outcomes(conn, strategy_id="winner", returns=[0.1] * 20)
    alerts = []
    fired = sh.auto_pause_check(
        conn, send_fn=lambda txt: (alerts.append(txt), True)[1],
    )
    assert len(fired) == 1
    assert fired[0]["strategy_id"] == "winner"
    assert fired[0]["action"] == "PAUSED"
    assert sh.is_paused(conn, "winner") is True
    assert len(alerts) == 1
    assert "winner" in alerts[0]


def test_auto_pause_check_silent_when_no_divergence(isolated_db):
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    _seed_live_outcomes(conn, strategy_id="winner", returns=[0.6] * 20)
    fired = sh.auto_pause_check(conn, send_fn=lambda txt: True)
    assert fired == []
    assert sh.is_paused(conn, "winner") is False


def test_auto_pause_check_skips_already_paused(isolated_db):
    """An already-paused strategy should NOT re-fire an alert in the same
    scan even if the live numbers are still below threshold."""
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    _seed_live_outcomes(conn, strategy_id="winner", returns=[0.1] * 20)
    sh.pause_strategy(conn, "winner", reason="prior", pause_days=None)
    alerts = []
    fired = sh.auto_pause_check(
        conn, send_fn=lambda txt: alerts.append(txt) or True,
    )
    assert fired == []
    assert alerts == []


# ---------- auto_trader integration ----------

def test_auto_trader_skips_entries_on_paused_strategy(isolated_db,
                                                       monkeypatch):
    conn = db.init_db()
    # Seed eligibility (5 winners) AND pause the strategy.
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    for i in range(5):
        sid = db.record_signal(
            conn, strategy_id="winner", symbol="W",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=102.0, exit_reason="long_exit_signal", bars_held=1,
        )
    sh.pause_strategy(conn, "winner", reason="diverged", pause_days=30)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_winner_settings())
    actions = res["actions"]
    paused_skips = [a for a in actions
                    if a.get("action") == "SKIP_PAUSED_STRATEGY"]
    assert len(paused_skips) == 1
    assert paused_skips[0]["reason"] == "diverged"
    assert paused_skips[0]["strategy_id"] == "winner"
    # No DRY_BUY allowed for the paused strategy.
    buys = [a for a in actions if a.get("action") == "DRY_BUY"]
    assert len(buys) == 0


def test_auto_trader_processes_exits_for_paused_strategy(isolated_db):
    """A pause must NOT block exits — an open position still needs to
    close cleanly even after its strategy goes on the bench."""
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    sh.pause_strategy(conn, "winner", reason="diverged", pause_days=30)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_exit",
                     close=110.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_winner_settings())
    actions = [a.get("action") for a in res["actions"]]
    assert "SKIP_PAUSED_STRATEGY" not in actions


def test_auto_trader_resumes_after_expiry(isolated_db, monkeypatch):
    """When a pause has expired, the auto_trader no longer skips."""
    conn = db.init_db()
    _seed_strategy_with_backtest(conn, strategy_id="winner",
                                  mean_per_trade_pct=1.0)
    # Same eligibility seeding as above.
    for i in range(5):
        sid = db.record_signal(
            conn, strategy_id="winner", symbol="W",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=102.0, exit_reason="long_exit_signal", bars_held=1,
        )
    # Pause set FAR in the past — expires_at is also in the past.
    sh.pause_strategy(conn, "winner", reason="old", pause_days=1,
                       now_iso="2025-01-01T00:00:00+00:00")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=_winner_settings())
    actions = [a.get("action") for a in res["actions"]]
    assert "SKIP_PAUSED_STRATEGY" not in actions
    assert "DRY_BUY" in actions
