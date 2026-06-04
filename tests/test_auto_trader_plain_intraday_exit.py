"""A2 (audit 2026-06-03) — plain intraday signal-exit closes the broker
position but never closed the OUTCOME.

`_process_exit` previously closed the outcome ONLY when a trailing stop
tripped (`trailing_triggered is not None`); a plain intraday
`long_exit_signal` recorded the closing SELL but left the outcome OPEN,
"for the reconcile". No reconcile pass closes intraday outcomes (the 1d EOD
reconcile filters bar_interval='1d'; the intraday reconcile runs open_only),
so intraday outcomes accumulated OPEN forever.

WIRING test: drives the real `_process_exit` for a plain (no-trailing)
intraday long_exit and proves BOTH the broker position closes (a sell
paper_trade is recorded) AND the matching outcome closes with
exit_reason='long_exit_signal'. On the old code the sell is recorded but the
outcome stays OPEN -> this test FAILS.

It also proves a plain 1d exit is NOT closed here (the 1d EOD reconcile keeps
ownership), guarding against a double-close.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


class _FakeFilledOrder:
    def __init__(self, oid, fill_price):
        self.id = oid
        self.status = "filled"
        self.submitted_at = "2026-05-14T20:00:00Z"
        self.filled_at = "2026-05-14T20:00:01Z"
        self.filled_avg_price = fill_price


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_open_position_with_outcome(conn, *, strategy_id, symbol,
                                     bar_interval, entry_bar_ts):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    entry_sig = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=entry_bar_ts, signal_type="long_entry",
        close=100.0, bar_interval=bar_interval,
    )
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at, fill_price) "
        "VALUES ('b1', ?, ?, ?, 'buy', 5, ?, 'filled', ?, 100.0)",
        (entry_sig, strategy_id, symbol, entry_bar_ts, entry_bar_ts),
    )
    db.open_outcome(conn, signal_id=entry_sig,
                    entry_ts=entry_bar_ts, entry_price=100.0)
    conn.commit()
    return entry_sig


def _exit_signal_row(conn, *, strategy_id, symbol, bar_interval, bar_ts,
                     close):
    exit_sig_id = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts, signal_type="long_exit",
        close=close, bar_interval=bar_interval,
    )
    return conn.execute(
        "SELECT * FROM signals WHERE id=?", (exit_sig_id,),
    ).fetchone()


def test_plain_intraday_exit_closes_position_and_outcome(isolated_db,
                                                         monkeypatch):
    conn = db.init_db()
    entry_sig = _seed_open_position_with_outcome(
        conn, strategy_id="intra-mr", symbol="QQQ",
        bar_interval="1m", entry_bar_ts="2026-05-14T14:30:00",
    )
    exit_sig = _exit_signal_row(
        conn, strategy_id="intra-mr", symbol="QQQ",
        bar_interval="1m", bar_ts="2026-05-14T14:45:00", close=101.5,
    )

    # No trailing stop exists -> trailing_triggered resolves to None (plain
    # signal exit). Patch the broker submit to a deterministic filled order.
    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder(
            f"sell-{symbol}", 101.5),
    )

    out = at._process_exit(
        conn, client=object(), settings={}, sig=exit_sig, dry_run=False,
    )

    # Broker position closed: a sell paper_trade was recorded.
    assert out["action"] == "SELL"
    assert out["exit_reason"] == "long_exit_signal"
    sells = conn.execute(
        "SELECT side, qty FROM paper_trades WHERE side='sell'",
    ).fetchall()
    assert len(sells) == 1
    assert sells[0]["qty"] == 5

    # Outcome ALSO closed with the plain-exit reason (the A2 gap).
    o = conn.execute(
        "SELECT status, exit_reason, exit_price FROM outcomes WHERE signal_id=?",
        (entry_sig,),
    ).fetchone()
    assert o["status"] == "closed", \
        "A2 regression: plain intraday exit left the outcome OPEN"
    assert o["exit_reason"] == "long_exit_signal"
    assert o["exit_price"] == pytest.approx(101.5)


def test_plain_1d_exit_does_not_close_outcome_here(isolated_db, monkeypatch):
    """A 1d plain exit records the sell but leaves the outcome for the EOD 1d
    reconcile — closing it here would risk a double-close / wrong reason."""
    conn = db.init_db()
    entry_sig = _seed_open_position_with_outcome(
        conn, strategy_id="eod-mr", symbol="SPY",
        bar_interval="1d", entry_bar_ts="2026-05-14",
    )
    exit_sig = _exit_signal_row(
        conn, strategy_id="eod-mr", symbol="SPY",
        bar_interval="1d", bar_ts="2026-05-15", close=102.0,
    )
    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder(
            f"sell-{symbol}", 102.0),
    )

    out = at._process_exit(
        conn, client=object(), settings={}, sig=exit_sig, dry_run=False,
    )
    assert out["action"] == "SELL"

    o = conn.execute(
        "SELECT status, exit_reason FROM outcomes WHERE signal_id=?",
        (entry_sig,),
    ).fetchone()
    assert o["status"] == "open", \
        "1d plain exit must stay owned by the EOD 1d reconcile, not close here"
    assert o["exit_reason"] is None
