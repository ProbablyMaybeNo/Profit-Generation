"""
validate_strategy.py — Run a generated compute_fn over historical daily
bars per (symbol) and report whether it has edge.

Pairs signals in-memory (single open position per symbol; each long_entry
opens, each subsequent long_exit closes; surplus signals ignored). Does
NOT touch the live trading.db — the auto-trader stays clean.

Updates records.jsonl with:
  - extra.tested = True
  - extra.test_runs += [{date_iso, instrument, period, trades, win_rate_pct,
                          mean_ret_pct, sharpe, total_return_pct, verdict}]
  - extra.current_verdict
  - extra.verdict_summary
  - extra.last_updated_iso

Verdicts (per-symbol minimum eligibility):
  PASS              — n>=20, mean>0, sharpe>=0.20
  PASS_WITH_NUANCE  — passed on at least one symbol (vs all)
  MARGINAL          — n>=20 and break-even-ish (sharpe in [0, 0.20))
  FAIL              — anything worse, or insufficient n
  UNTESTED          — no symbol produced n>=10 trades

CLI:
  py -3.13 scripts/validate_strategy.py --strategy-id rsi2-oversold \\
      --universe GDX,KRE,XHB --lookback-days 730
"""

import argparse
import importlib.util
import json
import re
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402

RECORDS_PATH = (
    ROOT / "data" / "scrapes"
    / "tradingview-in-daytrading-strategies-2026-04-26" / "records.jsonl"
)
GENERATED_DIR = ROOT / "strategies" / "generated"


def _safe_filename(strategy_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", strategy_id).strip("_").lower() or "strategy"


def _load_records() -> list:
    if not RECORDS_PATH.exists():
        return []
    with RECORDS_PATH.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _save_records(records: list) -> None:
    with RECORDS_PATH.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _find_record(records: list, strategy_id: str) -> Optional[Dict]:
    for r in records:
        if (r.get("extra", {}) or {}).get("strategy_id") == strategy_id:
            return r
    return None


def _load_compute_fn(strategy_id: str) -> Callable:
    """Import strategies/generated/<id_safe>.py and return the compute_<name> attr."""
    safe = _safe_filename(strategy_id)
    path = GENERATED_DIR / f"{safe}.py"
    if not path.exists():
        raise FileNotFoundError(
            f"no generated function for {strategy_id} at {path}; "
            f"run scripts/codegen_strategy.py --strategy-id {strategy_id} first"
        )
    spec = importlib.util.spec_from_file_location(f"generated.{safe}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected_fn = "compute_" + safe.removeprefix("compute_")
    fn = getattr(mod, expected_fn, None)
    if fn is None:
        candidates = [n for n in dir(mod) if n.startswith("compute_")]
        if len(candidates) == 1:
            fn = getattr(mod, candidates[0])
        else:
            raise AttributeError(
                f"could not find compute_* function in {path} (candidates: {candidates})"
            )
    return fn


def _pair_signals(signals_df) -> List[Dict]:
    """Walk a signals frame; pair each entry with the next exit; return closed trades."""
    trades: List[Dict] = []
    open_entry = None
    for ts, row in signals_df.iterrows():
        if open_entry is None:
            if bool(row.get("long_entry", False)):
                open_entry = (ts, float(row["close"]))
        else:
            if bool(row.get("long_exit", False)):
                entry_ts, entry_price = open_entry
                exit_price = float(row["close"])
                if entry_price > 0:
                    bars_held_days = (ts.date() - entry_ts.date()).days if hasattr(ts, "date") else None
                    trades.append({
                        "entry_ts": str(entry_ts.date() if hasattr(entry_ts, "date") else entry_ts),
                        "exit_ts": str(ts.date() if hasattr(ts, "date") else ts),
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "return_pct": (exit_price - entry_price) / entry_price * 100,
                        "bars_held": bars_held_days,
                    })
                open_entry = None
    return trades


def _stats(returns: List[float]) -> Dict:
    n = len(returns)
    if n == 0:
        return {"n": 0, "mean": 0.0, "median": 0.0, "stdev": 0.0,
                "sharpe_ish": 0.0, "win_rate": 0.0, "min": 0.0, "max": 0.0,
                "total_return_pct": 0.0}
    mean = sum(returns) / n
    sd = statistics.stdev(returns) if n > 1 else 0.0
    sharpe = (mean / sd) if sd > 0 else 0.0
    wr = sum(1 for r in returns if r > 0) / n
    total = 1.0
    for r in returns:
        total *= (1 + r / 100)
    return {
        "n": n,
        "mean": round(mean, 4),
        "median": round(statistics.median(returns), 4),
        "stdev": round(sd, 4),
        "sharpe_ish": round(sharpe, 4),
        "win_rate": round(wr, 4),
        "min": round(min(returns), 4),
        "max": round(max(returns), 4),
        "total_return_pct": round((total - 1) * 100, 4),
    }


def _verdict_for(stats: Dict) -> str:
    n, mean, sharpe = stats["n"], stats["mean"], stats["sharpe_ish"]
    if n < 10:
        return "UNTESTED"
    if n >= 20 and mean > 0 and sharpe >= 0.20:
        return "PASS"
    if n >= 20 and mean >= 0 and sharpe >= 0.0:
        return "MARGINAL"
    return "FAIL"


def _aggregate_verdict(per_symbol: Dict[str, Dict]) -> str:
    """Promote MARGINAL -> PASS_WITH_NUANCE if any symbol PASSes."""
    verdicts = [s["verdict"] for s in per_symbol.values()]
    if any(v == "PASS" for v in verdicts):
        return "PASS_WITH_NUANCE" if not all(v == "PASS" for v in verdicts) else "PASS"
    if any(v == "MARGINAL" for v in verdicts):
        return "MARGINAL"
    if all(v == "UNTESTED" for v in verdicts):
        return "UNTESTED"
    return "FAIL"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--universe", required=True,
                        help="comma-separated symbols, e.g. GDX,KRE,XHB")
    parser.add_argument("--lookback-days", type=int, default=730)
    parser.add_argument("--print-trades", action="store_true",
                        help="print every trade for the largest-sample symbol")
    parser.add_argument("--no-update-records", action="store_true",
                        help="don't write the verdict back to records.jsonl")
    args = parser.parse_args()

    fn = _load_compute_fn(args.strategy_id)
    symbols = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    if not symbols:
        print("no symbols specified")
        return 1

    from backtest.data import load_bars  # imported lazily so tests don't need yfinance
    end = date.today()
    start = end - timedelta(days=args.lookback_days)
    print(f"loading {len(symbols)} symbols of daily bars "
          f"{start.isoformat()} -> {end.isoformat()}...")
    bars_by_sym = load_bars(symbols, start=start.isoformat(), end=end.isoformat(),
                             interval="1d", source="yf")
    missing = [s for s in symbols if s not in bars_by_sym]
    if missing:
        print(f"  missing bars: {missing}")

    per_symbol: Dict[str, Dict] = {}
    test_runs: List[Dict] = []
    today_iso = end.isoformat()
    period_str = f"{start.isoformat()} to {end.isoformat()}"

    for sym in symbols:
        bars = bars_by_sym.get(sym)
        if bars is None or bars.empty or len(bars) < 30:
            per_symbol[sym] = {"verdict": "UNTESTED", "stats": _stats([]),
                                "trades": [], "note": "no/insufficient bars"}
            continue
        try:
            signals = fn(bars)
        except Exception as e:
            per_symbol[sym] = {"verdict": "FAIL", "stats": _stats([]),
                                "trades": [], "note": f"compute_fn raised: {e!s:.200}"}
            continue
        trades = _pair_signals(signals)
        rets = [t["return_pct"] for t in trades]
        stats = _stats(rets)
        verdict = _verdict_for(stats)
        per_symbol[sym] = {"verdict": verdict, "stats": stats, "trades": trades}
        test_runs.append({
            "test_id": f"{args.strategy_id}-{sym}-validate-{today_iso}",
            "date_iso": today_iso,
            "instrument": sym,
            "timeframe": "1d",
            "period": period_str,
            "trades": stats["n"],
            "win_rate_pct": round(stats["win_rate"] * 100, 2),
            "sharpe": stats["sharpe_ish"],
            "total_return_pct": stats["total_return_pct"],
            "max_drawdown_pct": None,
            "verdict": verdict,
        })

    overall = _aggregate_verdict(per_symbol)
    print()
    print(f"=== VALIDATION REPORT — {args.strategy_id} ===")
    print(f"{'symbol':<10} {'n':>4}  {'mean':>8}  {'WR':>6}  {'sharpe':>7}  "
          f"{'total':>8}  verdict")
    for sym, info in per_symbol.items():
        s = info["stats"]
        print(f"  {sym:<8} {s['n']:>4}  {s['mean']:+7.2f}%  "
              f"{s['win_rate']*100:>5.1f}%  {s['sharpe_ish']:>+7.3f}  "
              f"{s['total_return_pct']:+7.2f}%  {info['verdict']}")
    print()
    print(f"OVERALL VERDICT: {overall}")

    if args.print_trades:
        biggest = max(per_symbol.items(), key=lambda kv: kv[1]["stats"]["n"])
        sym, info = biggest
        print(f"\nTrades on {sym} (n={info['stats']['n']}):")
        for t in info["trades"][:30]:
            print(f"  {t['entry_ts']} → {t['exit_ts']} "
                  f"({t.get('bars_held','?')}d) "
                  f"{t['entry_price']:.2f} -> {t['exit_price']:.2f}  "
                  f"{t['return_pct']:+.2f}%")
        if len(info["trades"]) > 30:
            print(f"  ... +{len(info['trades']) - 30} more")

    if not args.no_update_records:
        records = _load_records()
        record = _find_record(records, args.strategy_id)
        if record is None:
            print("warning: strategy not in records.jsonl; skipping verdict update")
        else:
            extra = record.get("extra", {}) or {}
            extra["tested"] = True
            extra["test_runs"] = (extra.get("test_runs") or []) + test_runs
            extra["current_verdict"] = overall
            n_tot = sum(per_symbol[s]["stats"]["n"] for s in per_symbol)
            extra["verdict_summary"] = (
                f"validated {today_iso} on {','.join(symbols)} "
                f"({args.lookback_days}d, {n_tot} trades total). overall={overall}"
            )
            extra["last_updated_iso"] = today_iso
            record["extra"] = extra
            _save_records(records)
            conn = db.init_db()
            try:
                db.upsert_strategy(conn, record)
            finally:
                conn.close()
            print(f"\nupdated records.jsonl + trading.db with verdict={overall}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
