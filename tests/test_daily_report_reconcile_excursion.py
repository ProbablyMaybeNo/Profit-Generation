"""F3 (audit 2026-06-03) — MFE/MAE were 100% NULL in production because the
live reconcile (daily_report.persist_report -> reconcile_signals) passed no
bars_fetcher, so close_for_exit never computed excursion.

WIRING test: drives the real persist_report entry point (its real
reconcile_signals + close_for_exit + the real auto_trader
_build_default_bars_fetcher, whose only network source we stub). It asserts
a closed outcome lands with non-NULL mfe_pct/mae_pct. With the old code
(reconcile_signals called with no bars_fetcher) mfe/mae stay NULL and this
test FAILS.
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import daily_report as dr  # noqa: E402
from monitoring import wide_bars  # noqa: E402


def test_persist_report_reconcile_records_mfe_mae(tmp_path, monkeypatch):
    db_path = tmp_path / "f3.db"

    # Route the live entry point at an isolated DB. Capture the real init_db
    # BEFORE patching: dr.db is the same module object as db, so patching
    # dr.db.init_db replaces db.init_db too -- a lambda that called
    # db.init_db(db_path) would invoke itself, recursing unbounded (OOM).
    _real_init_db = db.init_db
    monkeypatch.setattr(dr.db, "init_db", lambda *a, **k: _real_init_db(db_path))

    # Pre-seed an OPEN outcome for an earlier long_entry on SPY.
    conn = db.init_db(db_path)
    db.upsert_strategy(conn, {"extra": {"strategy_id": "mr-x"}})
    entry_sig = db.record_signal(
        conn, strategy_id="mr-x", symbol="SPY",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )
    db.open_outcome(conn, signal_id=entry_sig,
                    entry_ts="2026-05-12", entry_price=100.0)
    conn.commit()
    conn.close()

    # Stub the bars source the real _build_default_bars_fetcher pulls from,
    # so no network is hit but the production fetcher path still runs.
    def fake_wide(symbols, lookback_bars=60):
        idx = pd.bdate_range("2026-05-12", "2026-05-14")
        return {
            s: pd.DataFrame(
                {
                    "open": [100.0, 103.0, 101.0],
                    "high": [104.0, 110.0, 106.0],  # peak 110 -> mfe +10%
                    "low": [99.0, 95.0, 97.0],      # trough 95 -> mae -5%
                    "close": [103.0, 101.0, 102.0],
                    "volume": [1e6, 1e6, 1e6],
                },
                index=idx,
            )
            for s in symbols
        }

    monkeypatch.setattr(wide_bars, "fetch_wide_daily_bars", fake_wide)

    report = dr.DailyReport(
        report_date=date(2026, 5, 14),
        market_regime="choppy",
        exit_signals=[{
            "strategy_id": "mr-x", "symbol": "SPY",
            "close": 102.0, "bar_date": "2026-05-14",
        }],
    )

    counts = dr.persist_report(report, markdown="x")
    assert counts["closed"] >= 1, counts

    conn = db.init_db(db_path)
    o = conn.execute(
        "SELECT status, exit_reason, mfe_pct, mae_pct "
        "  FROM outcomes WHERE signal_id=?", (entry_sig,),
    ).fetchone()
    conn.close()
    assert o["status"] == "closed"
    assert o["exit_reason"] == "long_exit_signal"
    assert o["mfe_pct"] is not None, "F3 regression: mfe_pct NULL (no bars_fetcher wired)"
    assert o["mae_pct"] is not None, "F3 regression: mae_pct NULL (no bars_fetcher wired)"
    assert o["mfe_pct"] == pytest.approx(0.10)
    assert o["mae_pct"] == pytest.approx(-0.05)
