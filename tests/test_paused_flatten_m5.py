"""test_paused_flatten_m5.py — Sprint 3 / M5: paused-strategy position policy.

Pause must mean BOTH "no new entries" AND "no silent holding". The entry gate
already refuses entries; M5 closes the other half — on pause, a strategy's
still-owned holdings are FLATTENED via the owner authority, and no new
stop-arming happens for them.

These tests drive the REAL production path (auto_trader.process_signals in
non-dry-run mode with an injected client) and prove a paused strategy with a
holding gets a real SELL submitted that flattens it. FAILS on pre-M5 code, which
left the paused strategy silently holding.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402


class _Order:
    def __init__(self, oid, symbol, qty, side):
        self.id = oid
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.filled_qty = 0
        self.status = "accepted"
        self.submitted_at = "2026-05-14T20:00:00Z"


class _Pos:
    def __init__(self, symbol, qty, qty_available):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty_available


class FlattenBroker:
    def __init__(self, holdings):
        self._holdings = dict(holdings)
        self._orders = []
        self.submitted = []
        self._n = 0

    def _reserved(self, sym):
        return sum(o.qty for o in self._orders if o.symbol == sym)

    def get_open_position(self, symbol):
        q = self._holdings.get(symbol, 0)
        if not q:
            raise Exception("position does not exist")
        return _Pos(symbol, float(q), max(0.0, float(q) - self._reserved(symbol)))

    def get_all_positions(self):
        return [_Pos(s, float(q), max(0.0, float(q) - self._reserved(s)))
                for s, q in self._holdings.items() if q]

    def get_orders(self, filter=None):
        return list(self._orders)

    def cancel_order_by_id(self, oid):
        self._orders = [o for o in self._orders if o.id != oid]

    def place(self, symbol, qty, side):
        self._n += 1
        o = _Order(f"{side}-{symbol}-{self._n}", symbol, qty, side)
        if side == "sell":
            self._orders.append(o)
        self.submitted.append({"symbol": symbol, "qty": qty, "side": side})
        return o


def _submit(client, *, symbol, qty, side, client_order_id=None):
    return client.place(symbol, qty, side)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    monkeypatch.setattr(at, "_submit_market_order", _submit)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    pm.reset_run_reservations()
    yield c
    pm.reset_run_reservations()
    c.close()


def _settings():
    return {"enabled": True, "dry_run": False, "max_position_usd": 100000}


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


def test_paused_strategy_holding_is_flattened(conn):
    """A paused strategy still owns KRE. process_signals must flatten it via a
    real SELL — pause = no silent holding. Pre-M5 it kept holding."""
    sid = "mean-rev-rsi2"
    sym = "KRE"
    _seed_open_buy(conn, sid, sym, 10, ts="2026-05-13T20:00:00")
    sh.pause_strategy(conn, sid, reason="divergence", pause_days=30)
    broker = FlattenBroker({sym: 10})

    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=_settings(),
        client=broker,
    )

    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert sells, "paused strategy was not flattened (silent holding)"
    assert sum(s["qty"] for s in sells) <= 10  # never oversell past flat
    flat_actions = [a for a in res["actions"]
                    if a.get("action") == "PAUSE_FLATTEN"]
    assert flat_actions and flat_actions[0]["symbol"] == sym


def test_non_paused_holding_not_flattened(conn):
    """A live (non-paused) strategy's holding is NOT touched by the pause pass."""
    sid = "trend-donchian-breakout-20"
    sym = "NVDA"
    _seed_open_buy(conn, sid, sym, 5, ts="2026-05-13T20:00:00")
    broker = FlattenBroker({sym: 5})

    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=_settings(), client=broker,
    )
    assert not [s for s in broker.submitted if s["side"] == "sell"], (
        "flattened a live strategy's holding")


def test_paused_flatten_then_no_new_stop_arming(conn):
    """After the pause-flatten sells out the position, the symbol is no longer
    owned, so a subsequent stop-arm attempt is suppressed (owner gate)."""
    sid = "mean-rev-rsi2"
    sym = "IWM"
    _seed_open_buy(conn, sid, sym, 10, ts="2026-05-13T20:00:00")
    sh.pause_strategy(conn, sid, reason="divergence", pause_days=30)
    broker = FlattenBroker({sym: 10})

    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=_settings(), client=broker,
    )
    # The flatten recorded a closing market SELL. Ownership is now released
    # (a market sell after the buy closes the position).
    assert pm.symbol_owner(conn, sym) is None
