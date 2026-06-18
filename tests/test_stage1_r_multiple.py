"""Stage 1.6 (master plan, 2026-06-17) — R-multiple logging + rolling expectancy.

R = realized return / initial-stop risk. +2R means the trade made twice what it
risked. R is the honest unit for expectancy / Kelly / pyramiding. close_outcome
computes and stores it from the entry's protective stop; expectancy_metrics rolls
it up over real (non-phantom/stale) closed outcomes.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import daily_report as dr  # noqa: E402


def _open_entry_with_stop(conn, sym, *, entry=100.0, stop=None, bar_ts="2026-05-14"):
    sig = db.record_signal(conn, strategy_id="winner", symbol=sym,
                           bar_ts=bar_ts, signal_type="long_entry",
                           close=entry, bar_interval="1d")
    db.open_outcome(conn, signal_id=sig, entry_ts=bar_ts, entry_price=entry)
    if stop is not None:
        db.record_paper_trade(conn, {
            "alpaca_order_id": f"stop-{sig}", "signal_id": sig,
            "strategy_id": "winner", "symbol": sym, "side": "sell", "qty": 5,
            "order_type": "stop", "stop_price": stop, "status": "accepted",
            "submitted_at": bar_ts,
        })
    conn.commit()
    return sig


def test_close_outcome_computes_r_multiple(tmp_path):
    conn = db.init_db(tmp_path / "r.db")
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    # entry 100, stop 95 (5% risk), exit 110 (+10% return) -> R = +2.
    sig = _open_entry_with_stop(conn, "GDX", entry=100.0, stop=95.0)
    db.close_outcome(conn, signal_id=sig, exit_ts="2026-05-20",
                     exit_price=110.0, exit_reason="long_exit_signal")
    row = conn.execute(
        "SELECT return_pct, r_multiple FROM outcomes WHERE signal_id=?", (sig,)
    ).fetchone()
    conn.close()
    assert row["return_pct"] == pytest.approx(10.0)
    assert row["r_multiple"] == pytest.approx(2.0)


def test_r_multiple_negative_on_a_loss(tmp_path):
    conn = db.init_db(tmp_path / "r2.db")
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    # entry 100, stop 95 (5% risk), exit 97.5 (-2.5%) -> R = -0.5.
    sig = _open_entry_with_stop(conn, "GDX", entry=100.0, stop=95.0)
    db.close_outcome(conn, signal_id=sig, exit_ts="2026-05-20",
                     exit_price=97.5, exit_reason="trailing_stop")
    row = conn.execute(
        "SELECT r_multiple FROM outcomes WHERE signal_id=?", (sig,)
    ).fetchone()
    conn.close()
    assert row["r_multiple"] == pytest.approx(-0.5)


def test_r_multiple_null_without_a_stop(tmp_path):
    conn = db.init_db(tmp_path / "r3.db")
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    sig = _open_entry_with_stop(conn, "GDX", entry=100.0, stop=None)
    db.close_outcome(conn, signal_id=sig, exit_ts="2026-05-20",
                     exit_price=110.0, exit_reason="long_exit_signal")
    row = conn.execute(
        "SELECT r_multiple FROM outcomes WHERE signal_id=?", (sig,)
    ).fetchone()
    conn.close()
    assert row["r_multiple"] is None


def test_expectancy_metrics_rolls_up_and_excludes_noise(tmp_path):
    conn = db.init_db(tmp_path / "exp.db")
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    # Three honest closes: +2R, +1R (exit 105 / stop 95), -1R (exit 95).
    s1 = _open_entry_with_stop(conn, "AAA", entry=100.0, stop=95.0, bar_ts="2026-05-14")
    db.close_outcome(conn, signal_id=s1, exit_ts="2026-05-20", exit_price=110.0,
                     exit_reason="long_exit_signal")
    s2 = _open_entry_with_stop(conn, "BBB", entry=100.0, stop=95.0, bar_ts="2026-05-15")
    db.close_outcome(conn, signal_id=s2, exit_ts="2026-05-20", exit_price=105.0,
                     exit_reason="long_exit_signal")
    s3 = _open_entry_with_stop(conn, "CCC", entry=100.0, stop=95.0, bar_ts="2026-05-16")
    db.close_outcome(conn, signal_id=s3, exit_ts="2026-05-20", exit_price=95.0,
                     exit_reason="trailing_stop")
    # A stale-flatten close with an R must be EXCLUDED from expectancy.
    s4 = _open_entry_with_stop(conn, "DDD", entry=100.0, stop=95.0, bar_ts="2026-05-17")
    db.close_outcome(conn, signal_id=s4, exit_ts="2026-05-20", exit_price=80.0,
                     exit_reason="stale_intraday_flatten_missed")

    m = dr.expectancy_metrics(conn)
    conn.close()
    # (+2 +1 -1)/3 = +0.667R over the 3 honest trades; stale excluded.
    assert m["n"] == 3
    assert m["avg_r"] == pytest.approx(0.667, abs=0.001)
    assert m["win_rate"] == pytest.approx(0.667, abs=0.001)
