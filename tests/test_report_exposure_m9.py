"""test_report_exposure_m9.py — Sprint 3 / M9: correct exposure/accounting.

The report computed exposure as portfolio_value - cash. For a long-only system
that's a rough proxy, but it silently miscounts a short (a short ADDS cash, so
the proxy understates gross exposure and can read negative-deployed when net
short) and never surfaces an unintended short at all.

M9: compute exposure from long/short market value + equity, and ALERT LOUDLY on
any short_market_value < 0 (the oversell-into-short signature) for this long-only
system. These tests drive the REAL report (schedulers/pg_report_data.py) against
a seeded DB and the snapshot recorder, proving the alert fires on a net short and
true exposure is reported.
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


def _run(dbfile):
    env = dict(os.environ)
    env["PG_TRADING_DB"] = str(dbfile)
    return subprocess.run([sys.executable, str(SCRIPT)],
                          capture_output=True, text=True, env=env, timeout=120)


def _seed_snapshot(dbfile, *, lmv, smv, equity=100000.0, cash=50000.0):
    conn = db.init_db(str(dbfile))
    db.record_equity_snapshot(
        conn, portfolio_value=equity, cash=cash, equity=equity,
        buying_power=cash, long_market_value=lmv, short_market_value=smv,
        recorded_at=f"{TODAY}T16:00:00",
    )
    conn.commit()
    conn.close()


def test_net_short_alert_fires(tmp_path):
    """A negative short_market_value (unintended short) must trip the loud
    long-only alert in the report."""
    dbfile = tmp_path / "trading.db"
    _seed_snapshot(dbfile, lmv=40000.0, smv=-5000.0)
    proc = _run(dbfile)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "SHORT MARKET VALUE < 0 ON A LONG-ONLY SYSTEM" in out
    assert "long market value: $40000.00" in out
    assert "short market value: $-5000.00" in out


def test_clean_long_only_no_short_alert(tmp_path):
    """A flat short market value reports exposure and fires NO net-short alert."""
    dbfile = tmp_path / "trading.db"
    _seed_snapshot(dbfile, lmv=60000.0, smv=0.0)
    proc = _run(dbfile)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "SHORT MARKET VALUE < 0" not in out
    # Gross exposure computed from long MV (60000 / 100000 equity = 60%).
    assert "gross exposure: $60000.00 (60.0% of equity)" in out


def test_snapshot_recorder_persists_long_short_mv(tmp_path):
    """db.record_equity_snapshot persists long/short market value (so the live
    DB the report runs against actually carries them)."""
    dbfile = tmp_path / "trading.db"
    conn = db.init_db(str(dbfile))
    db.record_equity_snapshot(
        conn, portfolio_value=100000.0, cash=50000.0, equity=100000.0,
        long_market_value=45000.0, short_market_value=-1000.0,
        recorded_at=f"{TODAY}T16:00:00",
    )
    conn.commit()
    row = conn.execute(
        "SELECT long_market_value, short_market_value FROM equity_snapshots"
    ).fetchone()
    conn.close()
    assert row["long_market_value"] == 45000.0
    assert row["short_market_value"] == -1000.0


def test_legacy_snapshot_without_mv_uses_proxy(tmp_path):
    """A pre-M9 snapshot row (no long/short MV) still renders via the legacy
    deployed proxy — no crash, no false net-short alert."""
    dbfile = tmp_path / "trading.db"
    conn = db.init_db(str(dbfile))
    # Insert WITHOUT long/short MV (NULL).
    conn.execute(
        "INSERT INTO equity_snapshots (recorded_at, portfolio_value, cash, "
        " buying_power) VALUES (?, 100000, 40000, 60000)",
        (f"{TODAY}T16:00:00",),
    )
    conn.commit()
    conn.close()
    proc = _run(dbfile)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "legacy proxy" in out
    assert "SHORT MARKET VALUE < 0" not in out
