"""Validate a trend-following strategy from strategies/trend/.

Wraps validate_strategy.validate_strategy_record() with direct compute_fn
injection so it bypasses the records.jsonl + strategies/generated/ path
that the main validator assumes.

Run from conda env `trading` (needs yfinance + alpaca-py):
  conda run -n trading python scripts/validate_trend_strategy.py \\
      --name donchian_breakout_20 \\
      --universe SPY,QQQ,IWM,NVDA,TSLA,GLD,TLT \\
      --lookback-days 1825
"""
import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_strategy import validate_strategy_record


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True,
                   help="trend strategy module name, e.g. donchian_breakout_20")
    p.add_argument("--universe", required=True,
                   help="comma-separated symbols")
    p.add_argument("--lookback-days", type=int, default=1825,
                   help="default 5 years (1825 days)")
    p.add_argument("--interval", default="1d")
    p.add_argument("--source", default=None)
    p.add_argument("--print-trades", action="store_true")
    args = p.parse_args()

    mod = importlib.import_module(f"strategies.trend.{args.name}")
    fn_name = f"compute_{args.name}"
    fn = getattr(mod, fn_name, None)
    if fn is None:
        candidates = [n for n in dir(mod) if n.startswith("compute_")]
        if len(candidates) == 1:
            fn = getattr(mod, candidates[0])
        else:
            print(f"ERROR: no compute_* function in {mod} (candidates: {candidates})")
            return 1

    symbols = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    print(f"loading {len(symbols)} symbols × {args.lookback_days}d {args.interval} bars...")

    result = validate_strategy_record(
        args.name, symbols,
        lookback_days=args.lookback_days,
        fn=fn,
        interval=args.interval,
        source=args.source,
    )

    per_symbol = result["per_symbol"]
    overall = result["overall_verdict"]

    print()
    print(f"=== VALIDATION REPORT — {args.name} ===")
    print(f"{'symbol':<10} {'n':>4}  {'mean':>8}  {'WR':>6}  {'sharpe':>7}  {'total':>8}  verdict")
    for sym, info in per_symbol.items():
        s = info["stats"]
        note = info.get("note", "")
        print(f"  {sym:<8} {s['n']:>4}  {s['mean']:+7.2f}%  "
              f"{s['win_rate']*100:>5.1f}%  {s['sharpe_ish']:>+7.3f}  "
              f"{s['total_return_pct']:+7.2f}%  {info['verdict']}"
              + (f"  ({note})" if note else ""))
    print()
    print(f"OVERALL VERDICT: {overall}")

    if args.print_trades:
        with_trades = {k: v for k, v in per_symbol.items() if v["stats"]["n"] > 0}
        if with_trades:
            biggest = max(with_trades.items(), key=lambda kv: kv[1]["stats"]["n"])
            sym, info = biggest
            print(f"\nTrades on {sym} (n={info['stats']['n']}):")
            for t in info["trades"][:30]:
                print(f"  {t['entry_ts']} → {t['exit_ts']} "
                      f"({t.get('bars_held','?')}d) "
                      f"{t['entry_price']:.2f} -> {t['exit_price']:.2f}  "
                      f"{t['return_pct']:+.2f}%")
            if len(info["trades"]) > 30:
                print(f"  ... +{len(info['trades']) - 30} more")

    return 0 if overall in ("PASS", "PASS_WITH_NUANCE") else 1


if __name__ == "__main__":
    sys.exit(main())
