"""test_broker_truth_m1.py — Sprint 3 / M1: broker is the single source of truth.

These tests drive the REAL production exit path (auto_trader._process_exit) with
a broker fake that models the live broker invariant the unit-level Sprint-2
fakes did NOT: a submitted SELL *reserves* shares (reduces qty_available), and an
accepted-but-unfilled SELL keeps the position qty unchanged while holding the
shares. This is exactly the live condition under which two strategies sharing one
broker symbol oversold past flat into a SHORT.

M1 acceptance (prod path): when the DB thinks a strategy owns qty the broker
does NOT have available, the system must trust the BROKER and submit only the
broker-available quantity — never the stale DB qty, never crossing zero short.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402


class ReservingOrder:
    def __init__(self, symbol, qty, side, oid):
        self.id = oid
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.filled_qty = 0
        self.status = "accepted"  # working, not yet filled — shares are HELD


class ReservingPos:
    def __init__(self, symbol, qty, qty_available):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty_available


class ReservingBroker:
    """Broker fake that honors the live held_for_orders invariant.

    A working SELL reserves shares: it leaves position.qty unchanged (the sell
    hasn't filled) but reduces qty_available by the order qty. This is the exact
    live state in which a second strategy's exit must NOT be able to oversell the
    shares the first strategy's exit already reserved.
    """

    def __init__(self, symbol, qty):
        self._sym = symbol
        self._qty = float(qty)
        self._reserved = 0.0
        self._orders = []
        self.submitted = []
        self._n = 0

    def _available(self):
        return max(0.0, self._qty - self._reserved)

    def get_open_position(self, symbol):
        if symbol != self._sym or self._qty == 0:
            raise Exception("position does not exist")
        return ReservingPos(symbol, self._qty, self._available())

    def get_all_positions(self):
        if self._qty == 0:
            return []
        return [ReservingPos(self._sym, self._qty, self._available())]

    def get_orders(self, filter=None):
        return list(self._orders)

    def cancel_order_by_id(self, oid):
        before = len(self._orders)
        self._orders = [o for o in self._orders if o.id != oid]
        freed = before - len(self._orders)
        # Releasing a working SELL frees its reserved shares.
        for o in list(self.submitted):
            pass
        # Re-derive reservation from surviving working orders.
        self._reserved = sum(o.qty for o in self._orders)

    def submit_order(self, req):
        # alpaca request objects expose .symbol/.qty/.side; our fake submit_fn
        # path calls this through _submit_market_order, but tests patch that.
        raise NotImplementedError

    def place_sell(self, symbol, qty):
        self._n += 1
        oid = f"sell-{symbol}-{self._n}"
        order = ReservingOrder(symbol, qty, "sell", oid)
        self._orders.append(order)
        self._reserved += qty
        self.submitted.append({"symbol": symbol, "qty": qty, "side": "sell"})
        return order


def _submit(client, *, symbol, qty, side, client_order_id=None):
    # Routes through the broker fake so a SELL reserves shares the way live does.
    if side == "sell":
        return client.place_sell(symbol, qty)
    raise AssertionError(f"unexpected {side} submit in M1 exit test")


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    monkeypatch.setattr(at, "_submit_market_order", _submit)
    # Each test models ONE trading pass — start from a clean reservation ledger.
    pm.reset_run_reservations()
    yield c
    pm.reset_run_reservations()
    c.close()


def _seed_open_buy(conn, sid, sym, qty, *, ts):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    buy_sig = db.record_signal(conn, strategy_id=sid, symbol=sym,
                               bar_ts=ts, signal_type="long_entry",
                               close=100.0, bar_interval="1d")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?)",
        (f"b-{sid}", buy_sig, sid, sym, qty, ts, ts),
    )
    conn.commit()
    return buy_sig


def _exit_sig(conn, sid, sym, *, ts):
    sig_id = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=ts,
                              signal_type="long_exit", close=105.0,
                              bar_interval="1d")
    return conn.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()


def test_db_overstates_qty_system_trusts_broker(conn):
    """DB says strategy holds 10; the broker only actually holds 4 long. The
    exit must submit at most 4 (broker truth), never the stale DB 10 — which
    would oversell 6 shares past flat into a short."""
    sid, sym = "trend-donchian-breakout-20", "IWM"
    _seed_open_buy(conn, sid, sym, 10, ts="2026-06-04T20:00:00")
    # Broker reality: only 4 long, all 4 available. DB is stale/overstated.
    broker = ReservingBroker(sym, 4)

    sig = _exit_sig(conn, sid, sym, ts="2026-06-05T20:00:00")
    at._process_exit(conn, broker, {}, sig, False)

    submitted_sell = [s for s in broker.submitted if s["side"] == "sell"]
    total = sum(s["qty"] for s in submitted_sell)
    assert total <= 4, (
        f"trusted stale DB over broker: submitted {total} of 4 real shares "
        f"(overselling {total - 4} past flat into a short)")
    assert total >= 1, "should still sell the 4 broker-real shares"


def test_two_strategies_shared_symbol_never_oversell_short(conn):
    """IWM owned by TWO strategies (DB qty 10 each = 20 claimed) but the broker
    holds exactly 10. Driving BOTH real exits in sequence must sell at most 10
    total and never cross zero into a short."""
    sym = "IWM"
    sidA = "intraday-orb-pivots-5m"
    sidB = "intraday-orbo-5m"
    _seed_open_buy(conn, sidA, sym, 10, ts="2026-06-04T20:00:00")
    _seed_open_buy(conn, sidB, sym, 10, ts="2026-06-04T20:00:01")
    broker = ReservingBroker(sym, 10)

    sigA = _exit_sig(conn, sidA, sym, ts="2026-06-05T20:00:00")
    at._process_exit(conn, broker, {}, sigA, False)
    sigB = _exit_sig(conn, sidB, sym, ts="2026-06-05T20:00:01")
    at._process_exit(conn, broker, {}, sigB, False)

    total = sum(s["qty"] for s in broker.submitted if s["side"] == "sell")
    assert total <= 10, (
        f"two strategies oversold shared IWM: {total} sold of 10 held "
        f"(this is the −$101k unintended-short root cause)")
