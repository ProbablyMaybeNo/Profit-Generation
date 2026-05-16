import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import earnings_calendar as ec  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_earnings_table_exists(isolated_db):
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='earnings'"
        ).fetchone()
        assert row is not None
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(earnings)").fetchall()}
        assert cols == {"symbol", "earnings_date", "source", "fetched_at"}
    finally:
        conn.close()


def test_upsert_earnings_date_is_idempotent(isolated_db):
    conn = db.init_db()
    try:
        assert db.upsert_earnings_date(
            conn, symbol="KRE", earnings_date="2026-06-12", source="yfinance") == 1
        assert db.upsert_earnings_date(
            conn, symbol="KRE", earnings_date="2026-06-12", source="yfinance") == 0
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM earnings WHERE symbol='KRE'"
        ).fetchone()["c"]
        assert count == 1
    finally:
        conn.close()


def test_upsert_earnings_date_skips_empty_inputs(isolated_db):
    conn = db.init_db()
    try:
        assert db.upsert_earnings_date(
            conn, symbol="", earnings_date="2026-06-12") == 0
        assert db.upsert_earnings_date(
            conn, symbol="KRE", earnings_date="") == 0
    finally:
        conn.close()


def test_next_earnings_date_on_or_after(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-04-12")
        db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-07-12")
        db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-10-12")
        assert db.next_earnings_date_on_or_after(
            conn, "KRE", "2026-05-01") == "2026-07-12"
        assert db.next_earnings_date_on_or_after(
            conn, "KRE", "2026-07-12") == "2026-07-12"
        assert db.next_earnings_date_on_or_after(
            conn, "KRE", "2027-01-01") is None
        assert db.next_earnings_date_on_or_after(
            conn, "NOPE", "2026-05-01") is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _coerce_date + _extract_dates_from_calendar
# ---------------------------------------------------------------------------

def test_coerce_date_handles_python_date():
    assert ec._coerce_date(date(2026, 5, 15)) == date(2026, 5, 15)


def test_coerce_date_handles_datetime():
    assert ec._coerce_date(datetime(2026, 5, 15, 12, 0)) == date(2026, 5, 15)


def test_coerce_date_handles_iso_string():
    assert ec._coerce_date("2026-05-15") == date(2026, 5, 15)
    assert ec._coerce_date("2026-05-15T13:00:00") == date(2026, 5, 15)


def test_coerce_date_returns_none_on_garbage():
    assert ec._coerce_date(None) is None
    assert ec._coerce_date("not a date") is None
    assert ec._coerce_date("") is None


def test_extract_dates_from_calendar_dict_shape():
    cal = {"Earnings Date": [date(2026, 6, 12), date(2026, 6, 13)],
           "Earnings Average": 1.5}
    out = ec._extract_dates_from_calendar(cal)
    assert out == [date(2026, 6, 12), date(2026, 6, 13)]


def test_extract_dates_from_calendar_single_value():
    cal = {"Earnings Date": date(2026, 6, 12)}
    assert ec._extract_dates_from_calendar(cal) == [date(2026, 6, 12)]


def test_extract_dates_from_calendar_empty():
    assert ec._extract_dates_from_calendar(None) == []
    assert ec._extract_dates_from_calendar({}) == []
    assert ec._extract_dates_from_calendar({"Other": 1}) == []


def test_extract_dates_filters_bad_items():
    cal = {"Earnings Date": [date(2026, 6, 12), "garbage", None]}
    assert ec._extract_dates_from_calendar(cal) == [date(2026, 6, 12)]


# ---------------------------------------------------------------------------
# fetch_next_earnings
# ---------------------------------------------------------------------------

def test_fetch_next_earnings_uses_factory():
    fake_ticker = MagicMock()
    fake_ticker.calendar = {"Earnings Date":
                             [date.today() + timedelta(days=5)]}
    out = ec.fetch_next_earnings(
        "KRE", get_ticker=lambda sym: fake_ticker,
    )
    assert out == [date.today() + timedelta(days=5)]


def test_fetch_next_earnings_filters_past_dates():
    fake_ticker = MagicMock()
    fake_ticker.calendar = {"Earnings Date":
                             [date.today() - timedelta(days=30),
                              date.today() + timedelta(days=10)]}
    out = ec.fetch_next_earnings("KRE", get_ticker=lambda sym: fake_ticker)
    assert out == [date.today() + timedelta(days=10)]


def test_fetch_next_earnings_returns_empty_on_factory_error():
    def boom(sym):
        raise RuntimeError("yfinance import failed")
    assert ec.fetch_next_earnings("KRE", get_ticker=boom) == []


def test_fetch_next_earnings_returns_empty_on_calendar_error():
    fake_ticker = MagicMock()
    type(fake_ticker).calendar = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("404")))
    assert ec.fetch_next_earnings(
        "KRE", get_ticker=lambda sym: fake_ticker) == []


# ---------------------------------------------------------------------------
# persist + fetch_and_persist_for_universe
# ---------------------------------------------------------------------------

def test_persist_earnings_dates_is_idempotent(isolated_db):
    target = date.today() + timedelta(days=10)
    assert ec.persist_earnings_dates("KRE", [target]) == 1
    assert ec.persist_earnings_dates("KRE", [target]) == 0


def test_fetch_and_persist_for_universe_dedupes_symbols(isolated_db):
    calls = []
    def fake_factory(sym):
        calls.append(sym)
        t = MagicMock()
        t.calendar = {"Earnings Date": [date.today() + timedelta(days=5)]}
        return t
    out = ec.fetch_and_persist_for_universe(
        ["KRE", "XME", "KRE"], get_ticker=fake_factory,
    )
    assert calls == ["KRE", "XME"]
    assert set(out.keys()) == {"KRE", "XME"}
    assert out["KRE"] == 1


# ---------------------------------------------------------------------------
# is_within_earnings_window
# ---------------------------------------------------------------------------

def _seed_calendar(conn, dates):
    for d in dates:
        db.record_snapshot_row(conn, d, {
            "symbol": "SPY", "asset_class": "etf", "bar_date": d,
            "close": 100.0, "ret_1d_pct": 0.0, "ret_5d_pct": 0.0,
            "ret_20d_pct": 0.0, "rvol_vs_20d": 1.0, "dist_sma20_pct": 0.0,
            "error": None,
        })


def test_window_returns_none_when_no_earnings(isolated_db):
    conn = db.init_db()
    try:
        assert ec.is_within_earnings_window(
            conn, "KRE", asof=date(2026, 5, 12)) is None
    finally:
        conn.close()


def test_window_trips_when_earnings_today(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-05-12")
        out = ec.is_within_earnings_window(
            conn, "KRE", asof=date(2026, 5, 12), window_trading_days=2,
        )
        assert out is not None
        assert out["trading_days_until"] == 0
        assert out["earnings_date"] == "2026-05-12"
    finally:
        conn.close()


def test_window_trips_at_two_trading_days(isolated_db):
    conn = db.init_db()
    try:
        _seed_calendar(conn, ["2026-05-12", "2026-05-13", "2026-05-14"])
        db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-05-14")
        out = ec.is_within_earnings_window(
            conn, "KRE", asof=date(2026, 5, 12), window_trading_days=2,
        )
        assert out is not None
        assert out["trading_days_until"] == 2
    finally:
        conn.close()


def test_window_does_not_trip_beyond_two_days(isolated_db):
    conn = db.init_db()
    try:
        _seed_calendar(conn, [
            "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15",
        ])
        db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-05-15")
        assert ec.is_within_earnings_window(
            conn, "KRE", asof=date(2026, 5, 12), window_trading_days=2,
        ) is None
    finally:
        conn.close()


def test_window_disabled_when_window_zero(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-05-12")
        assert ec.is_within_earnings_window(
            conn, "KRE", asof=date(2026, 5, 12), window_trading_days=0,
        ) is None
    finally:
        conn.close()


def test_window_returns_only_future_event(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-04-12")
        db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-07-12")
        # asof = 2026-05-12 → next event is 2026-07-12, far beyond the window.
        assert ec.is_within_earnings_window(
            conn, "KRE", asof=date(2026, 5, 12), window_trading_days=2,
        ) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Integration via process_signals
# ---------------------------------------------------------------------------

def _winner_settings(extra=None):
    out = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 3, "min_mean_ret_pct": -100.0, "min_sharpe_ish": -100.0,
        "max_position_usd": 1000, "skip_intraday_signals": True,
        "cool_down_losers": 0,  # isolated tests focus on the earnings path
        "earnings_veto_days": 2,
    }
    if extra:
        out.update(extra)
    return out


def _seed_eligible_outcomes(conn, strategy_id, n=5):
    for i in range(n):
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="HIST",
            bar_ts=f"2026-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2026-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2026-01-{i+2:02d}",
            exit_price=101.0, exit_reason="long_exit_signal", bars_held=1,
        )


def test_process_signals_blocks_entry_inside_earnings_window(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_eligible_outcomes(conn, "alpha")
    db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-05-13")
    db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["symbol"] == "KRE"]
    assert actions
    assert actions[0]["action"] == "SKIP_EARNINGS_WEEK"
    assert actions[0]["earnings_date"] == "2026-05-13"
    conn.close()


def test_process_signals_allows_entry_outside_window(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_eligible_outcomes(conn, "alpha")
    _seed_calendar(conn, [
        "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15", "2026-05-18",
    ])
    db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-05-18")
    db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["symbol"] == "KRE"]
    assert all(a["action"] != "SKIP_EARNINGS_WEEK" for a in actions)
    conn.close()


def test_process_signals_allows_exit_during_earnings_window(isolated_db):
    """Exits ALWAYS proceed — earnings veto only blocks new entries."""
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_eligible_outcomes(conn, "alpha")
    db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-05-13")
    sig_entry = db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-11", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": "entry-1", "signal_id": sig_entry,
        "strategy_id": "alpha", "symbol": "KRE",
        "side": "buy", "qty": 10, "order_type": "market",
        "submitted_at": "2026-05-11", "status": "filled",
        "fill_price": 70.0,
    })
    db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-12", signal_type="long_exit",
        close=72.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["symbol"] == "KRE"]
    assert any(a["action"] == "DRY_SELL" for a in actions)
    conn.close()


def test_process_signals_disabled_when_setting_zero(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_eligible_outcomes(conn, "alpha")
    db.upsert_earnings_date(conn, symbol="KRE", earnings_date="2026-05-13")
    db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    settings = _winner_settings({"earnings_veto_days": 0})
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=settings,
    )
    actions = [a for a in res["actions"] if a["symbol"] == "KRE"]
    assert all(a["action"] != "SKIP_EARNINGS_WEEK" for a in actions)
    conn.close()


def test_coerce_earnings_veto_days_defaults():
    assert at._coerce_earnings_veto_days(None) == at.DEFAULT_EARNINGS_VETO_DAYS
    assert at._coerce_earnings_veto_days("bad") == at.DEFAULT_EARNINGS_VETO_DAYS


def test_coerce_earnings_veto_days_disables_on_negative():
    assert at._coerce_earnings_veto_days(0) == 0
    assert at._coerce_earnings_veto_days(-1) == 0
