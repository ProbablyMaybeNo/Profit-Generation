"""
test_close_intraday_positions.py — 5.5.3: EOD intraday close-out.

Covers:
  - _open_intraday_buys identifies positions where signal.bar_interval != '1d'
  - EOD positions are excluded from the close-out
  - already-closed positions (paired sell) are excluded
  - dry-run path emits DRY_CLOSE_INTRADAY without inserting a sell
  - live path inserts a sell paper_trades row with notes flagging EOD close
  - empty-state returns OK / scanned=0
  - idempotency: re-running on the same DB produces no new closes
  - per-position dispatch (multiple open intraday positions get individual closes)
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import close_intraday_positions as ci  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    # Force is_paper_mode to True for tests so dry_run=None resolves to False
    # (we want the live path tested via submit_market_order_fn override).
    monkeypatch.setattr(ci, "is_paper_mode", lambda: True)
    yield test_db


def _seed_open_buy(conn, *, strategy_id, symbol, bar_interval,
                    bar_ts, qty=1.0, order_id="b1"):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    sig_id = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts, signal_type="long_entry",
        close=100.0, bar_interval=bar_interval,
    )
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?)",
        (order_id, sig_id, strategy_id, symbol, qty,
         f"{bar_ts}", f"{bar_ts}"),
    )
    conn.commit()
    return sig_id


def _seed_close_sell(conn, *, signal_id, strategy_id, symbol, qty=1.0,
                      order_id="s1"):
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'sell', ?, ?, 'filled', ?)",
        (order_id, signal_id, strategy_id, symbol, qty,
         "2026-05-14T15:30:00", "2026-05-14T15:30:00"),
    )
    conn.commit()


class FakeOrder:
    def __init__(self, oid: str, status: str = "submitted"):
        self.id = oid
        self.status = status
        self.submitted_at = "2026-05-14T16:00:00Z"


# ---------------- _open_intraday_buys ----------------

def test_open_intraday_buys_identifies_15m_position(isolated_db):
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                    bar_interval="15m",
                    bar_ts="2026-05-14T14:30:00")
    rows = ci._open_intraday_buys(conn)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "SPY"
    assert rows[0]["bar_interval"] == "15m"


def test_open_intraday_buys_excludes_eod(isolated_db):
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="eod-a", symbol="SPY",
                    bar_interval="1d",
                    bar_ts="2026-05-14")
    rows = ci._open_intraday_buys(conn)
    assert rows == []


def test_open_intraday_buys_excludes_already_closed(isolated_db):
    conn = db.init_db()
    sig_id = _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                              bar_interval="15m",
                              bar_ts="2026-05-14T14:30:00")
    _seed_close_sell(conn, signal_id=sig_id, strategy_id="intra-a",
                      symbol="SPY")
    rows = ci._open_intraday_buys(conn)
    assert rows == []


# ---------------- close_intraday_positions ----------------

def test_close_with_no_positions_returns_ok_zero(isolated_db):
    res = ci.close_intraday_positions(dry_run=True)
    assert res["status"] == "OK"
    assert res["closed"] == []
    assert res["scanned"] == 0


def test_close_dry_run_emits_DRY_CLOSE_INTRADAY(isolated_db):
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                    bar_interval="15m",
                    bar_ts="2026-05-14T14:30:00", qty=10)
    res = ci.close_intraday_positions(conn=conn, dry_run=True)
    assert res["status"] == "OK"
    assert len(res["closed"]) == 1
    closed = res["closed"][0]
    assert closed["action"] == "DRY_CLOSE_INTRADAY"
    assert closed["symbol"] == "SPY"
    assert closed["qty"] == 10

    # Dry-run did NOT insert a sell.
    rows = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE side='sell'",
    ).fetchone()
    assert rows[0] == 0


def test_close_live_path_inserts_sell_and_records_notes(isolated_db):
    """When dry_run=False with a fake submitter, the close inserts a
    paper_trades row tagged 'auto-close intraday EOD'."""
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                    bar_interval="15m",
                    bar_ts="2026-05-14T14:30:00", qty=7)
    fake_submit = lambda client, symbol, qty, side: FakeOrder(  # noqa: E731
        oid=f"close-{symbol}",
    )
    res = ci.close_intraday_positions(
        conn=conn, dry_run=False,
        client=object(),
        submit_market_order_fn=fake_submit,
    )
    assert res["status"] == "OK"
    assert len(res["closed"]) == 1
    closed = res["closed"][0]
    assert closed["action"] == "CLOSE_INTRADAY"
    assert closed["order_id"] == "close-SPY"

    sells = conn.execute(
        "SELECT side, qty, notes FROM paper_trades WHERE side='sell'",
    ).fetchall()
    assert len(sells) == 1
    assert sells[0]["qty"] == 7
    assert "auto-close intraday EOD" in (sells[0]["notes"] or "")


def test_close_is_idempotent(isolated_db):
    """Running close twice in a row produces zero closes the second time
    because the first run paired off the open buy."""
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                    bar_interval="15m",
                    bar_ts="2026-05-14T14:30:00", qty=5)
    fake_submit = lambda client, symbol, qty, side: FakeOrder(  # noqa: E731
        oid=f"close-{symbol}-1",
    )
    res1 = ci.close_intraday_positions(
        conn=conn, dry_run=False,
        client=object(),
        submit_market_order_fn=fake_submit,
    )
    assert len(res1["closed"]) == 1

    res2 = ci.close_intraday_positions(
        conn=conn, dry_run=False,
        client=object(),
        submit_market_order_fn=fake_submit,
    )
    assert res2["scanned"] == 0
    assert res2["closed"] == []


def test_close_handles_multiple_open_positions(isolated_db):
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                    bar_interval="15m",
                    bar_ts="2026-05-14T14:30:00", qty=3, order_id="b1")
    _seed_open_buy(conn, strategy_id="intra-b", symbol="QQQ",
                    bar_interval="5m",
                    bar_ts="2026-05-14T13:00:00", qty=4, order_id="b2")
    # Also seed an EOD position — must NOT be closed.
    _seed_open_buy(conn, strategy_id="eod-a", symbol="IWM",
                    bar_interval="1d",
                    bar_ts="2026-05-14", qty=2, order_id="b3")
    fake_submit = lambda client, symbol, qty, side: FakeOrder(  # noqa: E731
        oid=f"close-{symbol}",
    )
    res = ci.close_intraday_positions(
        conn=conn, dry_run=False,
        client=object(),
        submit_market_order_fn=fake_submit,
    )
    assert res["status"] == "OK"
    syms_closed = sorted(c["symbol"] for c in res["closed"])
    assert syms_closed == ["QQQ", "SPY"]
    assert "IWM" not in syms_closed


def test_close_cancels_resting_orders_before_flatten(isolated_db):
    """The live flatten cancels resting stops/entries for the symbols it's
    about to sell so Alpaca doesn't reject for wash-trade / held qty."""
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                    bar_interval="15m",
                    bar_ts="2026-05-14T14:30:00", qty=3, order_id="b1")
    _seed_open_buy(conn, strategy_id="intra-b", symbol="QQQ",
                    bar_interval="5m",
                    bar_ts="2026-05-14T13:00:00", qty=4, order_id="b2")
    seen = {}

    def fake_cancel(client, symbols):
        seen["symbols"] = list(symbols)
        return 2

    fake_submit = lambda client, symbol, qty, side: FakeOrder(  # noqa: E731
        oid=f"close-{symbol}",
    )
    res = ci.close_intraday_positions(
        conn=conn, dry_run=False, client=object(),
        submit_market_order_fn=fake_submit,
        cancel_open_orders_fn=fake_cancel,
        settle_seconds=0,
    )
    assert res["status"] == "OK"
    assert sorted(seen["symbols"]) == ["QQQ", "SPY"]
    assert len(res["closed"]) == 2


def test_close_continues_when_cancel_sweep_raises(isolated_db):
    """A broken cancel sweep must not abort the flatten."""
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                    bar_interval="15m",
                    bar_ts="2026-05-14T14:30:00", qty=3, order_id="b1")

    def boom(client, symbols):
        raise RuntimeError("broker down")

    fake_submit = lambda client, symbol, qty, side: FakeOrder(  # noqa: E731
        oid=f"close-{symbol}",
    )
    res = ci.close_intraday_positions(
        conn=conn, dry_run=False, client=object(),
        submit_market_order_fn=fake_submit,
        cancel_open_orders_fn=boom,
        settle_seconds=0,
    )
    assert res["status"] == "OK"
    assert len(res["closed"]) == 1


def test_close_skips_zero_qty(isolated_db):
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                    bar_interval="15m",
                    bar_ts="2026-05-14T14:30:00", qty=0)
    res = ci.close_intraday_positions(conn=conn, dry_run=True)
    assert res["status"] == "OK"
    assert res["closed"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["reason"] == "qty<1"


def test_run_daily_bat_invokes_close():
    """Sanity: schedulers/run_daily.bat references the module."""
    bat = (ROOT / "schedulers" / "run_daily.bat").read_text(encoding="utf-8")
    assert "monitoring.close_intraday_positions" in bat


# ---------------- M1: outcome-close + MFE/MAE on intraday flatten ----------------

class FakeFilledOrder:
    def __init__(self, oid, fill_price):
        self.id = oid
        self.status = "filled"
        # F4: broker fill timestamps are UTC. A 16:00 ET EOD flatten is
        # 20:00 UTC (EDT = UTC-4). The entry bar_ts is naive ET 14:30
        # (== 18:30 UTC), so the exit must be a *later* UTC instant for the
        # excursion window to be valid. The old "16:00:01Z" (= 12:00 ET)
        # predated the entry and only "worked" under the broken lexical
        # string compare this fix removes.
        self.submitted_at = "2026-05-14T20:00:00Z"
        self.filled_at = "2026-05-14T20:00:01Z"
        self.filled_avg_price = fill_price


def _seed_intraday_bar(conn, symbol, ts_utc, high, low, close):
    conn.execute(
        "INSERT INTO intraday_bars (symbol, ts_utc, open, high, low, close, "
        " volume, source, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (symbol, ts_utc, close, high, low, close, 1000, "iex", ts_utc),
    )
    conn.commit()


def test_close_intraday_writes_outcome_with_mfe_mae(isolated_db):
    """Flattening an intraday position closes its open outcome with
    exit_reason='eod_close' and MFE/MAE from intraday_bars (the M1 gap)."""
    conn = db.init_db()
    sig_id = _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                            bar_interval="1m",
                            bar_ts="2026-05-14T14:30:00", qty=3, order_id="b1")
    db.open_outcome(conn, signal_id=sig_id,
                    entry_ts="2026-05-14T14:30:00", entry_price=100.0)
    # In-window bars: max high 104 (+4%), min low 97 (-3%).
    _seed_intraday_bar(conn, "SPY", "2026-05-14T14:35:00", 104, 99, 102)
    _seed_intraday_bar(conn, "SPY", "2026-05-14T15:00:00", 103, 97, 101)

    res = ci.close_intraday_positions(
        conn=conn, dry_run=False, client=object(),
        submit_market_order_fn=lambda client, symbol, qty, side:
            FakeFilledOrder(f"close-{symbol}", 101.0),
    )
    assert res["closed"][0]["outcome_closed"] is True
    o = conn.execute(
        "SELECT status, exit_reason, exit_price, mfe_pct, mae_pct "
        "  FROM outcomes WHERE signal_id=?", (sig_id,),
    ).fetchone()
    assert o["status"] == "closed"
    assert o["exit_reason"] == "eod_close"
    assert o["exit_price"] == pytest.approx(101.0)
    assert o["mfe_pct"] == pytest.approx(0.04)
    assert o["mae_pct"] == pytest.approx(-0.03)


def test_close_intraday_outcome_close_is_best_effort(isolated_db):
    """No open outcome -> flatten still records the sell, no crash."""
    conn = db.init_db()
    _seed_open_buy(conn, strategy_id="intra-a", symbol="SPY",
                   bar_interval="1m",
                   bar_ts="2026-05-14T14:30:00", qty=3, order_id="b1")
    res = ci.close_intraday_positions(
        conn=conn, dry_run=False, client=object(),
        submit_market_order_fn=lambda client, symbol, qty, side:
            FakeFilledOrder(f"close-{symbol}", 101.0),
    )
    assert res["status"] == "OK"
    assert res["closed"][0]["outcome_closed"] is False
