"""test_eod_flat_assert_m6.py — Sprint 3 / M6: end-of-session flat assertion.

Root cause of `stale_intraday_flatten_missed`: F2 lets ONLY the EOD flatten close
an intraday outcome; if that flatten is missed or the broker rejected/partial-
filled the SELL, the position survives overnight and the outcome strands OPEN
until a LATER session's sweep closes it with the stale tag — silently. M6 adds
the missing assertion: after the flatten pass, verify every intraday symbol is
actually FLAT at the broker, and ALERT LOUDLY when one isn't.

These tests drive the REAL production close-out (close_intraday_positions) and
prove the assertion trips on an unflattened position and is silent on a clean
session. FAILS on pre-M6 code, which had no flat assertion at all.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import close_intraday_positions as ci  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402


class _Pos:
    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty


class _Order:
    def __init__(self, oid):
        self.id = oid
        self.status = "accepted"
        self.submitted_at = "2026-05-14T16:00:00Z"


class AssertBroker:
    """Broker whose post-flatten position view is controllable per symbol, so a
    test can model a SELL that did NOT actually flatten the broker (rejected /
    partial / missed) and verify the assertion catches it."""

    def __init__(self, post_flatten_qty):
        # post_flatten_qty: {symbol: qty still held AFTER the flatten}
        self._post = dict(post_flatten_qty)

    def get_open_position(self, symbol):
        q = self._post.get(symbol, 0)
        if not q:
            raise Exception("position does not exist")
        return _Pos(symbol, float(q))

    def get_all_positions(self):
        return [_Pos(s, float(q)) for s, q in self._post.items() if q]

    def get_orders(self, filter=None):
        return []

    def cancel_order_by_id(self, oid):
        return None


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(ci, "is_paper_mode", lambda: True)
    pm.reset_run_reservations()
    yield test_db
    pm.reset_run_reservations()


def _seed_open_buy(conn, *, strategy_id, symbol, qty, order_id):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    sig_id = db.record_signal(conn, strategy_id=strategy_id, symbol=symbol,
                              bar_ts="2026-05-14T14:30:00",
                              signal_type="long_entry", close=100.0,
                              bar_interval="15m")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?)",
        (order_id, sig_id, strategy_id, symbol, qty,
         "2026-05-14T14:30:00", "2026-05-14T14:30:00"),
    )
    conn.commit()
    return sig_id


def _submit(client, symbol, qty, side):
    return _Order(f"close-{symbol}")


def test_unflattened_position_trips_assertion(isolated_db):
    """The flatten SELL didn't actually flatten the broker (still 7 held). The
    EOD flat assertion must catch it and fire a loud alert."""
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="QQQ", qty=7,
                   order_id="b-QQQ")
    # Broker still shows 7 held AFTER the flatten (missed/rejected SELL).
    broker = AssertBroker({"QQQ": 7})
    alerts = []
    res = ci.close_intraday_positions(
        conn=conn, dry_run=False, client=broker,
        submit_market_order_fn=_submit,
        flat_assert_alert_fn=lambda msg: alerts.append(msg),
    )
    fa = res["flat_assert"]
    assert fa["still_open"] == [{"symbol": "QQQ", "qty": 7.0}], fa
    assert fa["alerted"] is True
    assert alerts and "flat assertion FAILED" in alerts[0]
    assert "QQQ" in alerts[0]


def test_clean_session_is_silent(isolated_db):
    """Every intraday symbol is flat after the close-out → no alert."""
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY", qty=5,
                   order_id="b-SPY")
    # Broker shows SPY flat after flatten (position gone).
    broker = AssertBroker({})
    alerts = []
    res = ci.close_intraday_positions(
        conn=conn, dry_run=False, client=broker,
        submit_market_order_fn=_submit,
        flat_assert_alert_fn=lambda msg: alerts.append(msg),
    )
    fa = res["flat_assert"]
    assert fa["still_open"] == [], fa
    assert fa["alerted"] is False
    assert alerts == []


def test_assertion_skipped_when_broker_cannot_report(isolated_db):
    """A stub client that can't report positions → no false alarm (nothing to
    assert against)."""
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="IWM", qty=3,
                   order_id="b-IWM")
    alerts = []
    res = ci.close_intraday_positions(
        conn=conn, dry_run=False, client=object(),
        submit_market_order_fn=_submit,
        flat_assert_alert_fn=lambda msg: alerts.append(msg),
    )
    assert res["flat_assert"]["asserted"] == 0
    assert alerts == []


def test_assert_intraday_flat_unit_dry_run_noop():
    """Dry-run never asserts (nothing was flattened)."""
    out = pm_dummy = ci.assert_intraday_flat(
        AssertBroker({"QQQ": 7}), ["QQQ"], dry_run=True)
    assert out == {"asserted": 0, "still_open": [], "alerted": False}
