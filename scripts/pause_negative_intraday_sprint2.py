"""
pause_negative_intraday_sprint2.py — Sprint 2 / M3.

Recent closed-outcome stats flag a cohort of intraday strategies as
negative-expectancy with heavy churn (momentum alone fired 3,254 exit signals
in a day). They are set to OBSERVE-ONLY via the existing pause mechanism
(strategy_health.pause_strategy → paused_strategies row). The auto_trader entry
gate already refuses entries on paused strategies while leaving exits and
outcome-tracking untouched, so these keep recording outcomes for re-evaluation
and are NOT deleted.

Donchian breakout (trend-donchian-breakout-20) and the small MR/botnet variants
are deliberately NOT in this list — they stay active.

Paused indefinitely (pause_days=None) until a human un-pauses after a genuine
edge re-emerges. Idempotent — pause_strategy UPSERTs on strategy_id.

Run from the project root:
  py -3.13 -m scripts.pause_negative_intraday_sprint2
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402

PAUSE_REASON = "sprint2 M3: negative-expectancy intraday — observe-only"
PAUSE_SOURCE = "sprint2_negative_intraday"

# (strategy_id, evidence note from recent closed-outcome stats)
NEGATIVE_INTRADAY: List[tuple] = [
    ("intraday-1m-momentum", "-0.42% avg; 3,254 exit signals/day churn"),
    ("intraday-1m-vwap-reclaim", "-0.57% avg"),
    ("intraday-1m-orb", "-0.43% avg"),
    ("intraday-orb-pivots-5m", "-1.84% avg, 0% win"),
    ("intraday-orbo-5m", "-1.45% avg"),
    ("rsi2-oversold", "-6.5% avg (toxic)"),
]


def pause_negative_intraday(conn, *, now_iso=None) -> List[Dict]:
    """Pause every NEGATIVE_INTRADAY strategy indefinitely. Returns pause rows."""
    results = []
    for sid, evidence in NEGATIVE_INTRADAY:
        res = sh.pause_strategy(
            conn, sid,
            reason=f"{PAUSE_REASON} — {evidence}",
            source=PAUSE_SOURCE,
            pause_days=None,  # indefinite — un-pause manually after re-eval
            now_iso=now_iso,
        )
        results.append(res)
    return results


def main() -> int:
    conn = db.init_db()
    try:
        rows = pause_negative_intraday(conn)
        for r in rows:
            print(f"PAUSED {r['strategy_id']:<28} reason={r['reason']}")
        print(f"\npaused {len(rows)} negative-expectancy intraday strategies "
              f"(observe-only, indefinite).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
