"""A5 (audit 2026-06-03) — 1d trend strategies had no bounded exit in the
outcome model.

trend-donchian-breakout-20 (154 open) + trend-ma-cross-20-50 (40 open) =
194 of 260 open outcomes; 153/154 open donchian outcomes had NO later exit
signal (the channel/MA breakdown is rare in a trend), so an outcome stayed
OPEN indefinitely even after the position was gone. The model now honors a
time_stop: a 1d trend outcome closes with exit_reason='time_stop' once held
past max_days_held, reusing the existing _process_exit close path.

WIRING test: drives the real _check_time_stops_for_open_positions and proves
a trend outcome held past the window closes BOTH the position (a sell is
recorded) AND the outcome with exit_reason='time_stop'; a fresh one stays
open. On the old code (no time-stop pass) the outcome stays OPEN forever ->
this FAILS.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from strategies.trend import TREND_DECLARATIONS  # noqa: E402


TREND_SID = "trend-donchian-breakout-20"


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
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_open_trend_position(conn, *, symbol, entry_date, entry_price=100.0):
    db.upsert_strategy(conn, {"extra": {"strategy_id": TREND_SID}})
    sig_id = db.record_signal(
        conn, strategy_id=TREND_SID, symbol=symbol,
        bar_ts=entry_date, signal_type="long_entry",
        close=entry_price, bar_interval="1d",
    )
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at, fill_price) "
        "VALUES (?, ?, ?, ?, 'buy', 10, ?, 'filled', ?, ?)",
        (f"buy-{symbol}", sig_id, TREND_SID, symbol, entry_date,
         entry_date, entry_price),
    )
    db.open_outcome(conn, signal_id=sig_id, entry_ts=entry_date,
                    entry_price=entry_price)
    conn.commit()
    return sig_id


def test_trend_declarations_carry_time_stop():
    """The fix is config-backed, not invented per-call."""
    for decl in TREND_DECLARATIONS:
        ts = decl.get("time_stop")
        assert isinstance(ts, dict)
        assert int(ts["max_days_held"]) > 0


def test_time_stop_closes_stale_trend_outcome(isolated_db, monkeypatch):
    conn = db.init_db()
    # Entered 200 days before asof -> well past the 90-day time_stop.
    entry = "2025-11-01"
    sig_id = _seed_open_trend_position(conn, symbol="SPY", entry_date=entry,
                                       entry_price=100.0)

    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder(
            f"sell-{symbol}", 130.0),
    )
    # bars_fetcher supplies the proxy current price.
    bars = {"SPY": [{"ts": "2026-05-20", "high": 131, "low": 129,
                     "close": 130.0}]}

    actions = at._check_time_stops_for_open_positions(
        conn, settings={}, client=object(), dry_run=False,
        bars_fetcher=lambda s: bars.get(s, []),
        tracked_strategies=TREND_DECLARATIONS,
        asof=date(2026, 5, 20),
    )
    assert len(actions) == 1
    assert actions[0]["action"] == "SELL"
    assert actions[0]["exit_reason"] == "time_stop"
    assert actions[0]["days_held"] > 90

    # Position closed: a sell was recorded.
    sells = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE side='sell' AND symbol='SPY'",
    ).fetchone()[0]
    assert sells == 1

    # Outcome closed with the time_stop reason (the A5 gap).
    o = conn.execute(
        "SELECT status, exit_reason, exit_price FROM outcomes WHERE signal_id=?",
        (sig_id,),
    ).fetchone()
    assert o["status"] == "closed", \
        "A5 regression: stale trend outcome left OPEN with no bounded exit"
    assert o["exit_reason"] == "time_stop"
    assert o["exit_price"] == pytest.approx(130.0)


def test_time_stop_leaves_fresh_trend_outcome_open(isolated_db, monkeypatch):
    conn = db.init_db()
    # Entered only 5 days before asof -> within the 90-day window.
    sig_id = _seed_open_trend_position(conn, symbol="QQQ",
                                       entry_date="2026-05-15")
    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder("x", 100.0),
    )
    actions = at._check_time_stops_for_open_positions(
        conn, settings={}, client=object(), dry_run=False,
        bars_fetcher=lambda s: [{"close": 100.0}],
        tracked_strategies=TREND_DECLARATIONS,
        asof=date(2026, 5, 20),
    )
    assert actions == []
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (sig_id,),
    ).fetchone()
    assert o["status"] == "open"


def test_time_stop_skips_strategy_without_time_stop_config(isolated_db,
                                                           monkeypatch):
    """A strategy that doesn't declare a time_stop is never time-stopped,
    even if held a long time."""
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "mr-no-timestop"}})
    sig_id = db.record_signal(
        conn, strategy_id="mr-no-timestop", symbol="GDX",
        bar_ts="2025-01-01", signal_type="long_entry",
        close=50.0, bar_interval="1d",
    )
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at, fill_price) "
        "VALUES ('b', ?, 'mr-no-timestop', 'GDX', 'buy', 5, '2025-01-01', "
        " 'filled', '2025-01-01', 50.0)",
        (sig_id,),
    )
    db.open_outcome(conn, signal_id=sig_id, entry_ts="2025-01-01",
                    entry_price=50.0)
    conn.commit()

    actions = at._check_time_stops_for_open_positions(
        conn, settings={}, client=object(), dry_run=False,
        bars_fetcher=lambda s: [{"close": 50.0}],
        tracked_strategies=[{"id": "mr-no-timestop", "strategy_class": "mr"}],
        asof=date(2026, 5, 20),
    )
    assert actions == []
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (sig_id,),
    ).fetchone()
    assert o["status"] == "open"
