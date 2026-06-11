import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import outcome_tracker  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = db.init_db(tmp_path / "trading.db")
    db.upsert_strategy(c, {"extra": {"strategy_id": "strat-A"}})
    db.upsert_strategy(c, {"extra": {"strategy_id": "strat-B"}})
    yield c
    c.close()


def _record_entry(c, strat, sym, bar_ts, close):
    return db.record_signal(c, strategy_id=strat, symbol=sym, bar_ts=bar_ts,
                            signal_type="long_entry", close=close, bar_interval="1d")


def _record_exit(c, strat, sym, bar_ts, close):
    return db.record_signal(c, strategy_id=strat, symbol=sym, bar_ts=bar_ts,
                            signal_type="long_exit", close=close, bar_interval="1d")


def _outcome(c, signal_id):
    return c.execute("SELECT * FROM outcomes WHERE signal_id=?", (signal_id,)).fetchone()


def test_open_for_entry_creates_outcome(conn):
    sid = _record_entry(conn, "strat-A", "GDX", "2026-05-12", 50.0)
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["opened"] == 1
    o = _outcome(conn, sid)
    assert o is not None
    assert o["status"] == "open"
    assert o["entry_price"] == 50.0


def test_open_for_entry_idempotent_with_existing_open(conn):
    _record_entry(conn, "strat-A", "GDX", "2026-05-12", 50.0)
    _record_entry(conn, "strat-A", "GDX", "2026-05-13", 51.0)
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["opened"] == 1
    assert counts["noop"] == 1
    n_open = conn.execute("SELECT COUNT(*) FROM outcomes WHERE status='open'").fetchone()[0]
    assert n_open == 1


def test_close_for_exit_with_no_open_is_noop(conn):
    _record_exit(conn, "strat-A", "GDX", "2026-05-13", 51.0)
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["closed"] == 0
    assert counts["noop"] == 1
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_full_cycle_returns_correct_pct(conn):
    entry_id = _record_entry(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    _record_exit(conn, "strat-A", "GDX", "2026-05-15", 105.0)
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["opened"] == 1
    assert counts["closed"] == 1
    o = _outcome(conn, entry_id)
    assert o["status"] == "closed"
    assert o["exit_price"] == 105.0
    assert abs(o["return_pct"] - 5.0) < 1e-9
    assert o["bars_held"] == 3
    assert o["exit_reason"] == "long_exit_signal"


def test_same_bar_entry_and_exit_resolves_in_order(conn):
    entry_id = _record_entry(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    _record_exit(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["opened"] == 1
    assert counts["closed"] == 1
    o = _outcome(conn, entry_id)
    assert o["status"] == "closed"
    assert o["return_pct"] == 0.0
    assert o["bars_held"] == 0


def test_reconcile_is_idempotent(conn):
    _record_entry(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    _record_exit(conn, "strat-A", "GDX", "2026-05-15", 110.0)
    outcome_tracker.reconcile_signals(conn)
    snapshot1 = conn.execute(
        "SELECT signal_id, status, entry_price, exit_price, return_pct, bars_held FROM outcomes"
    ).fetchall()
    counts = outcome_tracker.reconcile_signals(conn)
    snapshot2 = conn.execute(
        "SELECT signal_id, status, entry_price, exit_price, return_pct, bars_held FROM outcomes"
    ).fetchall()
    assert [dict(r) for r in snapshot1] == [dict(r) for r in snapshot2]
    assert counts["opened"] == 0
    assert counts["closed"] == 0


def test_isolated_per_strategy_and_symbol(conn):
    _record_entry(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    _record_entry(conn, "strat-B", "GDX", "2026-05-12", 100.0)
    _record_entry(conn, "strat-A", "XME", "2026-05-12", 50.0)
    outcome_tracker.reconcile_signals(conn)
    n_open = conn.execute("SELECT COUNT(*) FROM outcomes WHERE status='open'").fetchone()[0]
    assert n_open == 3
    _record_exit(conn, "strat-A", "GDX", "2026-05-13", 101.0)
    outcome_tracker.reconcile_signals(conn)
    closed = conn.execute(
        "SELECT s.strategy_id, s.symbol FROM outcomes o JOIN signals s ON s.id=o.signal_id WHERE o.status='closed'"
    ).fetchall()
    assert len(closed) == 1
    assert closed[0]["strategy_id"] == "strat-A"
    assert closed[0]["symbol"] == "GDX"


def test_re_entry_after_close_opens_new_outcome(conn):
    e1 = _record_entry(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    _record_exit(conn, "strat-A", "GDX", "2026-05-13", 101.0)
    e2 = _record_entry(conn, "strat-A", "GDX", "2026-05-14", 102.0)
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["opened"] == 2
    assert counts["closed"] == 1
    o1 = _outcome(conn, e1)
    o2 = _outcome(conn, e2)
    assert o1["status"] == "closed"
    assert o2["status"] == "open"


def test_open_outcomes_summary(conn):
    _record_entry(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    _record_entry(conn, "strat-B", "XME", "2026-05-13", 50.0)
    outcome_tracker.reconcile_signals(conn)
    summary = outcome_tracker.open_outcomes_summary(conn)
    assert len(summary) == 2
    syms = {row["symbol"] for row in summary}
    assert syms == {"GDX", "XME"}


def test_skip_entry_with_null_close(conn):
    db.record_signal(conn, strategy_id="strat-A", symbol="GDX", bar_ts="2026-05-12",
                     signal_type="long_entry", close=None, bar_interval="1d")
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["opened"] == 0
    assert counts["noop"] == 1


def test_only_reconciles_specified_interval(conn):
    db.record_signal(conn, strategy_id="strat-A", symbol="GDX",
                     bar_ts="2026-05-12T15:30:00", signal_type="long_entry",
                     close=100.0, bar_interval="1m")
    counts = outcome_tracker.reconcile_signals(conn, bar_interval="1d")
    assert counts["opened"] == 0
    counts_m = outcome_tracker.reconcile_signals(conn, bar_interval="1m")
    assert counts_m["opened"] == 1


def test_backfill_doesnt_block_earlier_dated_entry(conn):
    """Existing outcome at later date must not block backfill from opening earlier outcome."""
    _record_entry(conn, "strat-A", "GDX", "2026-05-14", 100.0)
    outcome_tracker.reconcile_signals(conn)
    assert conn.execute("SELECT COUNT(*) FROM outcomes WHERE status='open'").fetchone()[0] == 1
    _record_entry(conn, "strat-A", "GDX", "2024-08-01", 60.0)
    _record_exit(conn, "strat-A", "GDX", "2024-08-05", 62.0)
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["opened"] == 1
    assert counts["closed"] == 1
    rows = conn.execute(
        "SELECT s.bar_ts, o.status, o.return_pct "
        "FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        "ORDER BY s.bar_ts"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["bar_ts"] == "2024-08-01"
    assert rows[0]["status"] == "closed"
    assert abs(rows[0]["return_pct"] - (2.0 / 60.0 * 100.0)) < 1e-9
    assert rows[1]["bar_ts"] == "2026-05-14"
    assert rows[1]["status"] == "open"


def test_close_for_exit_picks_prior_open_only(conn):
    """An exit at t=5 should close the prior entry at t=2, not a later entry at t=10."""
    _record_entry(conn, "strat-A", "GDX", "2026-05-02", 50.0)
    _record_entry(conn, "strat-A", "GDX", "2026-05-10", 55.0)
    _record_exit(conn,  "strat-A", "GDX", "2026-05-05", 52.0)
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["opened"] == 2
    assert counts["closed"] == 1
    closed = conn.execute(
        "SELECT s.bar_ts FROM outcomes o JOIN signals s ON s.id=o.signal_id "
        "WHERE o.status='closed'"
    ).fetchone()
    assert closed["bar_ts"] == "2026-05-02"
    open_row = conn.execute(
        "SELECT s.bar_ts FROM outcomes o JOIN signals s ON s.id=o.signal_id "
        "WHERE o.status='open'"
    ).fetchone()
    assert open_row["bar_ts"] == "2026-05-10"


# ---------------------------------------------------------------------------
# M1 — exit_reason override + MFE/MAE via bars_fetcher
# ---------------------------------------------------------------------------

def test_close_for_exit_accepts_exit_reason_override(conn):
    sid = _record_entry(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    exit_row = conn.execute(
        "SELECT * FROM signals WHERE id=?",
        (_record_exit(conn, "strat-A", "GDX", "2026-05-15", 96.0),),
    ).fetchone()
    outcome_tracker.open_for_entry(
        conn, conn.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone(),
    )
    closed = outcome_tracker.close_for_exit(
        conn, exit_row, exit_reason="trailing_stop",
    )
    assert closed is True
    o = _outcome(conn, sid)
    assert o["exit_reason"] == "trailing_stop"


def test_reconcile_populates_mfe_mae_from_bars_fetcher(conn):
    entry_id = _record_entry(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    _record_exit(conn, "strat-A", "GDX", "2026-05-15", 103.0)

    def fetcher(symbol):
        # In-window highs/lows: max high 112 (+12%), min low 94 (-6%).
        return [
            {"ts": "2026-05-12", "high": 105, "low": 99},
            {"ts": "2026-05-13", "high": 112, "low": 94},
            {"ts": "2026-05-14", "high": 108, "low": 101},
        ]

    counts = outcome_tracker.reconcile_signals(conn, bars_fetcher=fetcher)
    assert counts["closed"] == 1
    o = _outcome(conn, entry_id)
    assert o["mfe_pct"] == pytest.approx(0.12)
    assert o["mae_pct"] == pytest.approx(-0.06)
    assert o["exit_reason"] == "long_exit_signal"


def test_reconcile_swallows_bars_fetcher_error(conn):
    entry_id = _record_entry(conn, "strat-A", "GDX", "2026-05-12", 100.0)
    _record_exit(conn, "strat-A", "GDX", "2026-05-15", 103.0)

    def boom(symbol):
        raise RuntimeError("yfinance down")

    counts = outcome_tracker.reconcile_signals(conn, bars_fetcher=boom)
    # Close still lands, MFE/MAE just stay NULL.
    assert counts["closed"] == 1
    o = _outcome(conn, entry_id)
    assert o["status"] == "closed"
    assert o["mfe_pct"] is None
    assert o["mae_pct"] is None


def test_reconcile_processes_intraday_intervals(conn):
    """An intraday (1m) entry+exit is reconciled into a closed outcome when
    the 1m interval is included in the reconcile pass."""
    db.record_signal(conn, strategy_id="strat-A", symbol="SPY",
                     bar_ts="2026-05-14T14:30:00", signal_type="long_entry",
                     close=100.0, bar_interval="1m")
    db.record_signal(conn, strategy_id="strat-A", symbol="SPY",
                     bar_ts="2026-05-14T15:45:00", signal_type="long_exit",
                     close=101.5, bar_interval="1m")
    counts = outcome_tracker.reconcile_signals(conn, bar_intervals=["1m"])
    assert counts["opened"] == 1
    assert counts["closed"] == 1
    row = conn.execute(
        "SELECT o.status, o.return_pct FROM outcomes o "
        " JOIN signals s ON s.id=o.signal_id "
        " WHERE s.bar_interval='1m' AND o.status='closed'"
    ).fetchone()
    assert row is not None
    assert row["return_pct"] == pytest.approx(1.5)


def _record_intraday_entry(c, strat, sym, bar_ts, close, interval="15m"):
    return db.record_signal(c, strategy_id=strat, symbol=sym, bar_ts=bar_ts,
                            signal_type="long_entry", close=close,
                            bar_interval=interval)


def _fill_buy(c, signal_id, strat, sym, qty=5):
    c.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'buy', ?, 'filled', '2026-06-09T14:00:00')",
        (f"o-{signal_id}", signal_id, strat, sym, qty),
    )


def test_require_fill_skips_unfilled_signal(conn):
    sid = _record_intraday_entry(conn, "strat-A", "TSLA",
                                 "2026-06-09T10:00:00", 300.0)
    counts = outcome_tracker.reconcile_signals(
        conn, bar_intervals=["15m"], open_only=True, require_fill=True)
    assert counts["opened"] == 0
    assert counts["noop"] == 1
    assert _outcome(conn, sid) is None


def test_require_fill_opens_filled_signal(conn):
    sid = _record_intraday_entry(conn, "strat-A", "TSLA",
                                 "2026-06-09T10:00:00", 300.0)
    _fill_buy(conn, sid, "strat-A", "TSLA")
    counts = outcome_tracker.reconcile_signals(
        conn, bar_intervals=["15m"], open_only=True, require_fill=True)
    assert counts["opened"] == 1
    o = _outcome(conn, sid)
    assert o is not None and o["status"] == "open"


def test_require_fill_default_off_preserves_legacy(conn):
    sid = _record_entry(conn, "strat-A", "GDX", "2026-06-09", 50.0)
    counts = outcome_tracker.reconcile_signals(conn)
    assert counts["opened"] == 1
    assert _outcome(conn, sid) is not None
