"""Stage 0.4 (master plan, 2026-06-17) — order_sync must run at END of pass too.

The top-of-pass order_sync only sees the PREVIOUS run's orders. The sells/stops
THIS pass submits are never re-queried, so on the EOD final run they strand at
status='accepted'/NULL forever — the precursor to orphan reconciled_no_position
outcomes. process_signals now re-runs sync_order_fills at the end of the pass
(same built_own_client guard as the top-of-pass sync).

The backfill mechanism itself is covered by test_order_sync.py; this asserts the
WIRING — that the end-of-pass call actually fires.
"""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import order_sync  # noqa: E402


def test_order_sync_runs_at_start_and_end_of_pass(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    conn = db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)

    calls = {"n": 0}

    def counting(conn_, client_, **k):
        calls["n"] += 1
        return {"checked": 0, "updated": 0, "filled": 0, "errors": 0}

    monkeypatch.setattr(order_sync, "sync_order_fills", counting)

    # built_own_client = (client is None and not dry_run) -> the live path that
    # runs the syncs. Feed a stub client via client_factory; no signals exist so
    # the pass just opens and closes around the two syncs.
    fake_client = MagicMock()
    fake_client.get_all_positions.return_value = []
    fake_client.list_positions.return_value = []
    fake_client.get_orders.return_value = []

    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        settings={"enabled": True, "dry_run": False},
        client=None, client_factory=lambda *a, **k: fake_client,
        bars_fetcher=lambda sym: [],
    )

    assert res["status"] == "OK"
    assert calls["n"] >= 2, (
        f"expected order_sync at start AND end of pass, saw {calls['n']}")
