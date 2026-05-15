"""
walk_forward.py — Rolling-window stability analysis for a generated
strategy.

For each strategy + universe, splits the historical bars into N
overlapping windows (default: 6mo train, 3mo test, step 3mo). Runs
the compute_fn over each test window, computes the verdict per
window, then compares to the in-sample (full-period) verdict.

If ≥70% of the test windows produce a verdict that "matches" the
in-sample verdict, the strategy is marked
`extra.walk_forward_stable = True`. Otherwise False. Verdict matching
is forgiving: PASS / PASS_WITH_NUANCE / MARGINAL all count as
"positive edge"; FAIL / UNTESTED count as "no edge". A stable
positive-edge strategy is one whose test windows mostly land on
positive edge too.

Re-uses the validation primitives in scripts/validate_strategy.py so
the stats / verdict logic stays identical to the production
validator.

CLI:
  py -3.13 scripts/walk_forward.py --strategy-id rsi2-oversold \\
      --universe GDX,KRE,XHB --lookback-days 730
  py -3.13 scripts/walk_forward.py --strategy-id rsi2-oversold \\
      --universe GDX --train-days 180 --test-days 90 --step-days 90
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from scripts import validate_strategy as vs  # noqa: E402

RECORDS_PATH = vs.RECORDS_PATH

DEFAULT_TRAIN_DAYS = 180  # 6mo
DEFAULT_TEST_DAYS = 90    # 3mo
DEFAULT_STEP_DAYS = 90    # 3mo
DEFAULT_STABLE_RATIO = 0.70

POSITIVE_VERDICTS = {"PASS", "PASS_WITH_NUANCE", "MARGINAL"}
NEGATIVE_VERDICTS = {"FAIL", "UNTESTED"}


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------

def build_windows(
    start_idx: int, end_idx: int,
    *, train_days: int, test_days: int, step_days: int,
) -> List[Tuple[int, int, int, int]]:
    """Return list of (train_lo, train_hi, test_lo, test_hi) index tuples.

    Indices are positional offsets into the bars frame. Window math uses
    "trading-day" indices (i.e. row positions, not calendar dates) — that
    way irregular gaps (weekends, holidays) don't shorten a window.

    Each tuple satisfies:
      train_lo < train_hi <= test_lo < test_hi
      train_hi - train_lo == train_days   (clipped at the right edge)
      test_hi  - test_lo  == test_days    (clipped at the right edge)

    Walks forward by `step_days` until the test window would exceed
    end_idx.
    """
    if train_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError("train/test/step days must all be positive")
    out: List[Tuple[int, int, int, int]] = []
    cursor = start_idx
    while True:
        train_lo = cursor
        train_hi = train_lo + train_days
        test_lo = train_hi
        test_hi = test_lo + test_days
        if test_hi > end_idx:
            break
        out.append((train_lo, train_hi, test_lo, test_hi))
        cursor += step_days
    return out


# ---------------------------------------------------------------------------
# Verdict comparison
# ---------------------------------------------------------------------------

def verdict_class(verdict: str) -> str:
    """Coarse class — 'positive', 'negative', or 'unknown'."""
    v = (verdict or "").upper()
    if v in POSITIVE_VERDICTS:
        return "positive"
    if v in NEGATIVE_VERDICTS:
        return "negative"
    return "unknown"


def verdicts_match(in_sample: str, window: str) -> bool:
    """Two verdicts match iff they share the same class."""
    return verdict_class(in_sample) == verdict_class(window)


# ---------------------------------------------------------------------------
# Per-symbol walk-forward
# ---------------------------------------------------------------------------

def _stats_and_verdict(bars, compute_fn: Callable) -> Dict:
    """Run compute_fn on the bars slice; return stats + verdict.

    Mirrors validate_strategy.validate_strategy_record's inner loop but
    operates on already-sliced bars rather than fetching them.
    """
    if bars is None or bars.empty or len(bars) < 5:
        return {"verdict": "UNTESTED", "stats": vs._stats([]), "trades": 0,
                "note": "no/insufficient bars"}
    try:
        signals = compute_fn(bars)
    except Exception as e:
        return {"verdict": "FAIL", "stats": vs._stats([]), "trades": 0,
                "note": f"compute_fn raised: {e!s:.200}"}
    trades = vs._pair_signals(signals)
    rets = [t["return_pct"] for t in trades]
    stats = vs._stats(rets)
    return {"verdict": vs._verdict_for(stats), "stats": stats,
            "trades": stats["n"]}


def walk_forward_symbol(
    bars,
    compute_fn: Callable,
    *,
    train_days: int = DEFAULT_TRAIN_DAYS,
    test_days: int = DEFAULT_TEST_DAYS,
    step_days: int = DEFAULT_STEP_DAYS,
) -> Dict:
    """Return walk-forward summary for one symbol's bars.

    Shape:
      {
        "in_sample_verdict": str,
        "windows": [{train_period, test_period, in_sample_verdict,
                     test_verdict, test_stats, matches}],
        "n_windows": int,
        "n_matching": int,
        "match_ratio": float,
      }
    """
    if bars is None or bars.empty:
        return {"in_sample_verdict": "UNTESTED", "windows": [],
                "n_windows": 0, "n_matching": 0, "match_ratio": 0.0}

    n = len(bars)
    in_sample = _stats_and_verdict(bars, compute_fn)
    in_sample_verdict = in_sample["verdict"]

    win_idx = build_windows(
        0, n,
        train_days=train_days, test_days=test_days, step_days=step_days,
    )

    windows: List[Dict] = []
    matching = 0
    for (tl, th, sl, sh) in win_idx:
        train_slice = bars.iloc[tl:th]
        test_slice = bars.iloc[sl:sh]
        train_res = _stats_and_verdict(train_slice, compute_fn)
        test_res = _stats_and_verdict(test_slice, compute_fn)
        match = verdicts_match(in_sample_verdict, test_res["verdict"])
        if match:
            matching += 1
        windows.append({
            "train_idx": [tl, th],
            "test_idx": [sl, sh],
            "train_verdict": train_res["verdict"],
            "train_trades": train_res["trades"],
            "test_verdict": test_res["verdict"],
            "test_trades": test_res["trades"],
            "test_mean_ret": test_res["stats"]["mean"],
            "test_sharpe": test_res["stats"]["sharpe_ish"],
            "matches_in_sample": match,
        })

    n_win = len(windows)
    match_ratio = matching / n_win if n_win else 0.0
    return {
        "in_sample_verdict": in_sample_verdict,
        "in_sample_stats": in_sample["stats"],
        "windows": windows,
        "n_windows": n_win,
        "n_matching": matching,
        "match_ratio": round(match_ratio, 4),
    }


# ---------------------------------------------------------------------------
# Strategy-level orchestration
# ---------------------------------------------------------------------------

def walk_forward_strategy(
    strategy_id: str,
    universe: List[str],
    *,
    lookback_days: int = 730,
    train_days: int = DEFAULT_TRAIN_DAYS,
    test_days: int = DEFAULT_TEST_DAYS,
    step_days: int = DEFAULT_STEP_DAYS,
    stable_ratio: float = DEFAULT_STABLE_RATIO,
    fn: Optional[Callable] = None,
    bars_by_sym: Optional[Dict] = None,
) -> Dict:
    """Run walk-forward across every symbol in the universe.

    `fn` and `bars_by_sym` are injectable so batch / test callers can
    avoid disk + yfinance round-trips. Mirrors validate_strategy_record.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    today_iso = end.isoformat()

    if fn is None:
        fn = vs._load_compute_fn(strategy_id)
    if bars_by_sym is None:
        from backtest.data import load_bars  # lazy
        bars_by_sym = load_bars(
            universe, start=start.isoformat(), end=today_iso,
            interval="1d", source="yf",
        )

    per_symbol: Dict[str, Dict] = {}
    total_windows = 0
    total_matching = 0
    for sym in universe:
        bars = bars_by_sym.get(sym)
        res = walk_forward_symbol(
            bars, fn,
            train_days=train_days, test_days=test_days, step_days=step_days,
        )
        per_symbol[sym] = res
        total_windows += res["n_windows"]
        total_matching += res["n_matching"]

    overall_ratio = (total_matching / total_windows) if total_windows else 0.0
    stable = overall_ratio >= stable_ratio and total_windows > 0

    return {
        "strategy_id": strategy_id,
        "lookback_days": lookback_days,
        "train_days": train_days,
        "test_days": test_days,
        "step_days": step_days,
        "stable_ratio_required": stable_ratio,
        "universe": universe,
        "per_symbol": per_symbol,
        "total_windows": total_windows,
        "total_matching": total_matching,
        "overall_match_ratio": round(overall_ratio, 4),
        "walk_forward_stable": bool(stable),
        "evaluated_iso": today_iso,
    }


# ---------------------------------------------------------------------------
# records.jsonl write-back
# ---------------------------------------------------------------------------

def apply_walk_forward_to_record(record: Dict, result: Dict) -> None:
    """Mutate record extra with walk_forward_stable + summary."""
    extra = record.setdefault("extra", {})
    extra["walk_forward_stable"] = bool(result["walk_forward_stable"])
    extra["walk_forward_summary"] = {
        "evaluated_iso": result["evaluated_iso"],
        "train_days": result["train_days"],
        "test_days": result["test_days"],
        "step_days": result["step_days"],
        "total_windows": result["total_windows"],
        "total_matching": result["total_matching"],
        "overall_match_ratio": result["overall_match_ratio"],
        "stable_ratio_required": result["stable_ratio_required"],
        "universe": result["universe"],
    }
    extra["last_updated_iso"] = date.today().isoformat()


def _load_records(records_path: Path) -> List[Dict]:
    if not records_path.exists():
        return []
    with records_path.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _save_records(records_path: Path, records: List[Dict]) -> None:
    records_path.parent.mkdir(parents=True, exist_ok=True)
    with records_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _find_record(records: List[Dict], strategy_id: str) -> Optional[Dict]:
    for r in records:
        if (r.get("extra", {}) or {}).get("strategy_id") == strategy_id:
            return r
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--universe", required=True,
                        help="comma-separated symbols, e.g. GDX,KRE,XHB")
    parser.add_argument("--lookback-days", type=int, default=730)
    parser.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS)
    parser.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    parser.add_argument("--step-days", type=int, default=DEFAULT_STEP_DAYS)
    parser.add_argument("--stable-ratio", type=float, default=DEFAULT_STABLE_RATIO)
    parser.add_argument("--no-update-records", action="store_true",
                        help="do not write walk_forward_stable back to records.jsonl")
    parser.add_argument("--records-path", default=str(RECORDS_PATH))
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    if not symbols:
        print("no symbols specified")
        return 1

    log(
        f"walk_forward start: strategy={args.strategy_id} "
        f"universe={symbols} train={args.train_days}d test={args.test_days}d "
        f"step={args.step_days}d",
        "INFO",
    )

    result = walk_forward_strategy(
        strategy_id=args.strategy_id,
        universe=symbols,
        lookback_days=args.lookback_days,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        stable_ratio=args.stable_ratio,
    )

    log(
        f"done: windows={result['total_windows']} matching={result['total_matching']} "
        f"ratio={result['overall_match_ratio']} stable={result['walk_forward_stable']}",
        "SUCCESS" if result["walk_forward_stable"] else "WARNING",
    )

    if not args.no_update_records:
        records_path = Path(args.records_path)
        records = _load_records(records_path)
        rec = _find_record(records, args.strategy_id)
        if rec is None:
            log(f"no record for {args.strategy_id} in {records_path}", "WARNING")
        else:
            apply_walk_forward_to_record(rec, result)
            _save_records(records_path, records)
            log(f"updated record for {args.strategy_id}", "INFO")

    print(json.dumps({
        "strategy_id": result["strategy_id"],
        "walk_forward_stable": result["walk_forward_stable"],
        "overall_match_ratio": result["overall_match_ratio"],
        "total_windows": result["total_windows"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
