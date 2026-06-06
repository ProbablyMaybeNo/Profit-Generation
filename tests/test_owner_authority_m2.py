"""test_owner_authority_m2.py — Sprint 3 / M2: single symbol-owner authority.

OPTION A (operator decision): the FIRST/priority strategy to hold a symbol OWNS
it. While the symbol is held, any OTHER strategy's ENTRY is REJECTED, and no
non-owner may submit an exit/stop/flatten for it. One broker position -> one
owner -> one exit/stop stack, so two strategies can never fight over the same
Alpaca position.

These tests drive the REAL production functions (auto_trader._process_entry,
._process_exit, ._maybe_attach_stop) with the multi-strategy shared-symbol
fixtures the bug actually used (IWM/KRE/NVDA/QQQ). They FAIL on pre-M2 code:
  * pre-M2 a second strategy's entry on an owned symbol SUBMITTED a buy;
  * pre-M2 a non-owner holding a legacy shared position SUBMITTED its own SELL.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402


# --- broker fake -----------------------------------------------------------

class _Order:
    def __init__(self, oid, symbol, qty, side):
        self.id = oid
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.filled_qty = 0
        self.status = "accepted"
        self.submitted_at = "2026-06-05T14:30:00Z"
        self.filled_avg_price = None


class _Pos:
    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty


class OwnerBroker:
    """Honours the live held_for_orders invariant and records every submit."""

    def __init__(self, holdings=None):
        # holdings: {symbol: qty_long}
        self._holdings = dict(holdings or {})
        self.submitted = []
        self._n = 0
        self._orders = []

    def get_open_position(self, symbol):
        q = self._holdings.get(symbol, 0)
        if q == 0:
            raise Exception("position does not exist")
        return _Pos(symbol, float(q))

    def get_all_positions(self):
        return [_Pos(s, float(q)) for s, q in self._holdings.items() if q]

    def get_orders(self, filter=None):
        return list(self._orders)

    def cancel_order_by_id(self, oid):
        self._orders = [o for o in self._orders if o.id != oid]

    def place(self, symbol, qty, side):
        self._n += 1
        o = _Order(f"{side}-{symbol}-{self._n}", symbol, qty, side)
        self.submitted.append({"symbol": symbol, "qty": qty, "side": side})
        if side == "sell":
            self._orders.append(o)
        return o


def _submit(client, *, symbol, qty, side, client_order_id=None):
    return client.place(symbol, qty, side)


# --- fixtures --------------------------------------------------------------

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
    return {
        "enabled": True, "dry_run": False,
        "min_outcomes": 1, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.0,
        "max_position_usd": 100000, "skip_intraday_signals": False,
    }


def _make_eligible(conn, sid):
    """Seed enough winning closed 1d outcomes that `sid` clears the edge gate."""
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    for i in range(3):
        s = db.record_signal(conn, strategy_id=sid, symbol="ELIG",
                             bar_ts=f"2024-01-0{i+1}", signal_type="long_entry",
                             close=100.0, bar_interval="1d")
        db.open_outcome(conn, signal_id=s, entry_ts=f"2024-01-0{i+1}",
                        entry_price=100.0)
        db.close_outcome(conn, signal_id=s, exit_ts=f"2024-01-0{i+2}",
                         exit_price=102.0, exit_reason="long_exit_signal",
                         bars_held=1)


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
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    sig_id = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=ts,
                              signal_type="long_entry", close=100.0,
                              bar_interval="1d")
    return conn.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()


def _exit_sig(conn, sid, sym, *, ts):
    sig_id = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=ts,
                              signal_type="long_exit", close=105.0,
                              bar_interval="1d")
    return conn.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()


# --- M2 acceptance: entry side --------------------------------------------

def test_second_strategy_entry_on_owned_symbol_rejected(conn):
    """KRE owned by strategy A. Strategy B's REAL entry on KRE must be REJECTED
    (no buy submitted) — OPTION A, one owner per symbol. Pre-M2 it submitted."""
    sym = "KRE"
    owner = "trend-donchian-breakout-20"
    intruder = "mean-rev-rsi2"
    _seed_open_buy(conn, owner, sym, 10, ts="2026-06-04T20:00:00")
    _make_eligible(conn, intruder)
    broker = OwnerBroker(holdings={sym: 10})

    sig = _entry_sig(conn, intruder, sym, ts="2026-06-05T14:30:00")
    action = at._process_entry(conn, broker, _settings(), sig, False,
                               portfolio_value=1_000_000.0)

    assert action["action"] == "SKIP_SYMBOL_OWNED", action
    assert action.get("owner") == owner
    assert not [s for s in broker.submitted if s["side"] == "buy"], (
        "non-owner entry submitted a buy on an owned symbol "
        "(pre-M2 shared-symbol dogpile)")


def test_owner_can_still_be_chosen_when_symbol_unowned(conn):
    """A genuinely flat symbol is unowned -> a fresh entry is allowed (no
    false-positive ownership block)."""
    sym = "QQQ"
    sid = "trend-donchian-breakout-20"
    _make_eligible(conn, sid)
    broker = OwnerBroker(holdings={})
    sig = _entry_sig(conn, sid, sym, ts="2026-06-05T14:30:00")
    action = at._process_entry(conn, broker, _settings(), sig, False,
                               portfolio_value=1_000_000.0)
    assert action["action"] == "BUY", action
    assert pm.symbol_owner(conn, sym) == sid


# --- M2 acceptance: exit side ---------------------------------------------

def test_non_owner_exit_on_shared_symbol_suppressed(conn):
    """Legacy state: IWM held by TWO strategies (A first, B second). Only the
    owner A may exit. B's REAL exit must be SKIP_NOT_OWNER — no second SELL
    against the one shared broker position. Pre-M2 both fired."""
    sym = "IWM"
    owner = "intraday-orb-pivots-5m"
    other = "intraday-orbo-5m"
    _seed_open_buy(conn, owner, sym, 10, ts="2026-06-04T20:00:00")
    _seed_open_buy(conn, other, sym, 10, ts="2026-06-04T20:00:05")
    broker = OwnerBroker(holdings={sym: 10})

    # Non-owner exit first: must be suppressed.
    sig_b = _exit_sig(conn, other, sym, ts="2026-06-05T20:00:00")
    act_b = at._process_exit(conn, broker, _settings(), sig_b, False)
    assert act_b["action"] == "SKIP_NOT_OWNER", act_b
    assert act_b.get("owner") == owner
    assert not broker.submitted, "non-owner exit fired a SELL on a shared symbol"

    # Owner exit: the single valid exit fires.
    sig_a = _exit_sig(conn, owner, sym, ts="2026-06-05T20:00:01")
    act_a = at._process_exit(conn, broker, _settings(), sig_a, False)
    assert act_a["action"] == "SELL", act_a
    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1, f"exactly one valid exit stack expected, got {sells}"
    assert sum(s["qty"] for s in sells) <= 10


def test_owner_exit_allowed(conn):
    """The sole owner's exit fires normally (no false-positive owner block)."""
    sym = "NVDA"
    sid = "intraday-orb-pivots-5m"
    _seed_open_buy(conn, sid, sym, 8, ts="2026-06-04T20:00:00")
    broker = OwnerBroker(holdings={sym: 8})
    sig = _exit_sig(conn, sid, sym, ts="2026-06-05T20:00:00")
    act = at._process_exit(conn, broker, _settings(), sig, False)
    assert act["action"] == "SELL", act
    assert sum(s["qty"] for s in broker.submitted if s["side"] == "sell") <= 8


# --- M2 owner-registry unit checks (derivation across stateless runs) ------

def test_symbol_owner_is_oldest_open_buy(conn):
    sym = "IWM"
    _seed_open_buy(conn, "first", sym, 5, ts="2026-06-04T20:00:00")
    _seed_open_buy(conn, "second", sym, 5, ts="2026-06-04T20:00:30")
    assert pm.symbol_owner(conn, sym) == "first"
    assert pm.owns_symbol(conn, "first", sym)
    assert not pm.owns_symbol(conn, "second", sym)


def test_ownership_released_when_owner_flat(conn):
    sym = "KRE"
    _seed_open_buy(conn, "first", sym, 5, ts="2026-06-04T20:00:00")
    # Owner sells out -> flat -> ownership released.
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, strategy_id, symbol, side, qty, status, submitted_at) "
        "VALUES ('s1', 'first', ?, 'sell', 5, 'filled', '2026-06-05T20:00:00')",
        (sym,),
    )
    conn.commit()
    assert pm.symbol_owner(conn, sym) is None
    assert pm.entry_owner_conflict(conn, "second", sym) is None
