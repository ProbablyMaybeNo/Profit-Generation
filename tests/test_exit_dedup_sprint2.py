"""test_exit_dedup_sprint2.py — Sprint 2 / M5 exit-signal de-duplication.

Proves a redundant exit signal is suppressed when a SELL exit for the same
(strategy, symbol) is already accepted/working — no duplicate order, no skip
row — while the genuine FIRST exit still fires. This fails on pre-M5 code,
which submitted a second SELL for the already-exiting pair.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


class FakePos:
    def __init__(self, symbol, qty, qty_available=None):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty_available if qty_available is not None else qty


class FakeOrder:
    def __init__(self, symbol, qty, side):
        self.id = f"o-{symbol}-{side}"
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.status = "accepted"


class FakeBroker:
    def __init__(self, positions):
        self._positions = {p.symbol: p for p in positions}
        self.submitted = []

    def get_open_position(self, symbol):
        if symbol not in self._positions:
            raise Exception("position does not exist")
        return self._positions[symbol]

    def get_all_positions(self):
        return list(self._positions.values())

    def get_orders(self, filter=None):
        return []

    def cancel_order_by_id(self, oid):
        pass


def _submit(client, *, symbol, qty, side):
    client.submitted.append({"symbol": symbol, "qty": qty, "side": side})
    return FakeOrder(symbol, qty, side)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    monkeypatch.setattr(at, "_submit_market_order", _submit)
    yield c
    c.close()


def _seed_open_position(conn, sid, sym, qty=10):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    buy_sig = db.record_signal(conn, strategy_id=sid, symbol=sym,
                               bar_ts="2026-06-03T20:00:00",
                               signal_type="long_entry", close=200.0,
                               bar_interval="1d")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?)",
        ("b1", buy_sig, sid, sym, qty, "2026-06-03T20:00:00",
         "2026-06-03T20:00:00"),
    )
    conn.commit()
    return buy_sig


def test_first_exit_fires(conn):
    sid, sym = "trend-donchian-breakout-20", "AAPL"
    _seed_open_position(conn, sid, sym, qty=10)
    exit_sig = db.record_signal(conn, strategy_id=sid, symbol=sym,
                                bar_ts="2026-06-04T20:00:00",
                                signal_type="long_exit", close=210.0,
                                bar_interval="1d")
    sig_row = conn.execute("SELECT * FROM signals WHERE id=?",
                           (exit_sig,)).fetchone()
    broker = FakeBroker([FakePos(sym, 10)])
    res = at._process_exit(conn, broker, {}, sig_row, False)
    assert res["action"] != "SKIP_EXIT_ALREADY_WORKING"
    assert broker.submitted == [{"symbol": sym, "qty": 10, "side": "sell"}]


def test_redundant_exit_suppressed_while_resting_stop_works(conn):
    # Real gap: a protective SELL STOP placed at ENTRY time (submitted_at == buy
    # time, not strictly later) leaves the position "open" to _open_buy_for_pair,
    # so an exit signal would fire a redundant market SELL on top of the resting
    # stop → Alpaca 40310000 wash-trade reject. M5 suppresses it.
    sid, sym = "trend-donchian-breakout-20", "AAPL"
    _seed_open_position(conn, sid, sym, qty=10)
    # Resting protective stop, submitted at the SAME ts as the entry buy.
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, strategy_id, symbol, side, qty, order_type, "
        " stop_price, status, submitted_at) "
        "VALUES ('stop1', ?, ?, 'sell', 10, 'stop', 180.0, 'accepted', ?)",
        (sid, sym, "2026-06-03T20:00:00"),
    )
    conn.commit()

    # An exit signal fires for the same pair while the stop is resting.
    exit_sig = db.record_signal(conn, strategy_id=sid, symbol=sym,
                                bar_ts="2026-06-04T20:01:00",
                                signal_type="long_exit", close=210.5,
                                bar_interval="1d")
    sig_row = conn.execute("SELECT * FROM signals WHERE id=?",
                           (exit_sig,)).fetchone()
    broker = FakeBroker([FakePos(sym, 10)])
    res = at._process_exit(conn, broker, {}, sig_row, False)

    assert res["action"] == "SKIP_EXIT_ALREADY_WORKING"
    assert broker.submitted == []  # no duplicate/conflicting order on the stop


def test_working_exit_detector(conn):
    sid, sym = "s1", "MSFT"
    _seed_open_position(conn, sid, sym)
    assert at._exit_already_working_for_pair(conn, sid, sym) is False
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, strategy_id, symbol, side, qty, status, submitted_at) "
        "VALUES ('s9', ?, ?, 'sell', 10, 'accepted', '2026-06-04T20:00:05')",
        (sid, sym),
    )
    conn.commit()
    assert at._exit_already_working_for_pair(conn, sid, sym) is True
