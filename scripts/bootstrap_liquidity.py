"""bootstrap_liquidity.py — Populate liquidity_snapshots for the trend
universe. Run once after Phase 5.5 ships, then daily via run_daily.bat.

Usage:
  py -3.13 scripts/bootstrap_liquidity.py
  py -3.13 scripts/bootstrap_liquidity.py --universe trend
  py -3.13 scripts/bootstrap_liquidity.py --symbols SPY,QQQ,IWM
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitoring import liquidity  # noqa: E402
from monitoring.universe import load_trend_universe  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--universe", default="trend",
                   help="which universe to populate (default: trend)")
    p.add_argument("--symbols", default=None,
                   help="comma-separated override (skips universe loader)")
    p.add_argument("--lookback-days", type=int, default=20,
                   help="window for avg dollar volume (default: 20)")
    args = p.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.universe == "trend":
        symbols = load_trend_universe()
    else:
        print(f"unknown universe: {args.universe}")
        return 1

    print(f"populating liquidity_snapshots for {len(symbols)} symbols "
          f"({args.lookback_days}d lookback)...")
    result = liquidity.populate_liquidity_snapshots(
        symbols, lookback_days=args.lookback_days
    )
    print(f"done. {len(result)} symbols populated.")
    print()
    print("top 10 by avg dollar volume:")
    sorted_syms = sorted(result.items(), key=lambda kv: kv[1][0], reverse=True)
    for sym, (adv, last_close) in sorted_syms[:10]:
        print(f"  {sym:<6} adv=${adv:>15,.0f}  close=${last_close:.2f}")
    skipped = len(symbols) - len(result)
    if skipped > 0:
        print()
        print(f"{skipped} symbols had no bars / insufficient data (no-op for them).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
