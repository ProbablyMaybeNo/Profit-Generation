"""test_idempotent_stop_m3.py — Sprint 3 / M3: idempotent stop / flatten / sell.

The remaining 40310000 wash-trade source (per M1's handoff): `stops.submit_atr_stop`
blindly submitted a NEW SELL STOP every call, so re-arming a symbol that already
had a resting stop STACKED a second SELL STOP — Alpaca rejects it as a potential
wash trade, or both reserve shares so the later flatten sees held_for_orders==qty
and fails "insufficient qty available".

These tests drive the REAL production stop path (auto_trader._maybe_attach_stop)
and prove a re-arm now CANCELS/REPLACES the resting stop and submits only the
net-available quantity — never a second stacked stop, never an oversell. They
FAIL on pre-M3 code (which stacked a second stop and never cancelled the first).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402


class _StopOrder:
    def __init__(self, oid, symbol, qty):
        self.id = oid
        self.symbol = symbol
        self.qty = qty
        self.side = "sell"
        self.filled_qty = 0
        self.status = "accepted"  # working SELL STOP, reserves shares
        self.submitted_at = "2026-06-05T14:30:00Z"


class _Pos:
    def __init__(self, symbol, qty, qty_available):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty_available


class StopBroker:
    """Models the live held_for_orders invariant for SELL STOPs.

    A working SELL STOP reserves shares (reduces qty_available). Re-deriving the
    reservation from surviving working orders means a cancel frees its shares —
    exactly the broker behaviour M3 relies on for cancel/replace.
    """

    def __init__(self, symbol, qty):
        self._sym = symbol
        self._qty = float(qty)
        self._orders = []
        self.submitted = []
        self.cancelled = []
        self._n = 0

    def _reserved(self):
        return sum(o.qty for o in self._orders)

    def get_open_position(self, symbol):
        if symbol != self._sym or self._qty == 0:
            raise Exception("position does not exist")
        avail = max(0.0, self._qty - self._reserved())
        return _Pos(symbol, self._qty, avail)

    def get_all_positions(self):
        avail = max(0.0, self._qty - self._reserved())
        return [_Pos(self._sym, self._qty, avail)]

    def get_orders(self, filter=None):
        return list(self._orders)

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
        self._orders = [o for o in self._orders if o.id != oid]

    def submit_order(self, req):
        # stops.submit_atr_stop builds a StopOrderRequest; pull qty/symbol off it.
        self._n += 1
        qty = int(getattr(req, "qty", 0))
        oid = f"stop-{self._sym}-{self._n}"
        o = _StopOrder(oid, self._sym, qty)
        self._orders.append(o)
        self.submitted.append({"id": oid, "qty": qty})
        return o


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    pm.reset_run_reservations()
    yield c
    pm.reset_run_reservations()
    c.close()


def _settings():
    return {
        "enabled": True, "dry_run": False,
        "stops": {"atr_multiplier": 2.0, "fixed_percent_fallback": 0.05},
    }


def _bars(symbol):
    # Enough bars + range for compute_atr to yield a positive ATR.
    rows = []
    base = 100.0
    for i in range(30):
        rows.append({"high": base + 2, "low": base - 2, "close": base})
        base += 0.1
    return rows


def _seed_open_buy(conn, sid, sym, qty, *, ts):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    buy_sig = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=ts,
                               signal_type="long_entry", close=100.0,
                               bar_interval="1d")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?)",
        (f"b-{sid}-{sym}", buy_sig, sid, sym, qty, ts, ts),
    )
    conn.commit()
    return buy_sig


def _entry_sig(conn, sid, sym, *, ts):
    sig_id = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=ts,
                              signal_type="long_entry", close=110.0,
                              bar_interval="1d")
    return conn.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()


def test_re_arm_cancels_resting_stop_no_stack(conn):
    """Owner of NVDA arms a stop twice. The second arm must CANCEL the first
    resting stop and leave exactly ONE working SELL STOP — never two stacked
    (the 40310000 wash source). Pre-M3 left two resting stops, cancelled none."""
    sym = "NVDA"
    sid = "trend-donchian-breakout-20"
    _seed_open_buy(conn, sid, sym, 10, ts="2026-06-04T20:00:00")
    broker = StopBroker(sym, 10)
    sig = _entry_sig(conn, sid, sym, ts="2026-06-05T14:30:00")

    first = at._maybe_attach_stop(
        conn, broker, _settings(), sig, entry_fill=110.0, qty=10,
        client_order_id="ato-x-NVDA-b", bars_fetcher=_bars)
    assert first["status"] == "submitted", first
    assert len(broker._orders) == 1

    second = at._maybe_attach_stop(
        conn, broker, _settings(), sig, entry_fill=110.0, qty=10,
        client_order_id="ato-x-NVDA-b2", bars_fetcher=_bars)
    assert second["status"] == "submitted", second

    # The decisive M3 assertion: exactly ONE resting SELL STOP, and the first
    # was cancelled (cancel/replace, not stack).
    assert len(broker._orders) == 1, (
        f"stacked {len(broker._orders)} SELL STOPs on one long "
        f"(40310000 wash source)")
    assert broker.cancelled, "re-arm did not cancel the resting stop"


def test_stop_qty_capped_to_available_not_oversold(conn):
    """A resting stop already reserves 7 of 10 shares; re-arming for 10 must NOT
    place a stop for more than the broker can protect. After cancel/replace the
    full 10 is free again, so the replacement protects 10 — never >10."""
    sym = "QQQ"
    sid = "intraday-orb-pivots-5m"
    _seed_open_buy(conn, sid, sym, 10, ts="2026-06-04T20:00:00")
    broker = StopBroker(sym, 10)
    # Pre-existing resting stop reserving 7 shares.
    broker._orders.append(_StopOrder("pre-stop", sym, 7))
    sig = _entry_sig(conn, sid, sym, ts="2026-06-05T14:30:00")

    res = at._maybe_attach_stop(
        conn, broker, _settings(), sig, entry_fill=110.0, qty=10,
        client_order_id="ato-x-QQQ-b", bars_fetcher=_bars)
    assert res["status"] == "submitted", res
    assert len(broker._orders) == 1, "should have replaced, not stacked"
    # Replacement qty must never exceed the long position (no oversell/short).
    assert broker._orders[0].qty <= 10
    assert "pre-stop" in broker.cancelled
