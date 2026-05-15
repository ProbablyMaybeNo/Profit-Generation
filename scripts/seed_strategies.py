"""
seed_strategies.py — Backfill the strategies table from the persistent
strategy log bundle (records.jsonl) and overlay the active_on universe +
compute_fn from monitoring/config.TRACKED_STRATEGIES.

Idempotent. Run from the project root:

  py -3.13 scripts/seed_strategies.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402

RECORDS_PATH = (
    ROOT / "data" / "scrapes"
    / "tradingview-in-daytrading-strategies-2026-04-26" / "records.jsonl"
)


def _load_records():
    if not RECORDS_PATH.exists():
        raise FileNotFoundError(f"strategy bundle not found: {RECORDS_PATH}")
    out = []
    with RECORDS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _load_tracked():
    """Pull active_on + compute_fn overlays from monitoring/config.py."""
    try:
        from monitoring.config import TRACKED_STRATEGIES
    except Exception as e:
        print(f"warning: could not import TRACKED_STRATEGIES ({e}); skipping overlay")
        return {}
    return {entry["id"]: entry for entry in TRACKED_STRATEGIES}


def main() -> int:
    records = _load_records()
    tracked = _load_tracked()
    conn = db.init_db()

    inserted_or_updated = 0
    overlaid = 0
    skipped = 0
    for r in records:
        extra = r.get("extra", {})
        sid = extra.get("strategy_id") or r.get("strategy_id")
        if not sid:
            skipped += 1
            continue
        db.upsert_strategy(conn, r)
        inserted_or_updated += 1
        if sid in tracked:
            db.set_strategy_active_on(
                conn,
                strategy_id=sid,
                symbols=tracked[sid]["active_on"],
                compute_fn=tracked[sid].get("compute"),
            )
            overlaid += 1

    total = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    by_verdict = conn.execute(
        "SELECT current_verdict, COUNT(*) FROM strategies GROUP BY current_verdict"
    ).fetchall()
    conn.close()

    print(f"seed complete: upserted={inserted_or_updated}  overlaid={overlaid}  skipped={skipped}")
    print(f"strategies in db: {total}")
    for v, n in by_verdict:
        print(f"  {v or '(null)'}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
