"""test_pg_report_data_sprint2.py — Sprint 2 / M8 KPI honesty in daily report.

Runs schedulers/pg_report_data.py as a subprocess against a seeded temp DB (via
the PG_TRADING_DB override) and asserts the new sections render without error:
per-strategy health (held/available/open_orders/paused + shared-symbol),
fresh-vs-reconciliation split, and the equity-snapshot-present check.
"""

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402

SCRIPT = ROOT / "schedulers" / "pg_report_data.py"
TODAY = date.today().isoformat()


def _seed(dbfile):
    conn = db.init_db(str(dbfile))
    for sid in ("intraday-orb-pivots-5m", "intraday-orbo-5m",
                "intraday-1m-orb", "trend-donchian-breakout-20"):
        db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    # Two strategies sharing symbol NVDA (the oversell signature).
    for sid in ("intraday-orb-pivots-5m", "intraday-orbo-5m"):
        sig = db.record_signal(conn, strategy_id=sid, symbol="NVDA",
                               bar_ts=f"{TODAY}T15:00:00",
                               signal_type="long_entry", close=100.0,
                               bar_interval="5m")
        conn.execute(
            "INSERT INTO paper_trades "
            "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
            " status, submitted_at) "
            "VALUES (?, ?, ?, ?, 'buy', 10, 'filled', ?)",
            (f"b-{sid}", sig, sid, "NVDA", f"{TODAY}T15:00:00"),
        )
    # A paused strategy that also holds a position (so the [PAUSED] tag renders
    # on its health line).
    from monitoring import strategy_health as sh
    sh.pause_strategy(conn, "intraday-1m-orb", reason="test", pause_days=None)
    psig = db.record_signal(conn, strategy_id="intraday-1m-orb", symbol="SMH",
                            bar_ts=f"{TODAY}T15:10:00",
                            signal_type="long_entry", close=200.0,
                            bar_interval="1m")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " status, submitted_at) "
        "VALUES ('b-smh', ?, 'intraday-1m-orb', 'SMH', 'buy', 5, 'filled', ?)",
        (psig, f"{TODAY}T15:10:00"),
    )
    # An equity snapshot for today (snapshot-present path).
    conn.execute(
        "INSERT INTO equity_snapshots (recorded_at, portfolio_value, cash, "
        " buying_power) VALUES (?, 100000, 50000, 50000)",
        (f"{TODAY}T16:00:00",),
    )
    # A reconciliation close + a fresh close today.
    fresh = db.record_signal(conn, strategy_id="trend-donchian-breakout-20",
                             symbol="AES", bar_ts=f"{TODAY}T20:00:00",
                             signal_type="long_entry", close=50.0,
                             bar_interval="1d")
    db.open_outcome(conn, signal_id=fresh, entry_ts=f"{TODAY}T20:00:00",
                    entry_price=50.0)
    db.close_outcome(conn, signal_id=fresh, exit_ts=f"{TODAY}T20:30:00",
                     exit_price=51.0, exit_reason="long_exit_signal",
                     bars_held=1)
    recon = db.record_signal(conn, strategy_id="intraday-1m-orb", symbol="QQQ",
                             bar_ts=f"{TODAY}T15:30:00",
                             signal_type="long_entry", close=400.0,
                             bar_interval="1m")
    db.open_outcome(conn, signal_id=recon, entry_ts=f"{TODAY}T15:30:00",
                    entry_price=400.0)
    db.close_outcome(conn, signal_id=recon, exit_ts=f"{TODAY}T15:31:00",
                     exit_price=400.0, exit_reason="reconciled_no_position",
                     bars_held=0)
    conn.commit()
    conn.close()


def _run(dbfile):
    env = dict(os.environ)
    env["PG_TRADING_DB"] = str(dbfile)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    return proc


def test_report_renders_new_sections(tmp_path):
    dbfile = tmp_path / "trading.db"
    _seed(dbfile)
    proc = _run(dbfile)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "query failed" not in out
    assert "Traceback" not in proc.stderr

    # New M8 sections present.
    assert "[STRATEGY HEALTH]" in out
    assert "[FRESH ACTIVITY vs RECONCILIATION]" in out
    assert "EQUITY SNAPSHOT: present" in out

    # Per-strategy health line + shared-symbol detection on NVDA.
    assert "held=" in out
    assert "SHARED SYMBOL" in out
    assert "DUPLICATE-SYMBOL OWNERSHIP" in out

    # Fresh vs reconcile split counted correctly (1 fresh, 1 reconcile).
    assert "fresh trading closes: 1" in out
    assert "reconciliation/cleanup closes: 1" in out
    assert "reconciled_no_position=1" in out

    # Paused strategy surfaced.
    assert "PAUSED" in out
    assert "intraday-1m-orb" in out


def test_report_flags_missing_snapshot(tmp_path):
    dbfile = tmp_path / "trading.db"
    _seed(dbfile)
    # Wipe today's snapshot to exercise the loud-alert path.
    conn = db.init_db(str(dbfile))
    conn.execute("DELETE FROM equity_snapshots")
    conn.commit()
    conn.close()

    proc = _run(dbfile)
    assert proc.returncode == 0, proc.stderr
    assert "*** ALERT: NO EQUITY SNAPSHOT RECORDED TODAY ***" in proc.stdout
