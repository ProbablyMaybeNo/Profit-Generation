"""P8 duplicate/conflicting trailing-stop report coverage."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import trailing_stops as ts  # noqa: E402

SCRIPT = ROOT / "schedulers" / "pg_report_data.py"


def _run(dbfile):
    env = dict(os.environ)
    env["PG_TRADING_DB"] = str(dbfile)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_report_flags_duplicate_trailing_stop_conflicts(tmp_path):
    dbfile = tmp_path / "trading.db"
    conn = db.init_db(dbfile)
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, strategy_id, symbol, side, qty, status, submitted_at) "
        "VALUES ('b-owner', 'trend-donchian-breakout-20', 'KRE', 'buy', 10, "
        "'filled', '2026-06-04T20:00:00')"
    )
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, strategy_id, symbol, side, qty, status, submitted_at) "
        "VALUES ('b-other', 'intraday-vwap-reclaim-1m', 'KRE', 'buy', 10, "
        "'filled', '2026-06-04T20:00:05')"
    )
    ts.upsert_stop(
        conn, strategy_id="trend-donchian-breakout-20", symbol="KRE",
        method="atr_trail", stop_price=69.25, extreme_price=71.44,
    )
    ts.upsert_stop(
        conn, strategy_id="intraday-vwap-reclaim-1m", symbol="KRE",
        method="atr_trail", stop_price=68.90, extreme_price=71.44,
    )
    conn.close()

    proc = _run(dbfile)

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "TRAILING STOP CONFLICTS" in out
    assert "KRE" in out
    assert "owner=trend-donchian-breakout-20" in out
    assert "non_owner=intraday-vwap-reclaim-1m" in out
    assert "duplicate_extreme" in out
    assert "conflicting_stop_levels" in out
