"""
quarantine_negative_edge.py — Sprint-1 M2 quarantine of negative-edge
strategies via the existing pause mechanism.

Three strategies are evidence-backed money-losers and are paused (not
deleted — we keep their history for the MFE/MAE measurement work in M1):

  - intraday-1m-orb            (-$56 live, 22% WR; ORB backtest -12.9% vs
                                SPY +22.6%)
  - intraday-1m-vwap-reclaim   (negative live + backtest)
  - botnet101-consec-bearish   (PF 0.95, -0.06%/trade, n=168)

Paused indefinitely (pause_days=None) so they stay quarantined until a
human un-pauses them after a genuine edge re-emerges. Idempotent — the
underlying pause_strategy UPSERTs on strategy_id, so re-running just
refreshes paused_at.

Run from the project root:
  py -3.13 -m scripts.quarantine_negative_edge
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402

QUARANTINE_REASON = "sprint1: negative edge"
QUARANTINE_SOURCE = "sprint1_quarantine"

# (strategy_id, evidence note kept with the pause row)
QUARANTINE: List[tuple] = [
    ("intraday-1m-orb",
     "-$56 live, 22% WR; ORB backtest -12.9% vs SPY +22.6%"),
    ("intraday-1m-vwap-reclaim",
     "negative live + backtest"),
    ("botnet101-consec-bearish",
     "PF 0.95, -0.06%/trade, n=168"),
]


def quarantine(conn, *, now_iso=None) -> List[Dict]:
    """Pause every QUARANTINE strategy indefinitely. Returns the pause rows."""
    results = []
    for sid, evidence in QUARANTINE:
        res = sh.pause_strategy(
            conn, sid,
            reason=f"{QUARANTINE_REASON} — {evidence}",
            source=QUARANTINE_SOURCE,
            pause_days=None,  # indefinite — un-pause manually
            now_iso=now_iso,
        )
        results.append(res)
    return results


def main() -> int:
    conn = db.init_db()
    try:
        rows = quarantine(conn)
        for r in rows:
            print(f"PAUSED {r['strategy_id']:<28} reason={r['reason']}")
        print(f"\nquarantined {len(rows)} strategies (indefinite).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
