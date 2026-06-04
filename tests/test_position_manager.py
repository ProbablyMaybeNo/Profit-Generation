"""
test_position_manager.py — Sprint 2 / M1: single per-symbol order/position
reservation layer.

These tests fail on the pre-M1 code path (which submitted the full requested
qty straight to the broker via _submit_market_order, overselling past flat and
stacking conflicting exits). They pass once every sell/stop/flatten path routes
through monitoring.position_manager.

Coverage:
  (a) held_for_orders / qty_available reserves shares → manager submits only
      `available`, never oversells past flat.
  (b) a second strategy's exit for an already-exiting symbol (shares fully
      reserved by a working SELL) produces NO duplicate/conflicting order.
  (c) a long position is never flipped short (cap_sell_qty + safe_submit_sell).
  Wiring: close_intraday_positions and auto_trader._execute exits actually call
      the reservation layer (a fix not on the real call path is dead in prod).
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import position_manager as pm  # noqa: E402


class FakeOrder:
    def __init__(self, *, id="o1", symbol="NVDA", side="sell", qty=10,
                 filled_qty=0, status="accepted"):
        self.id = id
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.filled_qty = filled_qty
        self.status = status


class FakePosition:
    def __init__(self, *, symbol="NVDA", qty=10, qty_available=None):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty_available


class FakeBroker:
    """Minimal alpaca-shaped client for the manager. Records submitted orders
    and exposes positions + open orders the manager reads."""

    def __init__(self, *, positions=None, open_orders=None):
        self._positions = {p.symbol: p for p in (positions or [])}
        self._open_orders = list(open_orders or [])
        self.submitted = []
        self.cancelled = []

    def get_open_position(self, symbol):
        if symbol not in self._positions:
            raise Exception("position does not exist")  # alpaca 404 shape
        return self._positions[symbol]

    def get_all_positions(self):
        return list(self._positions.values())

    def get_orders(self, filter=None):
        return list(self._open_orders)

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
        self._open_orders = [o for o in self._open_orders if o.id != oid]


def _submit_fn(client, *, symbol, qty, side):
    order = FakeOrder(id=f"new-{symbol}", symbol=symbol, side=side, qty=qty,
                      status="accepted")
    client.submitted.append({"symbol": symbol, "qty": qty, "side": side})
    return order


# --- cap_sell_qty (pure) -----------------------------------------------------

def test_cap_sell_never_exceeds_available():
    assert pm.cap_sell_qty(10, 4) == 4
    assert pm.cap_sell_qty(3, 10) == 3


def test_cap_sell_zero_when_flat_or_short():
    assert pm.cap_sell_qty(10, 0) == 0
    assert pm.cap_sell_qty(10, -5) == 0  # already short → never sell more


# --- available_to_sell honours reservation ---------------------------------

def test_available_uses_broker_qty_available():
    # broker says 10 held, only 3 free (7 reserved by a resting stop).
    broker = FakeBroker(positions=[FakePosition(qty=10, qty_available=3)])
    assert pm.available_to_sell(broker, "NVDA") == 3


def test_available_derived_from_open_sell_orders_when_no_qty_available():
    # No broker qty_available; a working SELL for 7 reserves 7 of the 10 long.
    broker = FakeBroker(
        positions=[FakePosition(qty=10, qty_available=None)],
        open_orders=[FakeOrder(side="sell", qty=7, status="accepted")],
    )
    assert pm.available_to_sell(broker, "NVDA") == 3


def test_available_zero_when_short():
    broker = FakeBroker(positions=[FakePosition(qty=-50, qty_available=0)])
    assert pm.available_to_sell(broker, "NVDA") == 0


def test_available_zero_when_flat():
    broker = FakeBroker(positions=[])
    assert pm.available_to_sell(broker, "NVDA") == 0


def test_available_unknown_when_client_cannot_read_positions():
    # A stub client with no position getters → unknown (None), so the flatten
    # falls back to the requested qty rather than being silently blocked.
    class Stub:
        pass
    assert pm.available_to_sell(Stub(), "NVDA") is None


def test_safe_submit_falls_back_to_requested_when_unknown():
    class Stub:
        def __init__(self):
            self.submitted = []
    stub = Stub()

    def submit(client, *, symbol, qty, side):
        client.submitted.append({"symbol": symbol, "qty": qty, "side": side})
        return FakeOrder(symbol=symbol, qty=qty, side=side)

    res = pm.safe_submit_sell(stub, symbol="NVDA", requested_qty=7,
                              submit_fn=submit)
    assert res["action"] == "SUBMITTED"
    assert res["qty"] == 7
    assert stub.submitted == [{"symbol": "NVDA", "qty": 7, "side": "sell"}]


# --- (a) never oversell past flat -------------------------------------------

def test_safe_submit_caps_to_available_not_requested():
    # Strategy wants to sell 10, but only 3 are available → submit 3, not 10.
    broker = FakeBroker(positions=[FakePosition(qty=10, qty_available=3)])
    res = pm.safe_submit_sell(broker, symbol="NVDA", requested_qty=10,
                              submit_fn=_submit_fn)
    assert res["action"] == "SUBMITTED"
    assert res["qty"] == 3
    assert broker.submitted == [{"symbol": "NVDA", "qty": 3, "side": "sell"}]


# --- (b) second strategy's exit on an already-exiting symbol → no dup --------

def test_second_exit_on_fully_reserved_symbol_submits_nothing():
    # First strategy already has a working SELL for the entire 10-share long.
    # Broker reports 0 available. A second strategy's exit must NOT fire a
    # duplicate/conflicting SELL (the wash-trade + oversell root cause).
    broker = FakeBroker(
        positions=[FakePosition(qty=10, qty_available=0)],
        open_orders=[FakeOrder(id="resting", side="sell", qty=10,
                               status="accepted")],
    )
    res = pm.safe_submit_sell(broker, symbol="NVDA", requested_qty=10,
                              submit_fn=_submit_fn, reconcile=False)
    assert res["action"] == "SKIP_NO_AVAILABLE_QTY"
    assert res["qty"] == 0
    assert broker.submitted == []


# --- (c) long never flipped short -------------------------------------------

def test_long_never_flipped_short():
    # Hold 5 long; a buggy path requests 12. Cap to 5 — never sell into a short.
    broker = FakeBroker(positions=[FakePosition(qty=5, qty_available=5)])
    res = pm.safe_submit_sell(broker, symbol="NVDA", requested_qty=12,
                              submit_fn=_submit_fn)
    assert res["qty"] == 5
    assert broker.submitted[0]["qty"] == 5


def test_short_position_sell_submits_nothing():
    # Already short → a SELL would deepen the short. Must submit nothing.
    broker = FakeBroker(positions=[FakePosition(symbol="AAPL", qty=-30,
                                                qty_available=0)])
    res = pm.safe_submit_sell(broker, symbol="AAPL", requested_qty=30,
                              submit_fn=_submit_fn)
    assert res["action"] == "SKIP_NO_AVAILABLE_QTY"
    assert broker.submitted == []


# --- reconcile cancels conflicting resting sells ----------------------------

def test_reconcile_cancels_resting_sells():
    broker = FakeBroker(
        positions=[FakePosition(qty=10, qty_available=10)],
        open_orders=[FakeOrder(id="stop1", side="sell", qty=10,
                               status="accepted")],
    )
    n = pm.reconcile_exit_orders(broker, "NVDA")
    assert n == 1
    assert broker.cancelled == ["stop1"]


# --- buy-to-cover (M2 dependency) -------------------------------------------

def test_buy_to_cover_only_when_short():
    short = FakeBroker(positions=[FakePosition(symbol="META", qty=-40,
                                               qty_available=0)])
    res = pm.safe_submit_buy_to_cover(short, symbol="META", submit_fn=_submit_fn)
    assert res["action"] == "COVERED"
    assert res["qty"] == 40
    assert short.submitted == [{"symbol": "META", "qty": 40, "side": "buy"}]


def test_buy_to_cover_noop_on_long():
    longp = FakeBroker(positions=[FakePosition(symbol="META", qty=40,
                                               qty_available=40)])
    res = pm.safe_submit_buy_to_cover(longp, symbol="META", submit_fn=_submit_fn)
    assert res["action"] == "SKIP_NOT_SHORT"
    assert longp.submitted == []


# --- WIRING: close_intraday_positions routes through the manager ------------

def test_close_intraday_wired_to_position_manager(tmp_path, monkeypatch):
    from data import db
    from monitoring import close_intraday_positions as ci

    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    conn = db.init_db(test_db)
    monkeypatch.setattr(ci, "is_paper_mode", lambda: True)

    sid, sym = "intraday-1m-orb", "NVDA"
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    sig_id = db.record_signal(conn, strategy_id=sid, symbol=sym,
                              bar_ts="2026-06-04T15:57:00",
                              signal_type="long_entry", close=100.0,
                              bar_interval="1m")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at, fill_price) "
        "VALUES (?, ?, ?, ?, 'buy', 10, ?, 'filled', ?, 100.0)",
        ("b1", sig_id, sid, sym, "2026-06-04T15:57:00",
         "2026-06-04T15:57:00"),
    )
    conn.commit()

    # Broker says only 3 of the 10 are available (7 reserved). The pre-M1 code
    # sold the full DB qty (10) → oversell. M1 must cap to 3.
    broker = FakeBroker(positions=[FakePosition(symbol=sym, qty=10,
                                                qty_available=3)])

    result = ci.close_intraday_positions(
        conn=conn, dry_run=False, client=broker,
        submit_market_order_fn=_submit_fn,
        cancel_open_orders_fn=lambda c, s: 0,
        settle_seconds=0,
    )
    assert result["status"] == "OK"
    assert broker.submitted == [{"symbol": sym, "qty": 3, "side": "sell"}]
    conn.close()


# --- WIRING: auto_trader exit routes through the manager --------------------

def test_auto_trader_exit_wired_to_position_manager(tmp_path, monkeypatch):
    from data import db
    from monitoring import auto_trader as at

    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    conn = db.init_db(test_db)

    sid, sym = "trend-donchian-breakout-20", "AAPL"
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    entry_sig = db.record_signal(conn, strategy_id=sid, symbol=sym,
                                 bar_ts="2026-06-03T20:00:00",
                                 signal_type="long_entry", close=200.0,
                                 bar_interval="1d")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'buy', 10, ?, 'filled', ?)",
        ("b1", entry_sig, sid, sym, "2026-06-03T20:00:00",
         "2026-06-03T20:00:00"),
    )
    exit_sig = db.record_signal(conn, strategy_id=sid, symbol=sym,
                                bar_ts="2026-06-04T20:00:00",
                                signal_type="long_exit", close=205.0,
                                bar_interval="1d")
    conn.commit()
    sig_row = conn.execute("SELECT * FROM signals WHERE id=?",
                           (exit_sig,)).fetchone()

    # Broker reports only 4 available of the 10-share DB position. Pre-M1 sold
    # 10 (oversell); M1 caps to 4.
    broker = FakeBroker(positions=[FakePosition(symbol=sym, qty=10,
                                                qty_available=4)])
    monkeypatch.setattr(at, "_submit_market_order", _submit_fn)

    res = at._process_exit(conn, broker, {}, sig_row, False)
    assert res["action"] not in ("ERROR",)
    assert broker.submitted == [{"symbol": sym, "qty": 4, "side": "sell"}]
    conn.close()
