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

import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
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

# Auto-pause defaults (3.3.4). If the last N LIVE outcomes mean return is
# below `live_pause_ratio * backtest_mean`, the strategy is auto-paused.
# Pause persists until `pause_days` elapse or manual unpause.
DEFAULT_LIVE_N = 20
DEFAULT_LIVE_PAUSE_RATIO = 0.3
DEFAULT_PAUSE_DAYS = 30


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


# ---------------------------------------------------------------------------
# Auto-pause on live divergence (3.3.4)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _live_outcomes_for_strategy(
    conn, strategy_id: str, *, limit: int = DEFAULT_LIVE_N,
) -> List[float]:
    """Closed outcomes for paper-traded entries — i.e. live-tracked.

    'Live' here means there is a `paper_trades` row backing the signal
    that opened the outcome. Pure backtest/validator runs never write
    to `paper_trades`, so this excludes them cleanly.
    """
    rows = conn.execute(
        "SELECT o.return_pct "
        "  FROM outcomes o "
        "  JOIN signals s ON s.id = o.signal_id "
        "  JOIN paper_trades pt ON pt.signal_id = o.signal_id "
        "                       AND pt.side = 'buy' "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        "   AND s.strategy_id = ? "
        " GROUP BY o.signal_id "
        " ORDER BY o.exit_ts DESC, o.signal_id DESC "
        " LIMIT ?",
        (strategy_id, int(limit)),
    ).fetchall()
    return [float(r["return_pct"]) for r in rows]


def backtest_mean_return_pct(conn, strategy_id: str) -> Optional[float]:
    """Mean per-trade return from the strategy's validator test_runs.

    Reads `strategies.raw_record_json`, extracts every `test_run` with
    `trades>0` and `total_return_pct` present, and returns the mean of
    (total_return_pct / trades) across runs.

    Returns None when the strategy is unknown or has no usable test_runs.
    Negative means are returned as-is (so a strategy that backtested
    negative will never trigger auto-pause — there's no positive edge
    to diverge from).
    """
    row = conn.execute(
        "SELECT raw_record_json FROM strategies WHERE strategy_id=?",
        (strategy_id,),
    ).fetchone()
    if row is None or not row["raw_record_json"]:
        return None
    try:
        rec = json.loads(row["raw_record_json"])
    except Exception:
        return None
    test_runs = ((rec.get("extra") or {}).get("test_runs") or [])
    per_trade_returns: List[float] = []
    for tr in test_runs:
        try:
            trades = int(tr.get("trades") or 0)
            total = tr.get("total_return_pct")
            if trades > 0 and total is not None:
                per_trade_returns.append(float(total) / trades)
        except Exception:
            continue
    if not per_trade_returns:
        return None
    return sum(per_trade_returns) / len(per_trade_returns)


def evaluate_live_divergence(
    conn, strategy_id: str,
    *,
    live_n: int = DEFAULT_LIVE_N,
    pause_ratio: float = DEFAULT_LIVE_PAUSE_RATIO,
) -> Dict:
    """Per-strategy live-vs-backtest divergence check. Pure function.

    Returns:
      {strategy_id, n_live, live_mean_pct, backtest_mean_pct,
       ratio, should_pause, reason}

    `should_pause=True` only when:
      - we have >= live_n closed live outcomes
      - backtest_mean_pct exists and is positive (nothing to diverge from
        on a negative backtest)
      - live_mean_pct < pause_ratio * backtest_mean_pct
    """
    out = {
        "strategy_id": strategy_id,
        "n_live": 0,
        "live_mean_pct": 0.0,
        "backtest_mean_pct": None,
        "ratio": 0.0,
        "should_pause": False,
        "reason": "",
    }
    live = _live_outcomes_for_strategy(conn, strategy_id, limit=live_n)
    n = len(live)
    out["n_live"] = n
    if n < live_n:
        out["reason"] = f"only {n} live outcomes (need {live_n})"
        return out
    live_mean = sum(live) / n
    out["live_mean_pct"] = round(live_mean, 4)
    bt = backtest_mean_return_pct(conn, strategy_id)
    out["backtest_mean_pct"] = round(bt, 4) if bt is not None else None
    if bt is None:
        out["reason"] = "no backtest mean available"
        return out
    if bt <= 0:
        out["reason"] = (
            f"backtest mean {bt:.4f}% is non-positive; nothing to diverge "
            f"from — auto-pause skipped"
        )
        return out
    ratio = live_mean / bt
    out["ratio"] = round(ratio, 4)
    threshold = pause_ratio * bt
    if live_mean < threshold:
        out["should_pause"] = True
        out["reason"] = (
            f"last {n} live trades mean {live_mean:+.4f}% is "
            f"< {pause_ratio*100:.0f}% of backtest mean "
            f"{bt:+.4f}% (ratio {ratio:.2f})"
        )
    return out


def is_paused(
    conn, strategy_id: str, *, asof_iso: Optional[str] = None,
) -> bool:
    """True iff a non-expired paused_strategies row exists for the
    strategy. Expired rows are NOT auto-deleted by this call — they
    simply return False so the strategy is treated as live.
    """
    asof = asof_iso or _utc_now_iso()
    row = conn.execute(
        "SELECT expires_at FROM paused_strategies WHERE strategy_id=?",
        (strategy_id,),
    ).fetchone()
    if row is None:
        return False
    if row["expires_at"] is None:
        return True  # indefinite pause
    return row["expires_at"] > asof


def list_paused(conn, *, include_expired: bool = False) -> List[Dict]:
    asof = _utc_now_iso()
    rows = conn.execute(
        "SELECT strategy_id, reason, paused_at, expires_at, source, "
        "       live_mean_pct, backtest_mean_pct, sample_size "
        "  FROM paused_strategies ORDER BY paused_at DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if not include_expired and d["expires_at"] is not None and d["expires_at"] <= asof:
            continue
        out.append(d)
    return out


def pause_strategy(
    conn, strategy_id: str, *,
    reason: str,
    source: str = "auto_pause",
    pause_days: Optional[int] = DEFAULT_PAUSE_DAYS,
    live_mean_pct: Optional[float] = None,
    backtest_mean_pct: Optional[float] = None,
    sample_size: Optional[int] = None,
    now_iso: Optional[str] = None,
) -> Dict:
    """Write a paused_strategies row. Idempotent (UPSERT on strategy_id)."""
    paused_at = now_iso or _utc_now_iso()
    expires_at: Optional[str] = None
    if pause_days is not None and pause_days > 0:
        base = datetime.fromisoformat(paused_at.replace("Z", "+00:00"))
        expires_at = (base + timedelta(days=int(pause_days))).isoformat(
            timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO paused_strategies"
            " (strategy_id, reason, paused_at, expires_at, source, "
            "  live_mean_pct, backtest_mean_pct, sample_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(strategy_id) DO UPDATE SET "
            "  reason=excluded.reason, paused_at=excluded.paused_at, "
            "  expires_at=excluded.expires_at, source=excluded.source, "
            "  live_mean_pct=excluded.live_mean_pct, "
            "  backtest_mean_pct=excluded.backtest_mean_pct, "
            "  sample_size=excluded.sample_size",
            (strategy_id, reason, paused_at, expires_at, source,
             live_mean_pct, backtest_mean_pct, sample_size),
        )
    return {
        "strategy_id": strategy_id, "paused_at": paused_at,
        "expires_at": expires_at, "reason": reason, "source": source,
    }


def unpause_strategy(conn, strategy_id: str) -> bool:
    """Remove a pause row. Returns True iff a row was actually deleted."""
    with conn:
        cur = conn.execute(
            "DELETE FROM paused_strategies WHERE strategy_id=?",
            (strategy_id,),
        )
    return cur.rowcount > 0


def _send_pause_alert(
    *, sid: str, action: str, reason: str = "", send_fn=None,
) -> bool:
    """Telegram-notify a pause/unpause event. Returns send_fn's bool."""
    try:
        if send_fn is None:
            from monitoring.telegram_alerter import send_message as send_fn
    except Exception as e:
        log(f"telegram unavailable for pause alert ({e}); skipping", "WARNING")
        return False
    if action == "PAUSED":
        text = (
            f"\U000026D4 *Strategy auto-paused* — `{sid}`\n{reason}"
            if reason else f"\U000026D4 *Strategy auto-paused* — `{sid}`"
        )
    else:
        text = f"\U00002705 *Strategy unpaused* — `{sid}`"
    try:
        return bool(send_fn(text))
    except Exception as e:
        log(f"pause alert send failed: {e}", "WARNING")
        return False


def auto_pause_check(
    conn, *,
    live_n: int = DEFAULT_LIVE_N,
    pause_ratio: float = DEFAULT_LIVE_PAUSE_RATIO,
    pause_days: int = DEFAULT_PAUSE_DAYS,
    send_fn=None,
    now_iso: Optional[str] = None,
) -> List[Dict]:
    """Scan every tracked strategy with closed live outcomes and pause
    those whose live mean has diverged below `pause_ratio * backtest_mean`.

    Returns a list of `{strategy_id, action, ...}` dicts for every newly-
    paused strategy. Already-paused (non-expired) strategies are skipped
    silently. Each pause fires a Telegram alert.
    """
    fired: List[Dict] = []
    rows = conn.execute(
        "SELECT DISTINCT s.strategy_id "
        "  FROM signals s "
        "  JOIN outcomes o ON o.signal_id = s.id "
        "  JOIN paper_trades pt ON pt.signal_id = s.id AND pt.side='buy' "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL"
    ).fetchall()
    for r in rows:
        sid = r["strategy_id"]
        if is_paused(conn, sid, asof_iso=now_iso):
            continue
        result = evaluate_live_divergence(
            conn, sid, live_n=live_n, pause_ratio=pause_ratio,
        )
        if not result["should_pause"]:
            continue
        paused = pause_strategy(
            conn, sid,
            reason=result["reason"],
            source="auto_pause",
            pause_days=pause_days,
            live_mean_pct=result["live_mean_pct"],
            backtest_mean_pct=result["backtest_mean_pct"],
            sample_size=result["n_live"],
            now_iso=now_iso,
        )
        _send_pause_alert(
            sid=sid, action="PAUSED", reason=result["reason"], send_fn=send_fn,
        )
        fired.append({**paused, "action": "PAUSED", **{
            k: result[k] for k in
            ("live_mean_pct", "backtest_mean_pct", "ratio", "n_live")
        }})
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
    parser.add_argument("--auto-pause", action="store_true",
                        help="Run the live-vs-backtest divergence check and "
                             "auto-pause strategies whose live mean is < "
                             "30%% of their backtest mean")
    parser.add_argument("--live-n", type=int, default=DEFAULT_LIVE_N,
                        help="Live-outcomes sample size for auto-pause")
    parser.add_argument("--pause-ratio", type=float,
                        default=DEFAULT_LIVE_PAUSE_RATIO,
                        help="Pause when live_mean < pause_ratio * backtest_mean")
    parser.add_argument("--pause-days", type=int, default=DEFAULT_PAUSE_DAYS,
                        help="Auto-pause duration in days (0 = indefinite)")
    parser.add_argument("--unpause", metavar="STRATEGY_ID",
                        help="Manually remove a paused_strategies row + alert")
    parser.add_argument("--list-paused", action="store_true",
                        help="Print currently-paused strategies and exit")
    args = parser.parse_args()

    conn = db.init_db()
    try:
        if args.list_paused:
            rows = list_paused(conn)
            if not rows:
                log("no paused strategies", "INFO")
            for r in rows:
                log(
                    f"  [PAUSED] {r['strategy_id']}: source={r['source']} "
                    f"paused_at={r['paused_at']} expires_at={r['expires_at']} "
                    f"reason={r['reason']}",
                    "WARNING",
                )
            return

        if args.unpause:
            removed = unpause_strategy(conn, args.unpause)
            if removed:
                _send_pause_alert(sid=args.unpause, action="UNPAUSED")
                log(f"unpaused {args.unpause}", "SUCCESS")
            else:
                log(f"{args.unpause} was not paused", "INFO")
            return

        if args.auto_pause:
            fired = auto_pause_check(
                conn, live_n=args.live_n,
                pause_ratio=args.pause_ratio,
                pause_days=args.pause_days,
            )
            log(f"auto-pause: {len(fired)} strategies newly paused",
                "WARNING" if fired else "INFO")
            for f in fired:
                log(
                    f"  [PAUSED] {f['strategy_id']}: "
                    f"live={f['live_mean_pct']:+.4f}% "
                    f"backtest={f['backtest_mean_pct']:+.4f}% "
                    f"ratio={f['ratio']:.2f} expires_at={f['expires_at']}",
                    "WARNING",
                )
            return

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
