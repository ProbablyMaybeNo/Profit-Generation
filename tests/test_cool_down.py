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
    yield test_db


def _seed_outcomes_with_dates(conn, strategy_id, dated_returns):
    """`dated_returns` = [(entry_iso, exit_iso, return_pct_via_prices), ...].

    Each tuple records a signal + open + close. return_pct is computed by
    db.close_outcome from entry_price (100.0) and exit_price (= 100 * (1+r/100))
    so the closed outcome shows the requested return_pct.
    """
    for entry, exit_, ret in dated_returns:
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
            bar_ts=entry, signal_type="long_entry",
            close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=entry, entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=exit_,
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )


def _winner_settings(extra=None):
    out = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 3, "min_mean_ret_pct": -100.0, "min_sharpe_ish": -100.0,
        "max_position_usd": 1000, "skip_intraday_signals": True,
        "cool_down_losers": 3, "cool_down_days": 5,
    }
    if extra:
        out.update(extra)
    return out


def _seed_trading_calendar(conn, dates):
    """Seed snapshots.snapshot_date rows so _trading_days_between can count them."""
    for d in dates:
        db.record_snapshot_row(conn, d, {
            "symbol": "SPY", "asset_class": "etf", "bar_date": d,
            "close": 100.0, "ret_1d_pct": 0.0, "ret_5d_pct": 0.0,
            "ret_20d_pct": 0.0, "rvol_vs_20d": 1.0, "dist_sma20_pct": 0.0,
            "error": None,
        })


# ---------------------------------------------------------------------------
# _coerce_*
# ---------------------------------------------------------------------------

def test_coerce_cool_down_losers_defaults():
    assert at._coerce_cool_down_losers(None) == at.DEFAULT_COOL_DOWN_LOSERS
    assert at._coerce_cool_down_losers("bad") == at.DEFAULT_COOL_DOWN_LOSERS


def test_coerce_cool_down_losers_disables_on_zero_or_negative():
    assert at._coerce_cool_down_losers(0) == 0
    assert at._coerce_cool_down_losers(-1) == 0


def test_coerce_cool_down_losers_honors_value():
    assert at._coerce_cool_down_losers(5) == 5
    assert at._coerce_cool_down_losers("4") == 4


def test_coerce_cool_down_days_defaults():
    assert at._coerce_cool_down_days(None) == at.DEFAULT_COOL_DOWN_DAYS
    assert at._coerce_cool_down_days("bad") == at.DEFAULT_COOL_DOWN_DAYS


def test_coerce_cool_down_days_disables_on_zero_or_negative():
    assert at._coerce_cool_down_days(0) == 0
    assert at._coerce_cool_down_days(-1) == 0


# ---------------------------------------------------------------------------
# _trading_days_between
# ---------------------------------------------------------------------------

def test_trading_days_between_uses_snapshots(isolated_db):
    conn = db.init_db()
    _seed_trading_calendar(conn, [
        "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15",
    ])
    # Mon → Fri: 4 trading days strictly between.
    assert at._trading_days_between(conn, "2026-05-11", "2026-05-15") == 4
    # Same day → 0.
    assert at._trading_days_between(conn, "2026-05-11", "2026-05-11") == 0
    # End before start → 0.
    assert at._trading_days_between(conn, "2026-05-15", "2026-05-11") == 0
    conn.close()


def test_trading_days_between_falls_back_to_weekday_count(isolated_db):
    conn = db.init_db()
    # Mon 2026-05-11 → Mon 2026-05-18: Tue, Wed, Thu, Fri, Mon = 5.
    assert at._trading_days_between(conn, "2026-05-11", "2026-05-18") == 5
    # Sat/Sun ignored: Fri 2026-05-15 → Mon 2026-05-18 = 1.
    assert at._trading_days_between(conn, "2026-05-15", "2026-05-18") == 1
    conn.close()


def test_trading_days_between_handles_bad_input(isolated_db):
    conn = db.init_db()
    assert at._trading_days_between(conn, "garbage", "2026-05-15") == 0
    assert at._trading_days_between(conn, None, "2026-05-15") == 0
    conn.close()


# ---------------------------------------------------------------------------
# _last_n_closed_outcomes
# ---------------------------------------------------------------------------

def test_last_n_closed_outcomes_returns_newest_first(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s"}})
    _seed_outcomes_with_dates(conn, "s", [
        ("2026-05-01", "2026-05-02", 1.0),
        ("2026-05-03", "2026-05-04", -1.5),
        ("2026-05-05", "2026-05-06", 2.0),
    ])
    out = at._last_n_closed_outcomes(conn, "s", 3)
    assert len(out) == 3
    assert out[0]["exit_ts"] == "2026-05-06"
    assert out[0]["return_pct"] == pytest.approx(2.0)
    assert out[2]["exit_ts"] == "2026-05-02"
    conn.close()


def test_last_n_closed_outcomes_caps_at_n(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s"}})
    _seed_outcomes_with_dates(conn, "s", [
        (f"2026-05-{i:02d}", f"2026-05-{i+1:02d}", 1.0) for i in range(1, 8)
    ])
    assert len(at._last_n_closed_outcomes(conn, "s", 3)) == 3
    assert len(at._last_n_closed_outcomes(conn, "s", 0)) == 0
    conn.close()


# ---------------------------------------------------------------------------
# _cool_down_state
# ---------------------------------------------------------------------------

def test_cool_down_disabled_when_setting_zero(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s"}})
    _seed_outcomes_with_dates(conn, "s", [
        ("2026-05-01", "2026-05-02", -1.0),
        ("2026-05-03", "2026-05-04", -1.0),
        ("2026-05-05", "2026-05-06", -1.0),
    ])
    settings = _winner_settings({"cool_down_losers": 0})
    assert at._cool_down_state(conn, "s", settings,
                               asof=date(2026, 5, 7)) is None
    conn.close()


def test_cool_down_trips_on_three_losers(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s"}})
    _seed_outcomes_with_dates(conn, "s", [
        ("2026-05-04", "2026-05-05", -1.0),
        ("2026-05-06", "2026-05-07", -2.0),
        ("2026-05-08", "2026-05-11", -0.5),  # last loser
    ])
    settings = _winner_settings()
    # asof = next trading day after last loser → 0 trading days since.
    cd = at._cool_down_state(conn, "s", settings, asof=date(2026, 5, 11))
    assert cd is not None
    assert cd["losers_required"] == 3
    assert cd["pause_days"] == 5
    assert cd["last_loser_exit_ts"] == "2026-05-11"
    assert cd["trading_days_remaining"] == 5
    conn.close()


def test_cool_down_does_not_trip_with_mixed_wins(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s"}})
    _seed_outcomes_with_dates(conn, "s", [
        ("2026-05-01", "2026-05-02", -1.0),
        ("2026-05-03", "2026-05-04", 2.0),  # win in the middle
        ("2026-05-05", "2026-05-06", -1.0),
    ])
    cd = at._cool_down_state(conn, "s", _winner_settings(),
                             asof=date(2026, 5, 7))
    assert cd is None
    conn.close()


def test_cool_down_does_not_trip_with_too_few_outcomes(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s"}})
    _seed_outcomes_with_dates(conn, "s", [
        ("2026-05-01", "2026-05-02", -1.0),
        ("2026-05-03", "2026-05-04", -1.0),
        # only 2 outcomes — needs 3.
    ])
    cd = at._cool_down_state(conn, "s", _winner_settings(),
                             asof=date(2026, 5, 5))
    assert cd is None
    conn.close()


def test_cool_down_zero_return_counts_as_loser(isolated_db):
    """`return_pct <= 0` is the loser predicate — flat trades are losers
    because they tied up capital for nothing."""
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s"}})
    _seed_outcomes_with_dates(conn, "s", [
        ("2026-05-04", "2026-05-05", -1.0),
        ("2026-05-06", "2026-05-07", 0.0),
        ("2026-05-08", "2026-05-11", -0.5),
    ])
    cd = at._cool_down_state(conn, "s", _winner_settings(),
                             asof=date(2026, 5, 11))
    assert cd is not None
    conn.close()


def test_cool_down_rearms_after_five_trading_days(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s"}})
    _seed_outcomes_with_dates(conn, "s", [
        ("2026-05-04", "2026-05-05", -1.0),
        ("2026-05-05", "2026-05-06", -1.0),
        ("2026-05-07", "2026-05-08", -1.0),  # Fri last loser
    ])
    # Seed exactly 5 trading days after the last loser exit_ts.
    _seed_trading_calendar(conn, [
        "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15",
    ])
    cd = at._cool_down_state(conn, "s", _winner_settings(),
                             asof=date(2026, 5, 15))
    assert cd is None  # exactly 5 trading days passed → re-armed
    conn.close()


def test_cool_down_still_active_at_four_trading_days(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s"}})
    _seed_outcomes_with_dates(conn, "s", [
        ("2026-05-04", "2026-05-05", -1.0),
        ("2026-05-05", "2026-05-06", -1.0),
        ("2026-05-07", "2026-05-08", -1.0),
    ])
    _seed_trading_calendar(conn, [
        "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14",
    ])
    cd = at._cool_down_state(conn, "s", _winner_settings(),
                             asof=date(2026, 5, 14))
    assert cd is not None
    assert cd["trading_days_since"] == 4
    assert cd["trading_days_remaining"] == 1
    conn.close()


# ---------------------------------------------------------------------------
# Integration via process_signals
# ---------------------------------------------------------------------------

def test_process_signals_blocks_entry_during_cool_down(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "loser"}})
    _seed_outcomes_with_dates(conn, "loser", [
        ("2026-05-04", "2026-05-05", -1.0),
        ("2026-05-06", "2026-05-07", -1.0),
        ("2026-05-08", "2026-05-11", -1.0),
    ])
    db.record_signal(
        conn, strategy_id="loser", symbol="GDX",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "loser"]
    assert actions
    assert actions[0]["action"] == "SKIP_COOL_DOWN"
    assert actions[0]["trading_days_remaining"] >= 1
    conn.close()


def test_process_signals_allows_exit_during_cool_down(isolated_db):
    """Exits ALWAYS proceed — cool-down only blocks new entries."""
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "loser"}})
    _seed_outcomes_with_dates(conn, "loser", [
        ("2026-05-04", "2026-05-05", -1.0),
        ("2026-05-06", "2026-05-07", -1.0),
        ("2026-05-08", "2026-05-11", -1.0),
    ])
    sig_entry = db.record_signal(
        conn, strategy_id="loser", symbol="GDX",
        bar_ts="2026-05-11", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": "entry-1", "signal_id": sig_entry,
        "strategy_id": "loser", "symbol": "GDX",
        "side": "buy", "qty": 10, "order_type": "market",
        "submitted_at": "2026-05-11", "status": "filled",
        "fill_price": 70.0,
    })
    db.record_signal(
        conn, strategy_id="loser", symbol="GDX",
        bar_ts="2026-05-12", signal_type="long_exit",
        close=72.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "loser"]
    assert any(a["action"] == "DRY_SELL" for a in actions)
    conn.close()


def test_process_signals_re_arms_after_window(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "loser"}})
    _seed_outcomes_with_dates(conn, "loser", [
        ("2026-05-04", "2026-05-05", -1.0),
        ("2026-05-05", "2026-05-06", -1.0),
        ("2026-05-07", "2026-05-08", -1.0),
    ])
    _seed_trading_calendar(conn, [
        "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15",
    ])
    db.record_signal(
        conn, strategy_id="loser", symbol="GDX",
        bar_ts="2026-05-15", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 15), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "loser"]
    # Cool-down has elapsed → should NOT be SKIP_COOL_DOWN.
    assert all(a["action"] != "SKIP_COOL_DOWN" for a in actions)
    conn.close()


def test_process_signals_mixed_outcomes_do_not_trigger(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "mixed"}})
    _seed_outcomes_with_dates(conn, "mixed", [
        ("2026-05-04", "2026-05-05", -1.0),
        ("2026-05-06", "2026-05-07", 2.0),  # win
        ("2026-05-08", "2026-05-11", -1.0),
    ])
    db.record_signal(
        conn, strategy_id="mixed", symbol="GDX",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "mixed"]
    assert all(a["action"] != "SKIP_COOL_DOWN" for a in actions)
    conn.close()


def test_process_signals_cool_down_disabled_by_zero(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "loser"}})
    _seed_outcomes_with_dates(conn, "loser", [
        ("2026-05-04", "2026-05-05", -1.0),
        ("2026-05-06", "2026-05-07", -1.0),
        ("2026-05-08", "2026-05-11", -1.0),
    ])
    db.record_signal(
        conn, strategy_id="loser", symbol="GDX",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    settings = _winner_settings({"cool_down_losers": 0})
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=settings,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "loser"]
    assert all(a["action"] != "SKIP_COOL_DOWN" for a in actions)
    conn.close()
