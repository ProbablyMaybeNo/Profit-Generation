"""
kelly_promotion.py — 7.4.1 Kelly tier promotion machinery.

Each strategy's Kelly tier defaults to "quarter" (¼ Kelly, the cap
shipped in 6.2.2). When a strategy demonstrates stability over 200+
closed paper outcomes, it earns the right to promote to "half"
(½ Kelly proper). Promotion is not automatic — it fires a Telegram
alert asking Ross to confirm; without confirmation the tier stays put.

Thresholds (all three must hold):
  - n_closed >= 200
  - live_win_rate within ±5% of the backtest win_rate weighted mean
  - live_max_drawdown_pct <= 1.5 × backtest_max_drawdown_pct

The kelly_tier column lives on the strategies table. A separate
`kelly_tier_alerts` row records the alert-sent state so each strategy
gets at most one alert per (current_tier, candidate_tier) pair.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


PROMOTION_MIN_CLOSED = 200
PROMOTION_WIN_RATE_TOLERANCE = 0.05  # ±5 percentage points
PROMOTION_MAX_DD_MULTIPLIER = 1.5

KELLY_TIER_QUARTER = "quarter"
KELLY_TIER_HALF = "half"

# Promotion ladder. Currently quarter → half is the only step shipped.
PROMOTION_LADDER: Dict[str, str] = {
    KELLY_TIER_QUARTER: KELLY_TIER_HALF,
}


# ---------------------------------------------------------------------------
# Schema migration — kelly_tier column on strategies + alert log
# ---------------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent column add + alert-log table. Safe to call repeatedly."""
    cols = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(strategies)"
        ).fetchall()
    }
    if "kelly_tier" not in cols:
        with conn:
            conn.execute(
                "ALTER TABLE strategies ADD COLUMN kelly_tier TEXT"
            )
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kelly_tier_alerts (
                strategy_id     TEXT NOT NULL,
                current_tier    TEXT NOT NULL,
                candidate_tier  TEXT NOT NULL,
                alerted_at      TEXT NOT NULL,
                PRIMARY KEY(strategy_id, current_tier, candidate_tier)
            )
        """)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Tier read / write
# ---------------------------------------------------------------------------

def get_current_tier(
    conn: sqlite3.Connection, strategy_id: str,
) -> str:
    """Return the strategy's current kelly_tier, defaulting to 'quarter'."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT kelly_tier FROM strategies WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()
    if row is None or row["kelly_tier"] is None:
        return KELLY_TIER_QUARTER
    return str(row["kelly_tier"])


def set_tier(
    conn: sqlite3.Connection, strategy_id: str, tier: str,
) -> None:
    """Update a strategy's kelly_tier. Strategy row must already exist."""
    ensure_schema(conn)
    with conn:
        conn.execute(
            "UPDATE strategies SET kelly_tier = ? WHERE strategy_id = ?",
            (tier, strategy_id),
        )


# ---------------------------------------------------------------------------
# Stats — live and backtest
# ---------------------------------------------------------------------------

def compute_live_stats(
    conn: sqlite3.Connection, strategy_id: str,
) -> Dict[str, Any]:
    """Closed paper-outcome stats: ``{n, win_rate, max_drawdown_pct}``.

    max_drawdown_pct is computed from the cumulative return series
    (compounding each trade at unit size). Returns 0 / 0.0 across the
    board when no closed outcomes exist.
    """
    rows = conn.execute(
        "SELECT o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        "   AND s.strategy_id = ? "
        " ORDER BY o.exit_ts ASC, o.signal_id ASC",
        (strategy_id,),
    ).fetchall()
    rets = [float(r["return_pct"]) for r in rows]
    n = len(rets)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "max_drawdown_pct": 0.0}
    wins = sum(1 for r in rets if r > 0)
    # Equity curve with 1% trade fractional sizing (mirrors dashboard).
    size_frac = 0.10
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rets:
        equity = max(0.0, equity * (1.0 + size_frac * r / 100.0))
        if equity > peak:
            peak = equity
        dd = (equity - peak) / peak * 100.0 if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    return {
        "n": n,
        "win_rate": round(wins / n, 4),
        "max_drawdown_pct": round(max_dd, 4),
    }


def compute_backtest_stats(
    record: Dict[str, Any],
) -> Dict[str, Any]:
    """Extract weighted backtest stats from a raw_record_json dict.

    Looks at extra.test_runs[*]: weighted average of win_rate_pct by
    trades count, and the worst max_drawdown_pct across runs (most
    conservative bound). Returns ``{win_rate, max_drawdown_pct,
    n_runs_used}``; values are None when test_runs is missing.
    """
    extra = (record.get("extra") or {}) if isinstance(record, dict) else {}
    runs = extra.get("test_runs") or []
    valid = [
        r for r in runs
        if not r.get("scenario") and (r.get("verdict") or "").upper() != "INFO"
    ]
    win_weighted_sum = 0.0
    total_trades = 0
    worst_dd = None
    used = 0
    for r in valid:
        trades = r.get("trades")
        wr = r.get("win_rate_pct")
        dd = r.get("max_drawdown_pct")
        if trades in (None, 0) or wr is None:
            continue
        try:
            tr = int(trades)
            wr_f = float(wr)
        except (TypeError, ValueError):
            continue
        win_weighted_sum += wr_f * tr
        total_trades += tr
        used += 1
        if dd is not None:
            try:
                dd_f = float(dd)
                if worst_dd is None or dd_f < worst_dd:
                    worst_dd = dd_f
            except (TypeError, ValueError):
                pass
    if total_trades == 0:
        return {
            "win_rate": None,
            "max_drawdown_pct": None,
            "n_runs_used": 0,
        }
    return {
        "win_rate": round(win_weighted_sum / total_trades / 100.0, 4),
        "max_drawdown_pct": round(worst_dd, 4) if worst_dd is not None else None,
        "n_runs_used": used,
    }


def fetch_backtest_record(
    conn: sqlite3.Connection, strategy_id: str,
) -> Optional[Dict[str, Any]]:
    """Read raw_record_json for `strategy_id` and parse to dict."""
    row = conn.execute(
        "SELECT raw_record_json FROM strategies WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()
    if row is None or not row["raw_record_json"]:
        return None
    try:
        return json.loads(row["raw_record_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Eligibility evaluation
# ---------------------------------------------------------------------------

def evaluate_promotion_eligibility(
    *,
    live_stats: Dict[str, Any],
    backtest_stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a verdict dict with per-threshold pass/fail flags.

    Shape:
      {
        eligible: bool,
        n_closed: int,
        n_closed_ok: bool,
        win_rate_ok: bool, win_rate_delta: float | None,
        max_dd_ok: bool, max_dd_ratio: float | None,
        reason: str,
      }
    """
    n = int(live_stats.get("n", 0))
    n_ok = n >= PROMOTION_MIN_CLOSED
    live_wr = live_stats.get("win_rate")
    bt_wr = backtest_stats.get("win_rate")
    wr_ok = False
    wr_delta: Optional[float] = None
    if live_wr is not None and bt_wr is not None:
        wr_delta = round(float(live_wr) - float(bt_wr), 4)
        wr_ok = abs(wr_delta) <= PROMOTION_WIN_RATE_TOLERANCE
    live_dd = live_stats.get("max_drawdown_pct")
    bt_dd = backtest_stats.get("max_drawdown_pct")
    dd_ok = False
    dd_ratio: Optional[float] = None
    if live_dd is not None and bt_dd is not None and bt_dd < 0:
        # Both are negative percentages (e.g. -8.0%). The bound is:
        # live_dd >= 1.5 × backtest_dd  (less negative → smaller drawdown)
        bound = PROMOTION_MAX_DD_MULTIPLIER * float(bt_dd)
        dd_ratio = (
            round(float(live_dd) / float(bt_dd), 4)
            if float(bt_dd) != 0 else None
        )
        dd_ok = float(live_dd) >= bound
    elif live_dd is not None and (bt_dd is None or bt_dd == 0):
        # No backtest dd to compare against → defer / fail.
        dd_ok = False
    eligible = bool(n_ok and wr_ok and dd_ok)
    reasons: List[str] = []
    if not n_ok:
        reasons.append(
            f"n_closed={n} < {PROMOTION_MIN_CLOSED}"
        )
    if not wr_ok:
        if bt_wr is None:
            reasons.append("no backtest win_rate")
        elif live_wr is None:
            reasons.append("no live win_rate")
        else:
            reasons.append(
                f"win_rate_delta={wr_delta:.4f} > "
                f"±{PROMOTION_WIN_RATE_TOLERANCE}"
            )
    if not dd_ok:
        if bt_dd is None:
            reasons.append("no backtest max_dd")
        elif live_dd is None:
            reasons.append("no live max_dd")
        else:
            reasons.append(
                f"live_dd={live_dd:.2f}% vs bound "
                f"{PROMOTION_MAX_DD_MULTIPLIER}×{bt_dd:.2f}%"
            )
    return {
        "eligible": eligible,
        "n_closed": n,
        "n_closed_ok": n_ok,
        "win_rate_ok": wr_ok,
        "win_rate_delta": wr_delta,
        "max_dd_ok": dd_ok,
        "max_dd_ratio": dd_ratio,
        "reason": "; ".join(reasons) if reasons else "all checks passed",
    }


# ---------------------------------------------------------------------------
# Alert dedupe — kelly_tier_alerts table
# ---------------------------------------------------------------------------

def alert_already_sent(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    current_tier: str,
    candidate_tier: str,
) -> bool:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT 1 FROM kelly_tier_alerts "
        " WHERE strategy_id=? AND current_tier=? AND candidate_tier=?",
        (strategy_id, current_tier, candidate_tier),
    ).fetchone()
    return row is not None


def record_alert_sent(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    current_tier: str,
    candidate_tier: str,
    now_iso: Optional[str] = None,
) -> None:
    ensure_schema(conn)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO kelly_tier_alerts "
            "(strategy_id, current_tier, candidate_tier, alerted_at) "
            "VALUES (?, ?, ?, ?)",
            (
                strategy_id, current_tier, candidate_tier,
                now_iso or _utc_now_iso(),
            ),
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def evaluate_strategy(
    conn: sqlite3.Connection,
    strategy_id: str,
) -> Dict[str, Any]:
    """Evaluate a single strategy for promotion. Pure read — no alerts sent.

    Returns a dict with the live stats, backtest stats, current tier,
    candidate tier (or None if at the top), and the eligibility verdict.
    """
    ensure_schema(conn)
    current = get_current_tier(conn, strategy_id)
    candidate = PROMOTION_LADDER.get(current)
    live = compute_live_stats(conn, strategy_id)
    rec = fetch_backtest_record(conn, strategy_id) or {}
    backtest = compute_backtest_stats(rec)
    if candidate is None:
        return {
            "strategy_id": strategy_id,
            "current_tier": current,
            "candidate_tier": None,
            "live_stats": live,
            "backtest_stats": backtest,
            "eligibility": {
                "eligible": False,
                "reason": "already at top tier",
            },
        }
    elig = evaluate_promotion_eligibility(
        live_stats=live, backtest_stats=backtest,
    )
    return {
        "strategy_id": strategy_id,
        "current_tier": current,
        "candidate_tier": candidate,
        "live_stats": live,
        "backtest_stats": backtest,
        "eligibility": elig,
    }


def evaluate_all(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Evaluate every strategy in the strategies table. Returns per-strategy verdicts."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT strategy_id FROM strategies ORDER BY strategy_id"
    ).fetchall()
    return [evaluate_strategy(conn, r["strategy_id"]) for r in rows]


def _format_alert(verdict: Dict[str, Any]) -> str:
    """Render a Telegram alert string for a promotion-eligible strategy."""
    sid = verdict.get("strategy_id", "?")
    cur = verdict.get("current_tier", "?")
    cand = verdict.get("candidate_tier", "?")
    live = verdict.get("live_stats", {}) or {}
    bt = verdict.get("backtest_stats", {}) or {}
    from monitoring.telegram_alerter import escape_markdown
    sid_esc = escape_markdown(sid)
    return (
        f"\U0001f4c8 *Kelly tier promotion candidate*\n\n"
        f"Strategy: `{sid_esc}`\n"
        f"Current tier: `{cur}` → candidate: `{cand}`\n\n"
        f"Live: n={live.get('n')}, "
        f"win_rate={live.get('win_rate'):.2%}, "
        f"max_dd={live.get('max_drawdown_pct'):.2f}%\n"
        f"Backtest: win_rate="
        f"{bt.get('win_rate'):.2%} (delta {verdict['eligibility']['win_rate_delta']:+.4f}), "
        f"max_dd={bt.get('max_drawdown_pct')}\n\n"
        f"Reply with \"promote {sid}\" to flip the tier, or "
        f"\"hold {sid}\" to defer."
    )


def alert_eligible_promotions(
    conn: sqlite3.Connection,
    *,
    send_fn=None,
    now_iso: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Walk every strategy; for each that's promotion-eligible AND hasn't
    yet had an alert sent for this (current_tier, candidate_tier) pair,
    send a Telegram alert and record it.

    ``send_fn`` is the function that delivers the alert (defaults to
    ``telegram_alerter.send_message``). Tests inject a stub.

    Returns the list of verdicts for strategies that got alerts (newly
    alerted only — strategies whose alert was already in the log are
    skipped).
    """
    if send_fn is None:
        from monitoring.telegram_alerter import send_message as send_fn
    alerted: List[Dict[str, Any]] = []
    for verdict in evaluate_all(conn):
        elig = verdict.get("eligibility") or {}
        if not elig.get("eligible"):
            continue
        sid = verdict["strategy_id"]
        cur = verdict["current_tier"]
        cand = verdict["candidate_tier"]
        if cand is None:
            continue
        if alert_already_sent(
            conn,
            strategy_id=sid, current_tier=cur, candidate_tier=cand,
        ):
            continue
        msg = _format_alert(verdict)
        sent = send_fn(msg)
        if sent:
            record_alert_sent(
                conn,
                strategy_id=sid, current_tier=cur,
                candidate_tier=cand, now_iso=now_iso,
            )
            alerted.append(verdict)
    return alerted


def confirm_promotion(
    conn: sqlite3.Connection, strategy_id: str,
) -> Optional[str]:
    """Ross's manual confirmation step. Promotes the strategy one rung
    up the ladder. Returns the new tier, or None if no promotion path
    exists from the current tier."""
    current = get_current_tier(conn, strategy_id)
    candidate = PROMOTION_LADDER.get(current)
    if candidate is None:
        return None
    set_tier(conn, strategy_id, candidate)
    return candidate


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from data import db
    conn = db.init_db()
    try:
        verdicts = evaluate_all(conn)
        eligible = [v for v in verdicts if v["eligibility"]["eligible"]]
        print(f"Strategies evaluated: {len(verdicts)}")
        print(f"Promotion-eligible:   {len(eligible)}")
        for v in eligible:
            print(f"  {v['strategy_id']}: {v['current_tier']} → "
                  f"{v['candidate_tier']}  ({v['eligibility']['reason']})")
    finally:
        conn.close()
