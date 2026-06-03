"""F2 (audit 2026-06-03) — intraday signals never got outcome rows.

The only live reconcile (daily_report.persist_report -> reconcile_signals)
defaulted bar_interval='1d', so open_for_entry never ran for intraday
entries. With 0 open intraday outcomes, M1's EOD-flatten capture
(close_intraday_positions) had nothing to close -> dead in production.

WIRING test: drives the real persist_report entry point and proves an
intraday (bar_interval='1m') entry produces an OPEN outcome row via the new
intraday pass, then proves the real EOD flatten closes that same outcome
with exit_reason='eod_close' and non-NULL MFE/MAE from intraday_bars.

On the old code (no intraday pass) the outcome is never opened, so the
flatten reports outcome_closed=False and the outcome row never exists ->
this test FAILS.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import daily_report as dr  # noqa: E402
from monitoring import close_intraday_positions as ci  # noqa: E402


class _FakeFilledOrder:
    def __init__(self, oid, fill_price):
        self.id = oid
        self.status = "filled"
        # 16:00 ET flatten == 20:00 UTC (EDT). Entry naive ET 14:30 == 18:30
        # UTC, so the exit is a later UTC instant (valid excursion window).
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


def test_intraday_entry_opens_outcome_then_eod_flatten_closes_it(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "f2.db"

    # Route the live entry point at an isolated DB. Capture the real init_db
    # BEFORE patching so the lambda calls the *real* callable (not itself) --
    # a self-referential monkeypatch would recurse unbounded (OOM).
    _real_init_db = db.init_db
    monkeypatch.setattr(dr.db, "init_db", lambda *a, **k: _real_init_db(db_path))

    # Seed an intraday (1m) long_entry signal + a filled open buy, exactly as
    # the live intraday path would have left it before EOD.
    conn = db.init_db(db_path)
    db.upsert_strategy(conn, {"extra": {"strategy_id": "intra-mr"}})
    entry_sig = db.record_signal(
        conn, strategy_id="intra-mr", symbol="QQQ",
        bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
        close=100.0, bar_interval="1m",
    )
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at, fill_price) "
        "VALUES ('b1', ?, 'intra-mr', 'QQQ', 'buy', 5, ?, 'filled', ?, 100.0)",
        (entry_sig, "2026-05-14T14:30:00", "2026-05-14T14:30:00"),
    )
    # In-window intraday bars (naive ET, the bar pipeline convention): max
    # high 104 (+4%), min low 97 (-3%). 14:35 ET == 18:35 UTC, inside the
    # [entry 18:30 UTC, exit 20:00 UTC] window.
    _seed_intraday_bar(conn, "QQQ", "2026-05-14T14:35:00", 104, 99, 102)
    _seed_intraday_bar(conn, "QQQ", "2026-05-14T15:00:00", 103, 97, 101)
    conn.commit()
    conn.close()

    # --- Step 1: the live EOD reconcile must OPEN an intraday outcome. ---
    report = dr.DailyReport(
        report_date=date(2026, 5, 14),
        market_regime="choppy",
    )
    counts = dr.persist_report(report, markdown="x")
    assert counts["opened"] >= 1, counts

    conn = db.init_db(db_path)
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (entry_sig,),
    ).fetchone()
    assert o is not None, "F2 regression: no intraday outcome opened"
    assert o["status"] == "open"
    conn.close()

    # --- Step 2: the EOD flatten closes that outcome with eod_close. ---
    monkeypatch.setattr(ci, "is_paper_mode", lambda: True)
    conn = db.init_db(db_path)
    res = ci.close_intraday_positions(
        conn=conn, dry_run=False, client=object(),
        submit_market_order_fn=lambda client, symbol, qty, side:
            _FakeFilledOrder(f"close-{symbol}", 101.0),
    )
    assert res["status"] == "OK"
    assert len(res["closed"]) == 1
    assert res["closed"][0]["outcome_closed"] is True

    o = conn.execute(
        "SELECT status, exit_reason, exit_price, mfe_pct, mae_pct "
        "  FROM outcomes WHERE signal_id=?", (entry_sig,),
    ).fetchone()
    conn.close()
    assert o["status"] == "closed"
    assert o["exit_reason"] == "eod_close"
    assert o["exit_price"] == pytest.approx(101.0)
    assert o["mfe_pct"] == pytest.approx(0.04)
    assert o["mae_pct"] == pytest.approx(-0.03)


def test_intraday_pass_does_not_close_on_intraday_exit_signal(
    tmp_path, monkeypatch
):
    """open_only: an intraday scanner long_exit signal must NOT pre-empt the
    EOD flatten by closing the outcome as 'long_exit_signal'. The outcome
    stays open after persist_report so the flatten can stamp 'eod_close'."""
    db_path = tmp_path / "f2b.db"
    _real_init_db = db.init_db
    monkeypatch.setattr(dr.db, "init_db", lambda *a, **k: _real_init_db(db_path))

    conn = db.init_db(db_path)
    db.upsert_strategy(conn, {"extra": {"strategy_id": "intra-mr"}})
    entry_sig = db.record_signal(
        conn, strategy_id="intra-mr", symbol="QQQ",
        bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
        close=100.0, bar_interval="1m",
    )
    # An intraday long_exit signal exists (scanner emitted a target-hit).
    db.record_signal(
        conn, strategy_id="intra-mr", symbol="QQQ",
        bar_ts="2026-05-14T14:45:00", signal_type="long_exit",
        close=101.5, bar_interval="1m",
    )
    conn.commit()
    conn.close()

    report = dr.DailyReport(report_date=date(2026, 5, 14), market_regime="x")
    dr.persist_report(report, markdown="x")

    conn = db.init_db(db_path)
    o = conn.execute(
        "SELECT status, exit_reason FROM outcomes WHERE signal_id=?",
        (entry_sig,),
    ).fetchone()
    conn.close()
    assert o is not None
    assert o["status"] == "open", \
        "intraday long_exit must not close the outcome; EOD flatten owns it"
    assert o["exit_reason"] is None
