"""
edge_diff.py — Realized-vs-theoretical edge per strategy.

For each strategy with closed paper_trades, compare the backtest-expected
return per signal (from records.jsonl/strategies.raw_record_json
test_runs) against the actual paper-trade fill returns
(sell.fill_price vs buy.fill_price). The gap surfaces how much edge is
being eaten by slippage / market microstructure between backtest
ideal-close fills and live paper fills.

Theoretical edge per strategy:
  weighted mean across all instrument-level test_runs we trust, using
  mean_ret_pct when present, else total_return_pct / trades as a
  fallback. Weighting is by `trades` so heavy-sample symbols dominate.
  Only runs with at least 10 trades are counted (matches the validator's
  UNTESTED floor).

Realized edge per strategy:
  pair each filled BUY in paper_trades with the next filled SELL on the
  same (strategy_id, symbol) by submitted_at order. For each pair:
    realized_pct = (sell.fill_price - buy.fill_price) / buy.fill_price * 100
  Mean across pairs is the realized edge.

Slippage gap = theoretical - realized (positive = edge eaten).
Capture ratio = realized / theoretical * 100 (only when theoretical > 0).

Empty / degenerate cases:
  - strategy has no paper_trades                → status "no_paper_trades"
  - strategy has paper_trades but no closed BUY→SELL pair → "no_closed_pairs"
  - strategy has no theoretical test_runs       → "no_backtest_baseline"
  - all three present                            → "ok"
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple


MIN_TRADES_FOR_BASELINE = 10


def fetch_paper_pairs(conn: sqlite3.Connection) -> Dict[str, List[Dict]]:
    """Return {strategy_id: [{symbol, buy_fill, sell_fill, return_pct, buy_at, sell_at}]}.

    Walks paper_trades ordered by submitted_at. For each (strategy_id, symbol)
    pair, the first filled buy is opened; the next filled sell closes it and
    a pair is emitted. Surplus signals (e.g. an extra sell with nothing open)
    are ignored, mirroring _pair_signals in validate_strategy.
    """
    rows = conn.execute(
        "SELECT strategy_id, symbol, side, fill_price, submitted_at, filled_at, status "
        "  FROM paper_trades "
        " WHERE fill_price IS NOT NULL AND filled_at IS NOT NULL "
        " ORDER BY COALESCE(filled_at, submitted_at) ASC, id ASC"
    ).fetchall()
    open_buys: Dict[Tuple[str, str], Dict] = {}
    out: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        sid = r["strategy_id"] or ""
        sym = r["symbol"] or ""
        side = (r["side"] or "").lower()
        price = r["fill_price"]
        if not sid or not sym or price in (None, 0):
            continue
        key = (sid, sym)
        if side == "buy":
            # First filled buy on this (sid, sym) opens; ignore stacking buys.
            if key not in open_buys:
                open_buys[key] = {
                    "buy_fill": float(price),
                    "buy_at": r["filled_at"] or r["submitted_at"],
                }
        elif side == "sell":
            held = open_buys.pop(key, None)
            if held is None:
                continue
            buy_fill = held["buy_fill"]
            sell_fill = float(price)
            ret = (sell_fill - buy_fill) / buy_fill * 100.0
            out[sid].append({
                "symbol": sym,
                "buy_fill": buy_fill,
                "sell_fill": sell_fill,
                "return_pct": ret,
                "buy_at": held["buy_at"],
                "sell_at": r["filled_at"] or r["submitted_at"],
            })
    return dict(out)


def _per_signal_return(run: Dict) -> Optional[float]:
    """Best-effort mean-return-per-signal for one test_runs entry.

    Prefers mean_ret_pct; falls back to total_return_pct / trades.
    Returns None if neither yields a finite value.
    """
    if run.get("trades") in (None, 0):
        return None
    n = int(run["trades"])
    if n < MIN_TRADES_FOR_BASELINE:
        return None
    mean = run.get("mean_ret_pct")
    if mean is not None:
        try:
            return float(mean)
        except (TypeError, ValueError):
            pass
    tot = run.get("total_return_pct")
    if tot is None:
        return None
    try:
        return float(tot) / n
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def theoretical_edge_from_record(record: Dict) -> Dict:
    """Compute the weighted-mean per-signal backtest edge for one strategy
    record.

    Returns:
      {
        "per_signal_pct": float | None,
        "n_runs_used": int,
        "n_trades_total": int,
        "by_instrument": [{instrument, per_signal_pct, trades, verdict}],
      }
    """
    extra = (record.get("extra") or {})
    runs = extra.get("test_runs") or []
    # Skip overlay / scenario runs — they're not single-strategy signals.
    valid = [r for r in runs if not r.get("scenario")
             and (r.get("verdict") or "").upper() != "INFO"]
    by_inst: List[Dict] = []
    total_trades = 0
    weighted_sum = 0.0
    used = 0
    for r in valid:
        per_sig = _per_signal_return(r)
        if per_sig is None:
            continue
        trades = int(r["trades"])
        by_inst.append({
            "instrument": r.get("instrument"),
            "per_signal_pct": round(per_sig, 4),
            "trades": trades,
            "verdict": r.get("verdict"),
        })
        weighted_sum += per_sig * trades
        total_trades += trades
        used += 1
    if total_trades == 0:
        return {
            "per_signal_pct": None,
            "n_runs_used": 0,
            "n_trades_total": 0,
            "by_instrument": [],
        }
    return {
        "per_signal_pct": round(weighted_sum / total_trades, 4),
        "n_runs_used": used,
        "n_trades_total": total_trades,
        "by_instrument": by_inst,
    }


def fetch_records_by_strategy(conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Return {strategy_id: raw_record_dict} for every strategy in the db."""
    out: Dict[str, Dict] = {}
    rows = conn.execute(
        "SELECT strategy_id, raw_record_json FROM strategies "
        " WHERE raw_record_json IS NOT NULL"
    ).fetchall()
    for r in rows:
        try:
            out[r["strategy_id"]] = json.loads(r["raw_record_json"])
        except (TypeError, ValueError):
            continue
    return out


def realized_stats(pairs: Iterable[Dict]) -> Dict:
    """Mean / median / count / win-rate for a list of paired paper trades."""
    rets = [p["return_pct"] for p in pairs]
    n = len(rets)
    if n == 0:
        return {"n": 0, "mean_pct": 0.0, "win_rate": 0.0,
                "best_pct": 0.0, "worst_pct": 0.0}
    mean = sum(rets) / n
    wr = sum(1 for r in rets if r > 0) / n
    return {
        "n": n,
        "mean_pct": round(mean, 4),
        "win_rate": round(wr, 4),
        "best_pct": round(max(rets), 4),
        "worst_pct": round(min(rets), 4),
    }


def diff_row(strategy_id: str, theoretical: Dict, pairs: List[Dict]) -> Dict:
    """Assemble one per-strategy row of the edge_diff report."""
    realized = realized_stats(pairs)
    theo_pct = theoretical.get("per_signal_pct")
    real_pct = realized["mean_pct"] if realized["n"] else None

    status: str
    if realized["n"] == 0:
        status = "no_paper_trades" if not pairs else "no_closed_pairs"
    elif theo_pct is None:
        status = "no_backtest_baseline"
    else:
        status = "ok"

    if theo_pct is not None and real_pct is not None:
        slippage_pct = round(theo_pct - real_pct, 4)
        if theo_pct > 0:
            capture_ratio = round((real_pct / theo_pct) * 100.0, 2)
            edge_eaten_pct = round((1 - real_pct / theo_pct) * 100.0, 2)
        else:
            capture_ratio = None
            edge_eaten_pct = None
    else:
        slippage_pct = None
        capture_ratio = None
        edge_eaten_pct = None

    if status == "ok" and theo_pct and real_pct is not None:
        diff = theo_pct - real_pct
        if diff > 0 and theo_pct > 0:
            narrative = (
                f"backtest says {theo_pct:+.2f}% but paper fills are giving us "
                f"{real_pct:+.2f}% — slippage is eating "
                f"{abs(edge_eaten_pct):.0f}% of edge"
            )
        elif diff < 0:
            narrative = (
                f"paper fills ({real_pct:+.2f}%) are beating the backtest "
                f"({theo_pct:+.2f}%) by {abs(diff):.2f}%"
            )
        else:
            narrative = (
                f"paper fills ({real_pct:+.2f}%) are tracking backtest "
                f"({theo_pct:+.2f}%) closely"
            )
    else:
        narrative = ""

    return {
        "strategy_id": strategy_id,
        "status": status,
        "theoretical_per_signal_pct": theo_pct,
        "theoretical_runs_used": theoretical.get("n_runs_used", 0),
        "theoretical_trades_total": theoretical.get("n_trades_total", 0),
        "realized": realized,
        "slippage_pct": slippage_pct,
        "capture_ratio_pct": capture_ratio,
        "edge_eaten_pct": edge_eaten_pct,
        "narrative": narrative,
        "by_instrument": theoretical.get("by_instrument", []),
    }


def compute_slippage_burn(conn: sqlite3.Connection) -> Dict:
    """Compact ranked-by-burn view of the edge_diff rollup (milestone 3.6.1).

    Returns just the strategies with a usable theoretical baseline AND at
    least one closed paper-trade pair, sorted by slippage-burn % descending
    (most-burnt edge first). Each row carries the three numbers the spec
    asks for:

      expected_pct  = backtest mean return per signal
      actual_pct    = realized mean return per closed paper pair
      burn_pct      = (expected - actual) / |expected| * 100   when expected > 0
                       (positive = backtest beats live; negative = live beats)

    Shape::

      {
        "rows": [
          {"strategy_id": "...", "expected_pct": 0.97, "actual_pct": 0.42,
           "burn_pct": 56.7, "n_pairs": 18},
          ...
        ],
        "n_rows": int,
        "median_burn_pct": float | None,
        "worst": {"strategy_id": ..., "burn_pct": ...} | None,
      }

    Rows where `expected_pct <= 0` are excluded (burn ratio is undefined when
    backtest itself loses money; the underlying issue is strategy quality,
    not slippage). Rows with no closed pairs are excluded.
    """
    full = compute_edge_diff(conn)
    rows: List[Dict] = []
    for r in full.get("rows", []):
        if r.get("status") != "ok":
            continue
        expected = r.get("theoretical_per_signal_pct")
        realized_block = r.get("realized") or {}
        actual = realized_block.get("mean_pct")
        n_pairs = realized_block.get("n", 0)
        if expected is None or actual is None or n_pairs == 0:
            continue
        if expected <= 0:
            continue
        burn = (expected - actual) / abs(expected) * 100.0
        rows.append({
            "strategy_id": r["strategy_id"],
            "expected_pct": round(float(expected), 4),
            "actual_pct": round(float(actual), 4),
            "burn_pct": round(burn, 2),
            "n_pairs": int(n_pairs),
        })

    rows.sort(key=lambda x: (-x["burn_pct"], x["strategy_id"]))

    if rows:
        burns = sorted(r["burn_pct"] for r in rows)
        mid = len(burns) // 2
        if len(burns) % 2 == 1:
            median = burns[mid]
        else:
            median = (burns[mid - 1] + burns[mid]) / 2.0
        worst = {"strategy_id": rows[0]["strategy_id"],
                 "burn_pct": rows[0]["burn_pct"]}
    else:
        median = None
        worst = None

    return {
        "rows": rows,
        "n_rows": len(rows),
        "median_burn_pct": round(median, 2) if median is not None else None,
        "worst": worst,
    }


def compute_edge_diff(conn: sqlite3.Connection) -> Dict:
    """Produce the full edge-diff rollup served by the dashboard + CLI."""
    pairs_by_strat = fetch_paper_pairs(conn)
    records_by_strat = fetch_records_by_strategy(conn)

    # Scoped to strategies that have at least one closed paper-trade pair —
    # the spec is "for each strategy with paper_trades". Pure-baseline-no-paper
    # rows would just clone the validator's report.
    strategy_ids = sorted(pairs_by_strat.keys())

    rows: List[Dict] = []
    for sid in strategy_ids:
        record = records_by_strat.get(sid) or {}
        theoretical = theoretical_edge_from_record(record)
        pairs = pairs_by_strat.get(sid) or []
        rows.append(diff_row(sid, theoretical, pairs))

    # Sort: ok rows first (worst capture ratio first), then degenerate.
    def _sort_key(r):
        ok = 0 if r["status"] == "ok" else 1
        cap = r["capture_ratio_pct"]
        cap_key = cap if cap is not None else 1e9
        return (ok, cap_key, r["strategy_id"])
    rows.sort(key=_sort_key)

    return {
        "rows": rows,
        "n_rows": len(rows),
        "n_ok": sum(1 for r in rows if r["status"] == "ok"),
    }
