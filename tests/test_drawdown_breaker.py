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
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_outcomes(strategy_id, returns):
    conn = db.init_db()
    for i, ret in enumerate(returns):
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
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
    return conn


def _winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000, "skip_intraday_signals": True,
    }


# ---------------------------------------------------------------------------
# _coerce_max_daily_loss_pct
# ---------------------------------------------------------------------------

def test_coerce_daily_loss_disabled():
    assert at._coerce_max_daily_loss_pct(None) is None
    assert at._coerce_max_daily_loss_pct(0) is None
    assert at._coerce_max_daily_loss_pct(-2) is None
    assert at._coerce_max_daily_loss_pct("bad") is None


def test_coerce_daily_loss_positive():
    assert at._coerce_max_daily_loss_pct(2.0) == 2.0
    assert at._coerce_max_daily_loss_pct("1.5") == 1.5


# ---------------------------------------------------------------------------
# _drawdown_circuit_breaker
# ---------------------------------------------------------------------------

def test_breaker_returns_none_when_no_setting():
    out = at._drawdown_circuit_breaker(
        {}, {"portfolio_value": 95.0, "last_equity": 100.0},
    )
    assert out is None


def test_breaker_returns_none_when_no_account():
    out = at._drawdown_circuit_breaker(
        {"risk": {"max_daily_loss_pct": 2.0}}, {},
    )
    assert out is None


def test_breaker_returns_none_when_loss_below_threshold():
    out = at._drawdown_circuit_breaker(
        {"risk": {"max_daily_loss_pct": 2.0}},
        {"portfolio_value": 99.0, "last_equity": 100.0},  # -1% loss
    )
    assert out is None


def test_breaker_trips_at_exact_threshold():
    out = at._drawdown_circuit_breaker(
        {"risk": {"max_daily_loss_pct": 2.0}},
        {"portfolio_value": 98.0, "last_equity": 100.0},  # -2% loss exactly
    )
    assert out is not None
    assert out["daily_pl_pct"] == pytest.approx(-2.0)
    assert out["threshold_pct"] == 2.0


def test_breaker_trips_beyond_threshold():
    out = at._drawdown_circuit_breaker(
        {"risk": {"max_daily_loss_pct": 2.0}},
        {"portfolio_value": 95.0, "last_equity": 100.0},  # -5% loss
    )
    assert out is not None
    assert out["daily_pl_pct"] == pytest.approx(-5.0)


def test_breaker_uses_equity_at_open_if_present():
    out = at._drawdown_circuit_breaker(
        {"risk": {"max_daily_loss_pct": 2.0}},
        {"portfolio_value": 97.0, "equity_at_open": 100.0,
         "last_equity": 95.0},
    )
    # Open=100 → -3% (uses equity_at_open ahead of last_equity).
    assert out is not None
    assert out["daily_pl_pct"] == pytest.approx(-3.0)


def test_breaker_does_not_trip_on_gain():
    out = at._drawdown_circuit_breaker(
        {"risk": {"max_daily_loss_pct": 2.0}},
        {"portfolio_value": 110.0, "last_equity": 100.0},
    )
    assert out is None


def test_breaker_handles_zero_open_equity():
    out = at._drawdown_circuit_breaker(
        {"risk": {"max_daily_loss_pct": 2.0}},
        {"portfolio_value": 95.0, "last_equity": 0},
    )
    assert out is None


# ---------------------------------------------------------------------------
# Integration via process_signals
# ---------------------------------------------------------------------------

def test_process_signals_blocks_entries_when_breaker_trips(isolated_db):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "risk": {"max_daily_loss_pct": 2.0}}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {
            "portfolio_value": 95.0, "last_equity": 100.0,
        },
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "SKIP_DAILY_DRAWDOWN"
    assert actions[0]["threshold_pct"] == 2.0


def test_process_signals_allows_exits_when_breaker_trips(isolated_db):
    """Exit signals must still fire when the breaker is tripped."""
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    # Open an existing position so the exit has something to close.
    sig_entry = db.record_signal(
        conn, strategy_id="winner", symbol="GDX",
        bar_ts="2026-05-13", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": "entry-1", "signal_id": sig_entry,
        "strategy_id": "winner", "symbol": "GDX",
        "side": "buy", "qty": 10, "order_type": "market",
        "submitted_at": "2026-05-13", "status": "filled",
        "fill_price": 70.0,
    })
    # Today: an exit signal fires.
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_exit",
                     close=72.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "risk": {"max_daily_loss_pct": 2.0}}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {
            "portfolio_value": 95.0, "last_equity": 100.0,
        },
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    # Should be just the exit action; no entry to skip today.
    assert any(a["action"] == "DRY_SELL" for a in actions)


def test_process_signals_normal_when_loss_under_threshold(isolated_db):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "risk": {"max_daily_loss_pct": 2.0}}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {
            "portfolio_value": 99.5, "last_equity": 100.0,  # -0.5%
        },
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "DRY_BUY"


def test_process_signals_normal_when_breaker_setting_omitted(isolated_db):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    # No risk.max_daily_loss_pct set.
    settings = _winner_settings()
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {
            "portfolio_value": 50.0, "last_equity": 100.0,
        },
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    # Disabled → entry proceeds despite the catastrophic loss in the
    # injected account state.
    assert actions[0]["action"] == "DRY_BUY"


def test_breaker_skips_multiple_strategies(isolated_db):
    _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner2"}})
    _seed_outcomes("winner2", [1.5, 0.5] * 18)
    conn = db.init_db()
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="winner2", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "risk": {"max_daily_loss_pct": 2.0}}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {
            "portfolio_value": 90.0, "last_equity": 100.0,
        },
    )
    skips = [a for a in res["actions"]
             if a["action"] == "SKIP_DAILY_DRAWDOWN"]
    assert len(skips) == 2
