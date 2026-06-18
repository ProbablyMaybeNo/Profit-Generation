"""reintroduction.py — Stage 3 / M12: the strategy reintroduction framework.

The gate that decides whether a currently-paused strategy may be safely
re-admitted to the live(-on-paper) book. This is the precondition for unpausing
any of the strategies the Donchian-only reset shelved — it exists so re-adding a
strategy can never repeat the multi-owner oversell loop that cost ~$101k.

DESIGN — this module DECIDES, it does NOT act. `evaluate_candidate` is a pure,
queryable decision function: it reads outcomes/equity/meta and returns a verdict.
It never writes `paused_strategies`, never unpauses, never places an order. The
operator (Ross) reads the verdict and makes the unpause call by hand.

A candidate is ADMITTED only when ALL of these hold:

  1. EVIDENCE — >= MIN_FRESH_CLOSES fresh, honest closed outcomes (status='closed'
     with a present return, EXCLUDING phantom_no_fill / stale_intraday_flatten_missed
     and every other cleanup/reconcile close), AND positive expectancy. Expectancy
     is measured in R-multiples when an R-bearing sample of sufficient size exists
     (the honest, dollar-risk-normalized unit, Stage 1.6), and falls back to mean
     per-trade return% otherwise.

  2. LOW CORRELATION — the candidate's per-trade return series has Pearson
     correlation < MAX_BOOK_CORRELATION with the existing live book, measured on a
     common daily grid (each side's trades aggregated to a per-day mean return, then
     aligned on overlapping days). A re-add must DIVERSIFY, not double existing risk.
     If there are fewer than MIN_CORR_OVERLAP overlapping days the correlation is
     UNKNOWN and the candidate is treated as INELIGIBLE — fail closed.

  3. ONE-AT-A-TIME — the framework refuses to green-light a candidate while another
     strategy is inside its probation/grace window. An admission is recorded in
     `meta` (by the operator, via `record_admission`) with a window; while any such
     window is open and unexpired, every other candidate is gated out.

All evidence is read off the now-solid substrates: phantom-clean fresh outcomes
(strategy_health.CLEANUP_EXIT_REASONS), interval-scoped returns, R-multiple
expectancy, single-symbol-owner authority (position_manager.symbol_owner), and the
equity curve as the authoritative book P&L source.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402
from monitoring.strategy_health import (  # noqa: E402
    _fresh_only_clause,
    _interval_class_clause,
    is_paused,
)

# --- gate thresholds -------------------------------------------------------

MIN_FRESH_CLOSES = 20          # >= 20 fresh, honest closed outcomes
MIN_R_SAMPLE = 10              # use R-expectancy only with this many R-bearing rows
MAX_BOOK_CORRELATION = 0.3     # candidate vs book drawdown/return correlation
MIN_CORR_OVERLAP = 5           # overlapping days below which correlation is UNKNOWN
DEFAULT_GRACE_DAYS = 30        # one-at-a-time probation window length

ADMISSION_META_PREFIX = "reintroduction.admitted:"


# --- fresh, honest return / R series --------------------------------------


def _fresh_closed_rows(conn, strategy_id: str, bar_interval: str) -> List[Dict]:
    """Fresh closed outcomes for a strategy, scoped to its interval class.

    Returns rows with return_pct, r_multiple, and the exit DATE (for the daily
    correlation grid). Excludes phantom/stale/reconcile closes via the shared
    fresh-only clause — the same honest substrate every gate reads.
    """
    clause = _interval_class_clause(bar_interval)
    rows = conn.execute(
        "SELECT o.return_pct AS return_pct, o.r_multiple AS r_multiple, "
        "       substr(o.exit_ts, 1, 10) AS exit_day "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        f"   {_fresh_only_clause()} "
        f"   AND {clause} AND s.strategy_id=? "
        " ORDER BY o.exit_ts ASC, o.signal_id ASC",
        (strategy_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _book_strategy_ids(conn, exclude: str) -> List[str]:
    """Strategy ids currently in the live book (holding an open broker position),
    excluding the candidate itself. The book is what a re-add must not correlate
    with. Derived from single-owner authority over currently-held symbols."""
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM paper_trades "
        " WHERE side='buy' AND status IN ('filled','partially_filled')"
    ).fetchall()
    owners: List[str] = []
    for r in rows:
        sym = r["symbol"]
        if not sym:
            continue
        owner = pm.symbol_owner(conn, sym)
        if owner and owner != exclude and owner not in owners:
            owners.append(owner)
    return owners


def _book_daily_returns(conn, strategy_ids: List[str]) -> Dict[str, float]:
    """Per-day mean fresh return% across the whole book (union of strategy_ids).

    The book's return series for correlation is the daily mean over every fresh
    closed trade booked by any current book strategy. Interval class is not
    constrained here — the book's realized P&L is what the candidate must
    diversify against."""
    if not strategy_ids:
        return {}
    placeholders = ",".join("?" for _ in strategy_ids)
    rows = conn.execute(
        "SELECT substr(o.exit_ts, 1, 10) AS exit_day, o.return_pct AS return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        f"   {_fresh_only_clause()} "
        f"   AND s.strategy_id IN ({placeholders}) ",
        tuple(strategy_ids),
    ).fetchall()
    return _daily_mean([dict(r) for r in rows])


def _daily_mean(rows: List[Dict]) -> Dict[str, float]:
    """Aggregate [{exit_day, return_pct}] to {day: mean return%}."""
    buckets: Dict[str, List[float]] = {}
    for r in rows:
        day, ret = r.get("exit_day"), r.get("return_pct")
        if day is None or ret is None:
            continue
        buckets.setdefault(day, []).append(float(ret))
    return {d: sum(v) / len(v) for d, v in buckets.items() if v}


def _candidate_daily_returns(rows: List[Dict]) -> Dict[str, float]:
    """Per-day mean return% for the candidate's own fresh closed rows."""
    return _daily_mean(rows)


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson correlation; None when undefined (n<2 or a zero-variance side)."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denom = math.sqrt(sxx * syy)
    if denom == 0:
        return None
    return sxy / denom


# --- the three gates -------------------------------------------------------


def evaluate_evidence(
    conn, strategy_id: str, *,
    bar_interval: str = "1d",
    min_fresh: int = MIN_FRESH_CLOSES,
    min_r_sample: int = MIN_R_SAMPLE,
) -> Dict:
    """Gate 1 — fresh-evidence + positive-expectancy check. Pure.

    Returns {n_fresh, expectancy, expectancy_unit, passed, reason}. Prefers
    R-multiple expectancy when an R-bearing sample >= min_r_sample exists; else
    mean per-trade return%.
    """
    rows = _fresh_closed_rows(conn, strategy_id, bar_interval)
    n = len(rows)
    out = {
        "strategy_id": strategy_id,
        "n_fresh": n,
        "expectancy": None,
        "expectancy_unit": None,
        "passed": False,
        "reason": "",
    }
    if n < min_fresh:
        out["reason"] = (
            f"only {n} fresh closes (need >= {min_fresh}) — insufficient evidence"
        )
        return out
    r_vals = [float(r["r_multiple"]) for r in rows if r.get("r_multiple") is not None]
    if len(r_vals) >= min_r_sample:
        exp = sum(r_vals) / len(r_vals)
        unit = "R"
    else:
        rets = [float(r["return_pct"]) for r in rows]
        exp = sum(rets) / len(rets)
        unit = "return_pct"
    out["expectancy"] = round(exp, 4)
    out["expectancy_unit"] = unit
    if exp > 0:
        out["passed"] = True
        out["reason"] = (
            f"{n} fresh closes, expectancy {exp:+.4f} {unit} > 0"
        )
    else:
        out["reason"] = (
            f"{n} fresh closes but expectancy {exp:+.4f} {unit} <= 0 "
            f"— no edge to re-admit"
        )
    return out


def evaluate_correlation(
    conn, strategy_id: str, *,
    bar_interval: str = "1d",
    max_corr: float = MAX_BOOK_CORRELATION,
    min_overlap: int = MIN_CORR_OVERLAP,
    book_strategy_ids: Optional[List[str]] = None,
) -> Dict:
    """Gate 2 — drawdown/return correlation with the existing book. Pure.

    Correlation method: each side's fresh closed trades are aggregated to a
    per-day mean return on a common calendar grid, then aligned on overlapping
    days and Pearson-correlated. Fewer than `min_overlap` overlapping days =>
    correlation UNKNOWN => INELIGIBLE (fail closed). An empty book (nothing else
    holding) is treated as zero correlation — a first/only strategy diversifies
    against nothing and passes.

    Returns {correlation, overlap, book, passed, reason}.
    """
    out = {
        "strategy_id": strategy_id,
        "correlation": None,
        "overlap": 0,
        "book": [],
        "passed": False,
        "reason": "",
    }
    book_ids = (book_strategy_ids if book_strategy_ids is not None
                else _book_strategy_ids(conn, exclude=strategy_id))
    book_ids = [b for b in book_ids if b != strategy_id]
    out["book"] = list(book_ids)
    if not book_ids:
        out["passed"] = True
        out["correlation"] = 0.0
        out["reason"] = "no existing book to correlate against — diversifies trivially"
        return out

    cand_days = _candidate_daily_returns(_fresh_closed_rows(conn, strategy_id, bar_interval))
    book_days = _book_daily_returns(conn, book_ids)
    common = sorted(set(cand_days) & set(book_days))
    out["overlap"] = len(common)
    if len(common) < min_overlap:
        out["reason"] = (
            f"only {len(common)} overlapping days with the book "
            f"(need >= {min_overlap}) — correlation UNKNOWN, fail closed"
        )
        return out
    cand_series = [cand_days[d] for d in common]
    book_series = [book_days[d] for d in common]
    corr = _pearson(cand_series, book_series)
    if corr is None:
        out["reason"] = (
            "correlation undefined (zero-variance series) — fail closed"
        )
        return out
    out["correlation"] = round(corr, 4)
    if corr < max_corr:
        out["passed"] = True
        out["reason"] = (
            f"correlation {corr:+.3f} < {max_corr} over {len(common)} days "
            f"— diversifies the book"
        )
    else:
        out["reason"] = (
            f"correlation {corr:+.3f} >= {max_corr} over {len(common)} days "
            f"— would double existing book risk"
        )
    return out


# --- one-at-a-time admission window ---------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def active_admissions(conn, *, asof_iso: Optional[str] = None) -> List[Dict]:
    """Strategies currently inside an open (unexpired) probation/grace window.

    An admission is a `meta` row keyed `reintroduction.admitted:<sid>` whose
    JSON value carries {admitted_at, expires_at}. While any such window is open,
    one-at-a-time refuses a second admission.
    """
    asof = asof_iso or _utc_now_iso()
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ?",
        (ADMISSION_META_PREFIX + "%",),
    ).fetchall()
    out: List[Dict] = []
    for r in rows:
        sid = r["key"][len(ADMISSION_META_PREFIX):]
        try:
            rec = json.loads(r["value"]) if r["value"] else {}
        except Exception:
            rec = {}
        expires = rec.get("expires_at")
        if expires is not None and expires <= asof:
            continue  # window elapsed — no longer occupies the one-at-a-time slot
        out.append({"strategy_id": sid, **rec})
    return out


def evaluate_one_at_a_time(
    conn, strategy_id: str, *, asof_iso: Optional[str] = None,
) -> Dict:
    """Gate 3 — refuse a second admission while another window is open. Pure.

    A strategy that is ITSELF the one in-window is allowed (re-running the gate
    on the in-flight strategy must not block it).
    """
    active = active_admissions(conn, asof_iso=asof_iso)
    others = [a for a in active if a["strategy_id"] != strategy_id]
    out = {
        "strategy_id": strategy_id,
        "active_admissions": [a["strategy_id"] for a in active],
        "passed": not others,
        "reason": "",
    }
    if others:
        names = ", ".join(a["strategy_id"] for a in others)
        out["reason"] = (
            f"another strategy is in its probation window ({names}) "
            f"— one strategy at a time"
        )
    else:
        out["reason"] = "no other strategy in a probation window — slot is free"
    return out


# --- top-level decision ----------------------------------------------------


def evaluate_candidate(
    conn, strategy_id: str, *,
    bar_interval: str = "1d",
    min_fresh: int = MIN_FRESH_CLOSES,
    min_r_sample: int = MIN_R_SAMPLE,
    max_corr: float = MAX_BOOK_CORRELATION,
    min_overlap: int = MIN_CORR_OVERLAP,
    asof_iso: Optional[str] = None,
    require_paused: bool = True,
) -> Dict:
    """The M12 verdict for one candidate strategy. Pure — decides, never acts.

    Returns:
      {strategy_id, admit, evidence, correlation, one_at_a_time, reason}

    `admit=True` only when the candidate is currently paused (unless
    require_paused=False) AND all three gates pass. This function NEVER unpauses
    or mutates the paused_strategies table — the operator reads `admit` and makes
    the call.
    """
    out = {
        "strategy_id": strategy_id,
        "admit": False,
        "evidence": None,
        "correlation": None,
        "one_at_a_time": None,
        "reason": "",
    }

    if require_paused and not is_paused(conn, strategy_id, asof_iso=asof_iso):
        out["reason"] = (
            f"{strategy_id} is not currently paused — reintroduction only "
            f"applies to paused strategies"
        )
        return out

    evidence = evaluate_evidence(
        conn, strategy_id, bar_interval=bar_interval,
        min_fresh=min_fresh, min_r_sample=min_r_sample,
    )
    out["evidence"] = evidence

    correlation = evaluate_correlation(
        conn, strategy_id, bar_interval=bar_interval,
        max_corr=max_corr, min_overlap=min_overlap,
    )
    out["correlation"] = correlation

    one = evaluate_one_at_a_time(conn, strategy_id, asof_iso=asof_iso)
    out["one_at_a_time"] = one

    if not evidence["passed"]:
        out["reason"] = f"REFUSED (evidence): {evidence['reason']}"
        return out
    if not correlation["passed"]:
        out["reason"] = f"REFUSED (correlation): {correlation['reason']}"
        return out
    if not one["passed"]:
        out["reason"] = f"REFUSED (one-at-a-time): {one['reason']}"
        return out

    out["admit"] = True
    out["reason"] = (
        f"ADMIT: {evidence['reason']}; {correlation['reason']}; {one['reason']}"
    )
    return out


# --- admission bookkeeping (operator-driven; does NOT unpause) -------------


def record_admission(
    conn, strategy_id: str, *,
    grace_days: int = DEFAULT_GRACE_DAYS,
    now_iso: Optional[str] = None,
) -> Dict:
    """Open a probation/grace window for an admitted strategy in `meta`.

    This is the operator's bookkeeping step AFTER they decide to unpause — it
    occupies the one-at-a-time slot so no second strategy is green-lit during the
    window. It does NOT unpause the strategy or touch paused_strategies; the
    actual unpause is a separate, deliberate operator action.
    """
    admitted_at = now_iso or _utc_now_iso()
    expires_at: Optional[str] = None
    if grace_days and grace_days > 0:
        base = datetime.fromisoformat(admitted_at.replace("Z", "+00:00"))
        expires_at = (base + timedelta(days=int(grace_days))).isoformat(
            timespec="seconds")
    rec = {"admitted_at": admitted_at, "expires_at": expires_at}
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (ADMISSION_META_PREFIX + strategy_id, json.dumps(rec)),
        )
    return {"strategy_id": strategy_id, **rec}


def clear_admission(conn, strategy_id: str) -> bool:
    """Close a strategy's probation window (frees the one-at-a-time slot)."""
    with conn:
        cur = conn.execute(
            "DELETE FROM meta WHERE key=?",
            (ADMISSION_META_PREFIX + strategy_id,),
        )
    return cur.rowcount > 0


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="M12 strategy-reintroduction gate (read-only decision).")
    parser.add_argument("strategy_id", nargs="?",
                        help="evaluate this paused strategy")
    parser.add_argument("--interval", default="1d",
                        help="interval class to judge (1d or intraday)")
    parser.add_argument("--no-require-paused", action="store_true",
                        help="evaluate even if the strategy isn't paused")
    parser.add_argument("--list-windows", action="store_true",
                        help="print open probation windows and exit")
    args = parser.parse_args()

    conn = db.init_db()
    try:
        if args.list_windows:
            active = active_admissions(conn)
            if not active:
                log("no open probation windows", "INFO")
            for a in active:
                log(f"  [WINDOW] {a['strategy_id']}: expires_at={a.get('expires_at')}",
                    "INFO")
            return
        if not args.strategy_id:
            parser.error("strategy_id is required unless --list-windows")
        verdict = evaluate_candidate(
            conn, args.strategy_id, bar_interval=args.interval,
            require_paused=not args.no_require_paused,
        )
        tag = "ADMIT" if verdict["admit"] else "REFUSED"
        log(f"[{tag}] {args.strategy_id}: {verdict['reason']}",
            "SUCCESS" if verdict["admit"] else "WARNING")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
