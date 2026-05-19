"""
test_intraday_symbol_cap.py — 5.5.2: per-symbol same-day round-trip cap.

Covers:
  - intraday_round_trips_today: per-symbol min(buys, sells) on intraday
    paper_trades for `asof`
  - EOD trades on the same symbol are excluded from the count
  - check_intraday_symbol_cap: returns None below cap, SKIP payload at cap
  - cap <= 0 disables the guard
  - daily reset (yesterday's trades don't count toward today's cap)
  - auto_trader integration:
      * intraday entry on a symbol with 2 round trips today (cap default)
        → SKIP_INTRADAY_SYMBOL_CAP
      * EOD entry on the same symbol → not affected (no cap)
      * intraday entry on a different symbol → not blocked
      * cap setting override (max_intraday_round_trips_per_symbol)
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import intraday_symbol_cap as isc  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _insert_intraday_trade(conn, *, symbol: str, side: str,
                            filled_at: str, order_id: str,
                            strategy_id: str = "intra-sid",
                            bar_interval: str = "15m"):
    """Insert a paper_trade row backed by an intraday-tagged signal.

    The bar_ts incorporates `order_id` so per-leg signal rows never
    collide on the (strategy, symbol, bar_ts, interval, type) unique
    constraint — the cap test only cares about the paper_trades rows.
    """
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    sig_type = "long_entry" if side == "buy" else "long_exit"
    bar_ts = f"{filled_at}-{order_id}"
    sig_id = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts, signal_type=sig_type,
        close=100.0, bar_interval=bar_interval,
    )
    assert sig_id is not None, f"signal already existed for {bar_ts}"
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, sig_id, strategy_id, symbol, side, 1.0,
         filled_at, "filled", filled_at),
    )
    conn.commit()


def _insert_eod_trade(conn, *, symbol: str, side: str,
                       filled_at: str, order_id: str,
                       strategy_id: str = "eod-sid"):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    sig_type = "long_entry" if side == "buy" else "long_exit"
    sig_id = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=f"{filled_at[:10]}-{order_id}", signal_type=sig_type,
        close=100.0, bar_interval="1d",
    )
    assert sig_id is not None
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, sig_id, strategy_id, symbol, side, 1.0,
         filled_at, "filled", filled_at),
    )
    conn.commit()


# ---------------- counter ----------------

def test_zero_when_no_trades(isolated_db):
    conn = db.init_db()
    assert isc.intraday_round_trips_today(conn, "SPY",
                                            asof=date(2026, 5, 14)) == 0


def test_one_round_trip(isolated_db):
    conn = db.init_db()
    _insert_intraday_trade(conn, symbol="SPY", side="buy",
                            filled_at="2026-05-14T14:00:00", order_id="b1")
    _insert_intraday_trade(conn, symbol="SPY", side="sell",
                            filled_at="2026-05-14T15:00:00", order_id="s1")
    assert isc.intraday_round_trips_today(conn, "SPY",
                                            asof=date(2026, 5, 14)) == 1


def test_two_round_trips(isolated_db):
    conn = db.init_db()
    for i in range(2):
        _insert_intraday_trade(conn, symbol="SPY", side="buy",
                                filled_at=f"2026-05-14T1{i}:00:00",
                                order_id=f"b{i}")
        _insert_intraday_trade(conn, symbol="SPY", side="sell",
                                filled_at=f"2026-05-14T1{i+1}:00:00",
                                order_id=f"s{i}")
    assert isc.intraday_round_trips_today(conn, "SPY",
                                            asof=date(2026, 5, 14)) == 2


def test_asymmetric_buys_sells(isolated_db):
    """3 buys, 1 sell → 1 round trip (min)."""
    conn = db.init_db()
    for i in range(3):
        _insert_intraday_trade(conn, symbol="SPY", side="buy",
                                filled_at=f"2026-05-14T1{i}:00:00",
                                order_id=f"b{i}")
    _insert_intraday_trade(conn, symbol="SPY", side="sell",
                            filled_at="2026-05-14T15:00:00", order_id="s0")
    assert isc.intraday_round_trips_today(conn, "SPY",
                                            asof=date(2026, 5, 14)) == 1


def test_eod_trades_excluded(isolated_db):
    """EOD (1d) round trips on the same symbol don't count toward intraday cap."""
    conn = db.init_db()
    _insert_eod_trade(conn, symbol="SPY", side="buy",
                       filled_at="2026-05-14T14:00:00", order_id="eb1")
    _insert_eod_trade(conn, symbol="SPY", side="sell",
                       filled_at="2026-05-14T15:00:00", order_id="es1")
    assert isc.intraday_round_trips_today(conn, "SPY",
                                            asof=date(2026, 5, 14)) == 0


def test_other_symbols_not_counted(isolated_db):
    conn = db.init_db()
    _insert_intraday_trade(conn, symbol="QQQ", side="buy",
                            filled_at="2026-05-14T14:00:00", order_id="b1")
    _insert_intraday_trade(conn, symbol="QQQ", side="sell",
                            filled_at="2026-05-14T15:00:00", order_id="s1")
    assert isc.intraday_round_trips_today(conn, "SPY",
                                            asof=date(2026, 5, 14)) == 0


def test_yesterday_excluded(isolated_db):
    """Round trip yesterday → not counted for today's cap."""
    conn = db.init_db()
    _insert_intraday_trade(conn, symbol="SPY", side="buy",
                            filled_at="2026-05-13T14:00:00", order_id="b1")
    _insert_intraday_trade(conn, symbol="SPY", side="sell",
                            filled_at="2026-05-13T15:00:00", order_id="s1")
    assert isc.intraday_round_trips_today(conn, "SPY",
                                            asof=date(2026, 5, 14)) == 0


# ---------------- check_intraday_symbol_cap ----------------

def test_check_returns_none_below_cap(isolated_db):
    conn = db.init_db()
    _insert_intraday_trade(conn, symbol="SPY", side="buy",
                            filled_at="2026-05-14T14:00:00", order_id="b1")
    _insert_intraday_trade(conn, symbol="SPY", side="sell",
                            filled_at="2026-05-14T15:00:00", order_id="s1")
    assert isc.check_intraday_symbol_cap(
        conn, symbol="SPY", asof=date(2026, 5, 14), cap=2,
    ) is None


def test_check_blocks_at_cap(isolated_db):
    conn = db.init_db()
    for i in range(2):
        _insert_intraday_trade(conn, symbol="SPY", side="buy",
                                filled_at=f"2026-05-14T1{i}:00:00",
                                order_id=f"b{i}")
        _insert_intraday_trade(conn, symbol="SPY", side="sell",
                                filled_at=f"2026-05-14T1{i+1}:00:00",
                                order_id=f"s{i}")
    block = isc.check_intraday_symbol_cap(
        conn, symbol="SPY", asof=date(2026, 5, 14), cap=2,
    )
    assert block is not None
    assert block["reason"] == "intraday_symbol_cap"
    assert block["round_trips_today"] == 2
    assert block["cap"] == 2
    assert block["symbol"] == "SPY"


def test_check_zero_cap_disables(isolated_db):
    conn = db.init_db()
    for i in range(5):
        _insert_intraday_trade(conn, symbol="SPY", side="buy",
                                filled_at=f"2026-05-14T1{i}:00:00",
                                order_id=f"b{i}")
        _insert_intraday_trade(conn, symbol="SPY", side="sell",
                                filled_at=f"2026-05-14T1{i+1}:00:00",
                                order_id=f"s{i}")
    assert isc.check_intraday_symbol_cap(
        conn, symbol="SPY", asof=date(2026, 5, 14), cap=0,
    ) is None


# ---------------- auto_trader integration ----------------

def _settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 0, "min_mean_ret_pct": 0.0,
        "min_sharpe_ish": 0.0, "max_position_usd": 1000.0,
        "sizing_method": "fixed",
        "grace_period_size_multiplier": 1.0,
    }


_INTRADAY_DECL = [{
    "id": "intra-sid",
    "compute": "compute_3bar_low_intraday",
    "bar_interval": "15m",
    "active_on": ["SPY"],
    "grace_period": True,
}]


def _patch_tracked(monkeypatch, decls):
    from monitoring import config as mc
    monkeypatch.setattr(mc, "TRACKED_STRATEGIES", decls)


def test_auto_trader_blocks_intraday_at_symbol_cap(isolated_db, monkeypatch):
    """When SPY has 2 round trips today (the default cap), a new intraday
    entry on SPY produces SKIP_INTRADAY_SYMBOL_CAP."""
    _patch_tracked(monkeypatch, _INTRADAY_DECL)
    conn = db.init_db()
    # Seed 2 round trips on SPY today.
    for i in range(2):
        _insert_intraday_trade(conn, symbol="SPY", side="buy",
                                filled_at=f"2026-05-14T1{i}:00:00",
                                order_id=f"b{i}")
        _insert_intraday_trade(conn, symbol="SPY", side="sell",
                                filled_at=f"2026-05-14T1{i+1}:00:00",
                                order_id=f"s{i}")
    # New intraday signal on SPY at 14:30.
    db.record_signal(conn, strategy_id="intra-sid", symbol="SPY",
                      bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
                      close=50.0, bar_interval="15m")
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        bar_interval="15m", settings=_settings(),
    )
    actions = res["actions"]
    blocks = [a for a in actions
               if a.get("action") == "SKIP_INTRADAY_SYMBOL_CAP"]
    assert blocks, f"expected SKIP_INTRADAY_SYMBOL_CAP in {actions}"
    assert blocks[0]["symbol"] == "SPY"
    assert blocks[0]["cap"] == 2


def test_auto_trader_different_symbol_not_blocked(isolated_db, monkeypatch):
    """SPY has 2 round trips today, but a new entry on QQQ is unaffected."""
    decl_qqq = [{
        "id": "intra-sid",
        "compute": "compute_3bar_low_intraday",
        "bar_interval": "15m",
        "active_on": ["SPY", "QQQ"],
        "grace_period": True,
    }]
    _patch_tracked(monkeypatch, decl_qqq)
    conn = db.init_db()
    for i in range(2):
        _insert_intraday_trade(conn, symbol="SPY", side="buy",
                                filled_at=f"2026-05-14T1{i}:00:00",
                                order_id=f"b{i}")
        _insert_intraday_trade(conn, symbol="SPY", side="sell",
                                filled_at=f"2026-05-14T1{i+1}:00:00",
                                order_id=f"s{i}")
    db.record_signal(conn, strategy_id="intra-sid", symbol="QQQ",
                      bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
                      close=50.0, bar_interval="15m")
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        bar_interval="15m", settings=_settings(),
    )
    actions = res["actions"]
    qqq_actions = [a for a in actions if a.get("symbol") == "QQQ"]
    assert qqq_actions
    assert all(a.get("action") != "SKIP_INTRADAY_SYMBOL_CAP"
                for a in qqq_actions), qqq_actions


def test_auto_trader_eod_not_affected(isolated_db, monkeypatch):
    """SPY has 2 intraday round trips today; EOD signal on SPY still fires."""
    eod_decl = [{
        "id": "eod-sid",
        "compute": "compute_3bar_low",
        "active_on": ["SPY"],
        "grace_period": True,
    }]
    _patch_tracked(monkeypatch, eod_decl)
    conn = db.init_db()
    for i in range(2):
        _insert_intraday_trade(conn, symbol="SPY", side="buy",
                                filled_at=f"2026-05-14T1{i}:00:00",
                                order_id=f"b{i}")
        _insert_intraday_trade(conn, symbol="SPY", side="sell",
                                filled_at=f"2026-05-14T1{i+1}:00:00",
                                order_id=f"s{i}")
    db.upsert_strategy(conn, {"extra": {"strategy_id": "eod-sid"}})
    db.record_signal(conn, strategy_id="eod-sid", symbol="SPY",
                      bar_ts="2026-05-14", signal_type="long_entry",
                      close=50.0, bar_interval="1d")
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=_settings(),
    )
    actions = res["actions"]
    assert all(a.get("action") != "SKIP_INTRADAY_SYMBOL_CAP"
                for a in actions), actions


def test_auto_trader_cap_setting_override(isolated_db, monkeypatch):
    """max_intraday_round_trips_per_symbol=5 → 2 round trips today not blocked."""
    _patch_tracked(monkeypatch, _INTRADAY_DECL)
    conn = db.init_db()
    for i in range(2):
        _insert_intraday_trade(conn, symbol="SPY", side="buy",
                                filled_at=f"2026-05-14T1{i}:00:00",
                                order_id=f"b{i}")
        _insert_intraday_trade(conn, symbol="SPY", side="sell",
                                filled_at=f"2026-05-14T1{i+1}:00:00",
                                order_id=f"s{i}")
    db.record_signal(conn, strategy_id="intra-sid", symbol="SPY",
                      bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
                      close=50.0, bar_interval="15m")
    s = _settings()
    s["max_intraday_round_trips_per_symbol"] = 5
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        bar_interval="15m", settings=s,
    )
    actions = res["actions"]
    assert all(a.get("action") != "SKIP_INTRADAY_SYMBOL_CAP"
                for a in actions), actions
