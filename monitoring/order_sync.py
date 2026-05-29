"""
order_sync.py — Backfill paper_trades fill state from Alpaca.

The auto-trader records a paper_trades row at submit time, when a market
order is still 'accepted' / 'pending_new' with no fill price. Nothing else
re-queries the broker, so rows stay stuck at 'accepted' with NULL
fill_price / filled_at forever — which starves fill-latency stats, equity
attribution, the eligibility record (outcomes never see real fills), and
the reconciliation drift report.

sync_order_fills walks every paper_trades row in a non-terminal status,
asks Alpaca for the order's current state, and upserts status / fill_price /
filled_at back through db.record_paper_trade. Idempotent: rows already in a
terminal status are skipped, and re-running only advances state forward
(a row is rewritten only when its status changed or it gained a fill price).

CLI:
  py -3.13 -m monitoring.order_sync           # run once, print summary
  py -3.13 -m monitoring.order_sync --json    # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402

TERMINAL_STATUSES = ("filled", "canceled", "rejected", "expired")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pending_orders(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every paper_trades row with a broker order id that hasn't reached a
    terminal status. NULL status counts as pending (submit-time crash)."""
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    return conn.execute(
        f"SELECT * FROM paper_trades "
        f" WHERE alpaca_order_id IS NOT NULL AND alpaca_order_id != '' "
        f"   AND (status IS NULL OR status NOT IN ({placeholders}))",
        TERMINAL_STATUSES,
    ).fetchall()


def sync_order_fills(conn, client, *, now_iso: Optional[str] = None) -> Dict:
    """Re-query Alpaca for every non-terminal paper_trades order and backfill
    status / fill_price / filled_at.

    A row is rewritten only when the broker status differs from what we have,
    or when the broker now reports a fill price we hadn't recorded — so
    re-running is cheap and never thrashes unchanged rows.

    Returns {checked, updated, filled, errors}.
    """
    now_iso = now_iso or _utc_now_iso()
    pending = _pending_orders(conn)
    n_updated = n_filled = n_errors = 0
    for row in pending:
        order_id = row["alpaca_order_id"]
        try:
            order = client.get_order_by_id(order_id)
        except Exception as e:
            log(f"order_sync: get_order_by_id({order_id}) failed: {e}",
                "WARNING")
            n_errors += 1
            continue
        # Pass the raw broker status (an OrderStatus enum) through the
        # canonical normalizer rather than str().lower() — the latter
        # mangles "OrderStatus.FILLED" into "orderstatus.filled" by
        # defeating the suffix-stripping in db._normalize_order_status.
        raw_status = getattr(order, "status", None)
        status = db._normalize_order_status(raw_status)
        if not status:
            continue
        fill_price = float(getattr(order, "filled_avg_price", 0) or 0) or None
        filled_at_raw = getattr(order, "filled_at", None)
        filled_at = str(filled_at_raw) if filled_at_raw else None
        prev_status = db._normalize_order_status(row["status"])
        gained_fill = fill_price is not None and row["fill_price"] is None
        if status == prev_status and not gained_fill:
            continue
        db.record_paper_trade(conn, {
            "alpaca_order_id": order_id,
            "signal_id": row["signal_id"],
            "strategy_id": row["strategy_id"],
            "symbol": row["symbol"],
            "side": row["side"],
            "qty": row["qty"],
            "order_type": row["order_type"],
            "limit_price": row["limit_price"],
            "stop_price": row["stop_price"],
            "submitted_at": row["submitted_at"],
            "filled_at": filled_at,
            "fill_price": fill_price,
            "status": raw_status,
            "notes": row["notes"],
            "entry_stops": row["entry_stops"],
        })
        n_updated += 1
        if status == "filled":
            n_filled += 1
    return {"checked": len(pending), "updated": n_updated,
            "filled": n_filled, "errors": n_errors}


def main():
    parser = argparse.ArgumentParser(
        description="Backfill paper_trades fill state from Alpaca.")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of a one-line summary")
    args = parser.parse_args()
    from config.utils import get_alpaca_client
    conn = db.init_db()
    try:
        client = get_alpaca_client()
        result = sync_order_fills(conn, client)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"order_sync: checked={result['checked']} "
              f"updated={result['updated']} filled={result['filled']} "
              f"errors={result['errors']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
