"""
reset_to_donchian_only.py — Sprint 3 / RESET.

Hard reset to a single proven strategy. Pauses EVERY strategy that has fired
recently EXCEPT the keep-set (trend-donchian-breakout-20), via the existing
pause mechanism (strategy_health.pause_strategy -> paused_strategies). The
auto_trader entry gate refuses new entries on paused strategies while leaving
outcome tracking intact, so nothing is deleted — strategies keep recording
observe-only outcomes and can be reintroduced one at a time later, behind kill
gates, once the execution core (one-symbol/one-owner/one-order-authority) is
rebuilt.

Rationale: the intraday + marginal strategies are the source of the order
conflicts, accidental shorts, and churn — and they lose money. Donchian trend
is the only repeatedly-positive edge and is daily (no intraday flatten chaos).
Run it alone, clean, and prove it before adding anything back.

Idempotent. Run from project root:
  py -3.13 -m scripts.reset_to_donchian_only
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402

KEEP = {"trend-donchian-breakout-20"}
PAUSE_REASON = "Sprint3 RESET: Donchian-only — strip to the proven edge while the execution core is rebuilt"
PAUSE_SOURCE = "sprint3_reset_donchian_only"
LOOKBACK_DAYS = 45


def active_strategies(conn) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT strategy_id FROM signals "
        "WHERE bar_ts >= date('now', ?) AND strategy_id IS NOT NULL "
        "ORDER BY strategy_id",
        (f"-{LOOKBACK_DAYS} day",),
    ).fetchall()
    return [r[0] for r in rows]


def main() -> int:
    conn = db.init_db()
    try:
        active = active_strategies(conn)
        to_pause = [s for s in active if s not in KEEP]
        for sid in to_pause:
            r = sh.pause_strategy(
                conn, sid, reason=PAUSE_REASON, source=PAUSE_SOURCE,
                pause_days=None,
            )
            print(f"PAUSED  {r['strategy_id']}")
        kept = [s for s in active if s in KEEP]
        print(f"\nKEPT ACTIVE: {kept or list(KEEP)}")
        print(f"paused {len(to_pause)} strategies; {len(kept)} kept active "
              f"(Donchian-only reset).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
