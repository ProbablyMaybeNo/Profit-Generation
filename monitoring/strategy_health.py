"""
strategy_health.py — Detect strategies whose recent edge has decayed
versus their all-time record.

For each strategy with ≥ all_time_min_n closed 1d outcomes, computes:
  - all_time_sharpe over every closed outcome
  - last_n_sharpe over the most recent recent_n outcomes (default 30)

A strategy is flagged as degraded when the all-time Sharpe is meaningfully
positive (> 0.05) AND the recent Sharpe is below
`degradation_ratio` * all_time_sharpe (default 0.5).

The CLI fires a Telegram alert per newly-degraded strategy, persisting
the last-alert ISO in `meta` so we don't spam — a degradation has to
re-trip (recover and re-degrade) to alert again.

The dashboard reads `compute_strategy_health(conn)` directly and renders
a yellow warning icon on the matching strategy_edge row.
"""

from __future__ import annotations

import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402


DEFAULT_RECENT_N = 30
DEFAULT_ALL_TIME_MIN_N = 30
DEFAULT_DEGRADATION_RATIO = 0.5
DEGRADATION_BASELINE_SHARPE = 0.05
ALERT_META_KEY_PREFIX = "strategy_health.alerted:"


def _sharpe_ish(rets: List[float]) -> float:
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    sd = statistics.stdev(rets)
    return (mean / sd) if sd > 0 else 0.0


def _closed_returns_for_strategy(conn, strategy_id: str) -> List[float]:
    rows = conn.execute(
        "SELECT o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval='1d' AND s.strategy_id=? "
        " ORDER BY o.exit_ts ASC, o.signal_id ASC",
        (strategy_id,),
    ).fetchall()
    return [float(r["return_pct"]) for r in rows]


def evaluate_strategy(
    conn, strategy_id: str,
    *,
    recent_n: int = DEFAULT_RECENT_N,
    all_time_min_n: int = DEFAULT_ALL_TIME_MIN_N,
    degradation_ratio: float = DEFAULT_DEGRADATION_RATIO,
    baseline_sharpe: float = DEGRADATION_BASELINE_SHARPE,
) -> Dict:
    """Return a single-strategy health row.

    Shape:
      {"strategy_id", "n_total", "n_recent", "all_time_sharpe",
       "last_n_sharpe", "ratio", "degraded", "reason"}

    `degraded=False` for strategies with too few outcomes or with a
    non-positive all-time Sharpe (nothing to degrade from).
    """
    rets = _closed_returns_for_strategy(conn, strategy_id)
    n_total = len(rets)
    out = {
        "strategy_id": strategy_id,
        "n_total": n_total,
        "n_recent": 0,
        "all_time_sharpe": 0.0,
        "last_n_sharpe": 0.0,
        "ratio": 0.0,
        "degraded": False,
        "reason": "",
    }
    if n_total < max(all_time_min_n, 2):
        return out
    all_time = _sharpe_ish(rets)
    n_recent = min(recent_n, n_total)
    last_rets = rets[-n_recent:]
    last_n = _sharpe_ish(last_rets)
    out["all_time_sharpe"] = round(all_time, 4)
    out["last_n_sharpe"] = round(last_n, 4)
    out["n_recent"] = n_recent
    if all_time <= baseline_sharpe:
        # Nothing meaningfully positive to degrade from.
        return out
    threshold = degradation_ratio * all_time
    ratio = (last_n / all_time) if all_time > 0 else 0.0
    out["ratio"] = round(ratio, 4)
    if last_n < threshold:
        out["degraded"] = True
        out["reason"] = (
            f"last {n_recent} trades Sharpe-ish {last_n:.3f} "
            f"is < {degradation_ratio*100:.0f}% of all-time "
            f"{all_time:.3f}"
        )
    return out


def compute_strategy_health(
    conn,
    *,
    recent_n: int = DEFAULT_RECENT_N,
    all_time_min_n: int = DEFAULT_ALL_TIME_MIN_N,
    degradation_ratio: float = DEFAULT_DEGRADATION_RATIO,
) -> List[Dict]:
    """Health summary for every strategy that has closed 1d outcomes.

    Output sorted: degraded strategies first, then by all_time_sharpe desc.
    """
    rows = conn.execute(
        "SELECT DISTINCT s.strategy_id FROM signals s "
        " JOIN outcomes o ON o.signal_id = s.id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval='1d'"
    ).fetchall()
    out: List[Dict] = []
    for r in rows:
        out.append(evaluate_strategy(
            conn, r["strategy_id"],
            recent_n=recent_n,
            all_time_min_n=all_time_min_n,
            degradation_ratio=degradation_ratio,
        ))
    out.sort(key=lambda x: (not x["degraded"],
                             -x["all_time_sharpe"],
                             x["strategy_id"]))
    return out


def _read_last_alert(conn, strategy_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?",
        (ALERT_META_KEY_PREFIX + strategy_id,),
    ).fetchone()
    return row["value"] if row else None


def _record_alert(conn, strategy_id: str, when_iso: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (ALERT_META_KEY_PREFIX + strategy_id, when_iso),
        )


def _clear_alert(conn, strategy_id: str) -> None:
    with conn:
        conn.execute(
            "DELETE FROM meta WHERE key=?",
            (ALERT_META_KEY_PREFIX + strategy_id,),
        )


def fire_alerts(
    conn, health_rows: List[Dict],
    *,
    send_fn=None,
    now_iso: Optional[str] = None,
) -> List[Dict]:
    """Send a Telegram alert per newly-degraded strategy. Returns the
    list of strategies that were alerted (deduped via meta)."""
    from monitoring.telegram_alerter import send_message
    sender = send_fn or send_message
    now = now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")
    fired: List[Dict] = []
    for row in health_rows:
        sid = row["strategy_id"]
        if not row["degraded"]:
            # Recovery: drop the dedupe key so a future degradation alerts.
            if _read_last_alert(conn, sid) is not None:
                _clear_alert(conn, sid)
            continue
        if _read_last_alert(conn, sid) is not None:
            continue
        text = (
            "\U000026A0\U0000FE0F *Strategy degradation* — "
            f"`{sid}`\n"
            f"last {row['n_recent']} trades Sharpe-ish "
            f"*{row['last_n_sharpe']:.3f}* vs all-time "
            f"*{row['all_time_sharpe']:.3f}* "
            f"(ratio {row['ratio']:.2f})"
        )
        if sender(text):
            _record_alert(conn, sid, now)
            fired.append(row)
    return fired


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--recent-n", type=int, default=DEFAULT_RECENT_N)
    parser.add_argument("--min-n", type=int, default=DEFAULT_ALL_TIME_MIN_N)
    parser.add_argument("--ratio", type=float,
                        default=DEFAULT_DEGRADATION_RATIO)
    parser.add_argument("--no-alert", action="store_true",
                        help="Compute + print rollup without sending alerts")
    args = parser.parse_args()

    conn = db.init_db()
    try:
        rows = compute_strategy_health(
            conn, recent_n=args.recent_n,
            all_time_min_n=args.min_n,
            degradation_ratio=args.ratio,
        )
        degraded = [r for r in rows if r["degraded"]]
        log(f"{len(degraded)}/{len(rows)} strategies flagged as degraded",
            "INFO" if not degraded else "WARNING")
        for r in rows:
            tag = "DEGRADED" if r["degraded"] else "ok"
            log(
                f"  [{tag}] {r['strategy_id']}: "
                f"all-time={r['all_time_sharpe']:.3f}, "
                f"last_{r['n_recent']}={r['last_n_sharpe']:.3f}, "
                f"ratio={r['ratio']:.2f}",
                "INFO",
            )
        if not args.no_alert and degraded:
            fired = fire_alerts(conn, rows)
            log(f"Telegram alerts fired: {len(fired)}", "SUCCESS")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
