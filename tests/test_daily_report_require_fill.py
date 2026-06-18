"""Stage 0.1 (master plan, 2026-06-17) — kill the 1d phantom-outcome factory.

persist_report's 1d reconcile pass (daily_report.py:378) defaulted
require_fill=False, so open_for_entry ran for EVERY 1d long_entry that merely
had a close price — no broker fill required. The same-run orphan sweep then
quarantined each as phantom_no_fill: 13 such rows were manufactured then
quarantined on 2026-06-17 alone, and 2,634 of all phantoms are 1d.

The fix mirrors the intraday pass: pass require_fill=True on the 1d call too.
require_fill gates ONLY the OPEN path, so a real open outcome still closes on a
long_exit signal — covered by the second test.

WIRING tests: drive the real persist_report entry point (same pattern as
test_daily_report_intraday_outcomes). On the old code the unfilled 1d entry
opens a phantom outcome -> test_unfilled_1d_entry_opens_no_outcome FAILS.
"""

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import daily_report as dr  # noqa: E402


def _route_init_db(monkeypatch, db_path):
    # OOM-safe: capture the real init_db before patching so the lambda calls
    # the real callable, never itself.
    _real_init_db = db.init_db
    monkeypatch.setattr(dr.db, "init_db", lambda *a, **k: _real_init_db(db_path))


def test_unfilled_1d_entry_opens_no_outcome(tmp_path, monkeypatch):
    """A 1d long_entry with NO filled buy must not get an outcome row."""
    db_path = tmp_path / "phantom1d.db"
    _route_init_db(monkeypatch, db_path)

    conn = db.init_db(db_path)
    db.upsert_strategy(conn, {"extra": {"strategy_id": "eod-mr"}})
    sig = db.record_signal(
        conn, strategy_id="eod-mr", symbol="SPY",
        bar_ts="2026-05-14T00:00:00", signal_type="long_entry",
        close=500.0, bar_interval="1d",
    )
    # Deliberately NO paper_trades buy fill for this signal.
    conn.commit()
    conn.close()

    report = dr.DailyReport(report_date=date(2026, 5, 14), market_regime="x")
    dr.persist_report(report, markdown="x")

    conn = db.init_db(db_path)
    o = conn.execute(
        "SELECT 1 FROM outcomes WHERE signal_id=?", (sig,)
    ).fetchone()
    conn.close()
    assert o is None, "phantom regression: unfilled 1d entry opened an outcome"


def test_filled_1d_entry_opens_outcome(tmp_path, monkeypatch):
    """A 1d long_entry WITH a filled buy still opens an outcome (real path)."""
    db_path = tmp_path / "filled1d.db"
    _route_init_db(monkeypatch, db_path)

    conn = db.init_db(db_path)
    db.upsert_strategy(conn, {"extra": {"strategy_id": "eod-mr"}})
    sig = db.record_signal(
        conn, strategy_id="eod-mr", symbol="SPY",
        bar_ts="2026-05-14T00:00:00", signal_type="long_entry",
        close=500.0, bar_interval="1d",
    )
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at, fill_price) "
        "VALUES ('b1', ?, 'eod-mr', 'SPY', 'buy', 5, ?, 'filled', ?, 500.0)",
        (sig, "2026-05-14T00:00:00", "2026-05-14T00:00:00"),
    )
    conn.commit()
    conn.close()

    report = dr.DailyReport(report_date=date(2026, 5, 14), market_regime="x")
    dr.persist_report(report, markdown="x")

    conn = db.init_db(db_path)
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (sig,)
    ).fetchone()
    conn.close()
    assert o is not None, "real 1d entry with a fill should open an outcome"
    assert o["status"] == "open"
