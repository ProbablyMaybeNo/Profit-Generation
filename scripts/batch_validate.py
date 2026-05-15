"""
batch_validate.py — Walk records.jsonl, codegen + validate every UNTESTED
strategy in one pass. Prefetches the test universe's bars ONCE so yfinance
only gets hit per (symbol, lookback) instead of per (strategy, symbol).

Workflow per record:
  1. Skip if current_verdict is anything other than UNTESTED  (unless --force)
  2. If extra.compute_fn is unset OR the generated file doesn't exist:
       run llm_codegen  → write file → update extra.compute_fn
       on failure: mark verdict=FAIL with verdict_summary noting codegen failed
  3. Run validate_strategy_record(strategy_id, universe, lookback_days)
       update extra.test_runs / current_verdict / verdict_summary
  4. After the whole batch: save records.jsonl + reseed trading.db

CLI:
  py -3.13 scripts/batch_validate.py
  py -3.13 scripts/batch_validate.py --max 5 --universe GDX,KRE,XHB,XME
  py -3.13 scripts/batch_validate.py --force --strategy-id rsi2-oversold
  py -3.13 scripts/batch_validate.py --skip-codegen --since 2026-05-15
"""

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402

# Reuse helpers from the single-strategy CLIs.
from scripts.codegen_strategy import (  # noqa: E402
    _load_records as cs_load_records,
    _save_records as cs_save_records,
    _find_record as cs_find_record,
    _safe_filename,
    GENERATED_DIR,
    codegen_record,
    _persist_codegen,
)
from scripts import validate_strategy as vs  # noqa: E402

DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "GDX", "KRE", "XHB", "XME", "XLE", "XOP", "XBI"]


def _is_untested(record: Dict) -> bool:
    extra = record.get("extra") or {}
    return (extra.get("current_verdict") or "UNTESTED") == "UNTESTED"


def _has_generated_file(strategy_id: str) -> bool:
    return (GENERATED_DIR / f"{_safe_filename(strategy_id)}.py").exists()


def _record_added_after(record: Dict, since: Optional[date]) -> bool:
    if since is None:
        return True
    extra = record.get("extra") or {}
    iso = extra.get("first_logged_iso") or extra.get("last_updated_iso")
    if not iso:
        return True
    try:
        return date.fromisoformat(iso[:10]) >= since
    except Exception:
        return True


def _mark_codegen_failed(record: Dict, error: str) -> None:
    extra = record.get("extra", {}) or {}
    extra["tested"] = False
    extra["current_verdict"] = "FAIL"
    extra["verdict_summary"] = f"codegen failed {date.today().isoformat()}: {error[:240]}"
    extra["last_updated_iso"] = date.today().isoformat()
    record["extra"] = extra


def batch_run(
    *,
    universe: List[str],
    lookback_days: int = 730,
    max_n: Optional[int] = None,
    since: Optional[date] = None,
    force: bool = False,
    skip_codegen: bool = False,
    strategy_id_filter: Optional[str] = None,
    model: Optional[str] = None,
    bars_loader=None,
) -> Dict:
    """
    Returns a summary dict: {targets, by_verdict, codegen_failures, errors,
    per_strategy: [{strategy_id, action, verdict, error}, ...]}

    `bars_loader` defaults to backtest.data.load_bars; tests inject their own.
    """
    records = cs_load_records()
    targets = []
    for r in records:
        sid = (r.get("extra") or {}).get("strategy_id")
        if not sid:
            continue
        if strategy_id_filter and sid != strategy_id_filter:
            continue
        if not force and not _is_untested(r):
            continue
        if not _record_added_after(r, since):
            continue
        targets.append(r)
    if max_n is not None:
        targets = targets[:max_n]

    if not targets:
        return {"targets": 0, "per_strategy": [], "by_verdict": {},
                "codegen_failures": 0, "errors": 0,
                "note": "no UNTESTED records matched filters"}

    # Pre-fetch bars once for the entire universe.
    if bars_loader is None:
        from backtest.data import load_bars
        bars_loader = load_bars
    end = date.today()
    start = end - timedelta(days=lookback_days)
    print(f"prefetching {len(universe)} symbols × {lookback_days}d daily bars...")
    bars_by_sym = bars_loader(
        universe, start=start.isoformat(), end=end.isoformat(),
        interval="1d", source="yf",
    )
    fetched = sorted(bars_by_sym.keys())
    missing = sorted(set(universe) - set(fetched))
    print(f"  bars: fetched={len(fetched)}  missing={missing or 'none'}")

    per_strategy: List[Dict] = []
    by_verdict: Dict[str, int] = {}
    codegen_failures = 0
    errors = 0

    for r in targets:
        extra = r.get("extra", {}) or {}
        sid = extra["strategy_id"]
        outcome: Dict = {"strategy_id": sid}

        # ----- codegen (if needed) -----
        needs_codegen = not skip_codegen and (
            not extra.get("compute_fn") or not _has_generated_file(sid)
        )
        if needs_codegen:
            print(f"  [{sid}] codegen...")
            cg = codegen_record(r, model=model)
            if not cg["ok"]:
                _mark_codegen_failed(r, cg.get("error") or "unknown")
                outcome.update({"action": "codegen_failed",
                                "error": cg.get("error")})
                per_strategy.append(outcome)
                codegen_failures += 1
                by_verdict["FAIL"] = by_verdict.get("FAIL", 0) + 1
                continue
            _persist_codegen(r, cg, records, is_new=False)

        if skip_codegen and not _has_generated_file(sid):
            outcome.update({"action": "skipped", "error": "no generated file and --skip-codegen"})
            per_strategy.append(outcome)
            errors += 1
            continue

        # ----- validate -----
        try:
            print(f"  [{sid}] validate on {len(universe)} symbols × {lookback_days}d...")
            result = vs.validate_strategy_record(
                sid, universe, lookback_days=lookback_days,
                bars_by_sym=bars_by_sym,
            )
        except Exception as e:
            traceback.print_exc()
            outcome.update({"action": "validate_error", "error": str(e)[:200]})
            per_strategy.append(outcome)
            errors += 1
            continue

        vs.apply_verdict_to_record(r, result, ",".join(universe))
        verdict = result["overall_verdict"]
        outcome.update({"action": "validated", "verdict": verdict,
                        "per_symbol": {s: result["per_symbol"][s]["verdict"]
                                       for s in result["per_symbol"]}})
        per_strategy.append(outcome)
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1

    cs_save_records(records)

    # Reseed trading.db with the updated records.
    conn = db.init_db()
    try:
        for r in records:
            sid = (r.get("extra") or {}).get("strategy_id")
            if sid:
                db.upsert_strategy(conn, r)
    finally:
        conn.close()

    return {
        "targets": len(targets),
        "per_strategy": per_strategy,
        "by_verdict": by_verdict,
        "codegen_failures": codegen_failures,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default=",".join(DEFAULT_UNIVERSE),
                        help=f"comma-separated symbols (default: {','.join(DEFAULT_UNIVERSE)})")
    parser.add_argument("--lookback-days", type=int, default=730)
    parser.add_argument("--max", type=int, default=None,
                        help="limit number of strategies to process")
    parser.add_argument("--since", type=str, default=None,
                        help="only process records first_logged on/after this ISO date")
    parser.add_argument("--force", action="store_true",
                        help="re-validate strategies even if they already have a verdict")
    parser.add_argument("--skip-codegen", action="store_true",
                        help="don't regenerate functions; require existing generated file")
    parser.add_argument("--strategy-id", default=None,
                        help="restrict to a single strategy_id (overrides --max/--since)")
    parser.add_argument("--model", default=None, help="override OLLAMA_MODEL")
    args = parser.parse_args()

    universe = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    since = date.fromisoformat(args.since) if args.since else None

    summary = batch_run(
        universe=universe,
        lookback_days=args.lookback_days,
        max_n=args.max,
        since=since,
        force=args.force,
        skip_codegen=args.skip_codegen,
        strategy_id_filter=args.strategy_id,
        model=args.model,
    )

    print()
    print(f"=== BATCH SUMMARY ===")
    print(f"targets processed:  {summary['targets']}")
    print(f"codegen failures:   {summary['codegen_failures']}")
    print(f"validation errors:  {summary['errors']}")
    print(f"by verdict:         {dict(sorted(summary['by_verdict'].items()))}")
    print()
    print(f"{'strategy':<40} {'action':<18} {'verdict':<18}")
    for o in summary["per_strategy"]:
        print(f"  {o['strategy_id']:<38} {o.get('action','?'):<18} "
              f"{o.get('verdict') or o.get('error','')[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
