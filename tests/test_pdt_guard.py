"""
test_pdt_guard.py — 5.4.1: PDT counter + guard.

Covers:
  - count_round_trips_for_day: same-day buy+sell counted as one round trip
  - multi-symbol day-trade math (only matched buy/sell pairs count)
  - count_round_trips_last_5_days rolling window
  - pdt_status snapshot shape
  - check_pdt_guard threshold gating (account_value < $25k AND count >= 3)
  - paper-account bypass (account_value >= $25k → never blocks)
  - exact-threshold edge case ($25,000 → not below, so allowed)
  - None account_value → observe-only (returns None)
  - auto_trader integration: intraday entry blocked with SKIP_PDT_GUARD;
    EOD entry never blocked by PDT
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import pdt_guard as pdt  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _insert_filled_trade(conn, *, symbol: str, side: str,
                         filled_at: str,
                         alpaca_order_id: str,
                         status: str = "filled"):
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, symbol, side, qty, filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (alpaca_order_id, symbol, side, 1.0, filled_at, status, filled_at),
    )
    conn.commit()


def test_round_trips_same_day_one_symbol(isolated_db):
    conn = db.init_db()
    _insert_filled_trade(conn, symbol="SPY", side="buy",
                         filled_at="2026-05-14T14:00:00", alpaca_order_id="o1")
    _insert_filled_trade(conn, symbol="SPY", side="sell",
                         filled_at="2026-05-14T15:30:00", alpaca_order_id="o2")
    assert pdt.count_round_trips_for_day(conn, date(2026, 5, 14)) == 1


def test_round_trips_buy_without_sell_is_zero(isolated_db):
    conn = db.init_db()
    _insert_filled_trade(conn, symbol="SPY", side="buy",
                         filled_at="2026-05-14T14:00:00", alpaca_order_id="o1")
    assert pdt.count_round_trips_for_day(conn, date(2026, 5, 14)) == 0


def test_round_trips_multi_symbol(isolated_db):
    """SPY: 1 buy + 1 sell = 1 round trip. QQQ: 2 buys + 1 sell = 1 round trip."""
    conn = db.init_db()
    _insert_filled_trade(conn, symbol="SPY", side="buy",
                         filled_at="2026-05-14T14:00:00", alpaca_order_id="s1")
    _insert_filled_trade(conn, symbol="SPY", side="sell",
                         filled_at="2026-05-14T15:00:00", alpaca_order_id="s2")
    _insert_filled_trade(conn, symbol="QQQ", side="buy",
                         filled_at="2026-05-14T14:30:00", alpaca_order_id="q1")
    _insert_filled_trade(conn, symbol="QQQ", side="buy",
                         filled_at="2026-05-14T14:45:00", alpaca_order_id="q2")
    _insert_filled_trade(conn, symbol="QQQ", side="sell",
                         filled_at="2026-05-14T15:30:00", alpaca_order_id="q3")
    assert pdt.count_round_trips_for_day(conn, date(2026, 5, 14)) == 2


def test_round_trips_cross_day_does_not_count(isolated_db):
    """Buy on day 1, sell on day 2 → NOT a day trade."""
    conn = db.init_db()
    _insert_filled_trade(conn, symbol="SPY", side="buy",
                         filled_at="2026-05-13T15:00:00", alpaca_order_id="b1")
    _insert_filled_trade(conn, symbol="SPY", side="sell",
                         filled_at="2026-05-14T10:00:00", alpaca_order_id="s1")
    assert pdt.count_round_trips_for_day(conn, date(2026, 5, 13)) == 0
    assert pdt.count_round_trips_for_day(conn, date(2026, 5, 14)) == 0


def test_five_day_rolling_window(isolated_db):
    conn = db.init_db()
    # 3 round trips across 3 different days, all within the 5-day window.
    for i, day in enumerate(["2026-05-10", "2026-05-11", "2026-05-12"]):
        _insert_filled_trade(conn, symbol="SPY", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="SPY", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    # Window ending 2026-05-14 covers 05-10..05-14 (5 days).
    assert pdt.count_round_trips_last_5_days(conn, date(2026, 5, 14)) == 3


def test_five_day_excludes_older_trades(isolated_db):
    conn = db.init_db()
    # Round trip 6 days before asof → outside the 5-day window.
    _insert_filled_trade(conn, symbol="SPY", side="buy",
                         filled_at="2026-05-08T14:00:00", alpaca_order_id="b0")
    _insert_filled_trade(conn, symbol="SPY", side="sell",
                         filled_at="2026-05-08T15:00:00", alpaca_order_id="s0")
    assert pdt.count_round_trips_last_5_days(conn, date(2026, 5, 14)) == 0


def test_pdt_status_shape(isolated_db):
    conn = db.init_db()
    _insert_filled_trade(conn, symbol="SPY", side="buy",
                         filled_at="2026-05-14T14:00:00", alpaca_order_id="b1")
    _insert_filled_trade(conn, symbol="SPY", side="sell",
                         filled_at="2026-05-14T15:00:00", alpaca_order_id="s1")
    status = pdt.pdt_status(conn, account_value=10_000.0,
                             asof=date(2026, 5, 14))
    assert status["today"] == 1
    assert status["five_day"] == 1
    assert status["account_value"] == 10_000.0
    assert status["below_pdt_equity"] is True
    assert status["threshold"] == 3
    assert status["would_block"] is False  # only 1 round trip, below threshold


def test_pdt_status_blocks_at_threshold_when_below_equity(isolated_db):
    conn = db.init_db()
    for i, day in enumerate(["2026-05-12", "2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="SPY", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="SPY", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    status = pdt.pdt_status(conn, account_value=10_000.0,
                             asof=date(2026, 5, 14))
    assert status["five_day"] == 3
    assert status["would_block"] is True


def test_pdt_status_does_not_block_when_above_equity(isolated_db):
    conn = db.init_db()
    for i, day in enumerate(["2026-05-12", "2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="SPY", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="SPY", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    status = pdt.pdt_status(conn, account_value=100_000.0,
                             asof=date(2026, 5, 14))
    assert status["five_day"] == 3
    assert status["below_pdt_equity"] is False
    assert status["would_block"] is False


def test_pdt_status_equity_exactly_at_25k(isolated_db):
    """$25,000 exactly is the threshold — NOT below, so allowed."""
    conn = db.init_db()
    for i, day in enumerate(["2026-05-12", "2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="SPY", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="SPY", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    status = pdt.pdt_status(conn, account_value=25_000.0,
                             asof=date(2026, 5, 14))
    assert status["below_pdt_equity"] is False
    assert status["would_block"] is False


# ---------------- check_pdt_guard helper ----------------

def test_check_pdt_guard_returns_none_when_below_threshold(isolated_db):
    conn = db.init_db()
    # 2 round trips, account below $25k → still allowed.
    for i, day in enumerate(["2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="SPY", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="SPY", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    assert pdt.check_pdt_guard(conn, account_value=10_000.0,
                                asof=date(2026, 5, 14)) is None


def test_check_pdt_guard_blocks_at_threshold(isolated_db):
    conn = db.init_db()
    for i, day in enumerate(["2026-05-12", "2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="SPY", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="SPY", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    block = pdt.check_pdt_guard(conn, account_value=10_000.0,
                                 asof=date(2026, 5, 14))
    assert block is not None
    assert block["reason"] == "pdt_guard"
    assert block["five_day_round_trips"] == 3
    assert block["threshold"] == 3


def test_check_pdt_guard_paper_bypass(isolated_db):
    """Account >= $25k — never blocks even with many round trips."""
    conn = db.init_db()
    for i, day in enumerate(["2026-05-12", "2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="SPY", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="SPY", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    assert pdt.check_pdt_guard(conn, account_value=100_000.0,
                                asof=date(2026, 5, 14)) is None


def test_check_pdt_guard_none_account_observe_only(isolated_db):
    """No account value (dry-run) → observe-only, returns None."""
    conn = db.init_db()
    for i, day in enumerate(["2026-05-12", "2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="SPY", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="SPY", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    assert pdt.check_pdt_guard(conn, account_value=None,
                                asof=date(2026, 5, 14)) is None


# ---------------- auto_trader integration ----------------

def _seed_intraday_signal(conn, *, sid: str, sym: str,
                           bar_ts: str, bar_interval: str = "15m"):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    return db.record_signal(
        conn, strategy_id=sid, symbol=sym,
        bar_ts=bar_ts, signal_type="long_entry",
        close=400.0, bar_interval=bar_interval,
    )


def test_auto_trader_blocks_intraday_with_pdt_guard(isolated_db):
    """When account < $25k AND 3 round trips already this 5-day window,
    a new intraday entry produces SKIP_PDT_GUARD instead of an order."""
    conn = db.init_db()
    # 3 round trips in window.
    for i, day in enumerate(["2026-05-12", "2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="QQQ", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="QQQ", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    _seed_intraday_signal(conn, sid="intraday-mr-3bar-low-15m",
                           sym="SPY", bar_ts="2026-05-14T14:30:00")
    res = at.process_signals(
        conn,
        asof=date(2026, 5, 14),
        bar_interval="15m",
        settings={
            "enabled": True, "dry_run": True,
            "min_outcomes": 0, "min_mean_ret_pct": 0.0,
            "min_sharpe_ish": 0.0, "max_position_usd": 1000,
        },
        account_summary_fn=lambda: {"portfolio_value": 10_000.0,
                                      "equity_at_open": 10_000.0,
                                      "last_equity": 10_000.0,
                                      "cash": 10_000.0,
                                      "equity": 10_000.0,
                                      "buying_power": 10_000.0},
    )
    actions = res["actions"]
    skip = [a for a in actions if a.get("action") == "SKIP_PDT_GUARD"]
    assert skip, f"expected SKIP_PDT_GUARD in actions: {actions}"
    assert skip[0]["reason"] == "pdt_guard"
    assert skip[0]["five_day_round_trips"] == 3


def test_auto_trader_paper_bypass_pdt(isolated_db):
    """Account >= $25k → PDT guard does not block; some other action fires."""
    conn = db.init_db()
    for i, day in enumerate(["2026-05-12", "2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="QQQ", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="QQQ", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    _seed_intraday_signal(conn, sid="intraday-mr-3bar-low-15m",
                           sym="SPY", bar_ts="2026-05-14T14:30:00")
    res = at.process_signals(
        conn,
        asof=date(2026, 5, 14),
        bar_interval="15m",
        settings={
            "enabled": True, "dry_run": True,
            "min_outcomes": 0, "min_mean_ret_pct": 0.0,
            "min_sharpe_ish": 0.0, "max_position_usd": 1000,
        },
        account_summary_fn=lambda: {"portfolio_value": 100_000.0,
                                      "equity_at_open": 100_000.0,
                                      "last_equity": 100_000.0,
                                      "cash": 100_000.0,
                                      "equity": 100_000.0,
                                      "buying_power": 100_000.0},
    )
    actions = res["actions"]
    assert all(a.get("action") != "SKIP_PDT_GUARD" for a in actions), actions


def test_auto_trader_eod_signal_never_pdt_blocked(isolated_db):
    """EOD (1d) signals are by definition not day trades — PDT guard skips."""
    conn = db.init_db()
    # 5 round trips — would block intraday on a small account.
    for i, day in enumerate(["2026-05-10", "2026-05-11", "2026-05-12",
                              "2026-05-13", "2026-05-14"]):
        _insert_filled_trade(conn, symbol="QQQ", side="buy",
                             filled_at=f"{day}T14:00:00",
                             alpaca_order_id=f"b{i}")
        _insert_filled_trade(conn, symbol="QQQ", side="sell",
                             filled_at=f"{day}T15:00:00",
                             alpaca_order_id=f"s{i}")
    db.upsert_strategy(conn, {"extra": {"strategy_id": "botnet101-3-bar-low"}})
    db.record_signal(
        conn, strategy_id="botnet101-3-bar-low", symbol="SPY",
        bar_ts="2026-05-14", signal_type="long_entry",
        close=400.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn,
        asof=date(2026, 5, 14),
        settings={
            "enabled": True, "dry_run": True,
            "min_outcomes": 0, "min_mean_ret_pct": 0.0,
            "min_sharpe_ish": 0.0, "max_position_usd": 1000,
        },
        account_summary_fn=lambda: {"portfolio_value": 10_000.0,
                                      "equity_at_open": 10_000.0,
                                      "last_equity": 10_000.0,
                                      "cash": 10_000.0,
                                      "equity": 10_000.0,
                                      "buying_power": 10_000.0},
    )
    actions = res["actions"]
    assert all(a.get("action") != "SKIP_PDT_GUARD" for a in actions), actions


# ---------------- date coercion ----------------

def test_coerce_date_accepts_multiple_formats():
    assert pdt._coerce_date(date(2026, 5, 14)) == date(2026, 5, 14)
    assert pdt._coerce_date(datetime(2026, 5, 14, 10, 0)) == date(2026, 5, 14)
    assert pdt._coerce_date("2026-05-14") == date(2026, 5, 14)
    assert pdt._coerce_date("2026-05-14T15:30:00Z") == date(2026, 5, 14)
    assert pdt._coerce_date("2026-05-14T15:30:00") == date(2026, 5, 14)
