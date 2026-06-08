"""
intraday_build_check.py — real-data smoke for the intraday trend-following
build (docs/INTRADAY_TREND_BUILD_PLAN.md). Read-only; trades nothing.

Confirms the continuation strategy imports and produces a valid signal frame
on live Tier-1 intraday bars. Exit 0 = OK, 1 = a real failure (exception or
malformed output). Missing bars off-hours is a WARNING, not a failure — the
unit tests already prove correctness; this only adds a live-wiring check.
"""

from __future__ import annotations

import sys

SMOKE_SYMBOLS = ["AMD", "NVDA", "TSLA"]
REQUIRED_COLS = ("long_entry", "long_exit", "ema_fast", "ema_slow", "vwap")


def main() -> int:
    try:
        from backtest.data import load_intraday_bars
        from strategies.intraday.candle_continuation import (
            compute_candle_continuation,
        )
    except Exception as e:  # import-time failure is a hard fail
        print(f"BUILD_CHECK: import FAILED: {type(e).__name__}: {e}")
        return 1

    try:
        bars = load_intraday_bars(SMOKE_SYMBOLS, interval="5m",
                                  lookback_bars=300)
    except Exception as e:
        print(f"BUILD_CHECK: bar fetch error (treated as warning): {e}")
        return 0

    got_any = False
    for sym in SMOKE_SYMBOLS:
        df = bars.get(sym)
        if df is None or getattr(df, "empty", True):
            print(f"BUILD_CHECK: {sym} no bars (off-hours?) - skipped")
            continue
        got_any = True
        try:
            out = compute_candle_continuation(df)
        except Exception as e:
            print(f"BUILD_CHECK: {sym} compute FAILED: {type(e).__name__}: {e}")
            return 1
        missing = [c for c in REQUIRED_COLS if c not in out.columns]
        if missing:
            print(f"BUILD_CHECK: {sym} missing columns {missing}")
            return 1
        if out["long_entry"].dtype != bool or out["long_exit"].dtype != bool:
            print(f"BUILD_CHECK: {sym} signal columns not boolean")
            return 1
        print(f"BUILD_CHECK: {sym} OK | {len(df)} bars | "
              f"entries {int(out['long_entry'].sum())} | "
              f"exits {int(out['long_exit'].sum())}")

    if not got_any:
        print("BUILD_CHECK: no bars for any symbol (off-hours) - WARN, pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
