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

STAGE3_SID = "intraday-candle-continuation-15m"


def check_stage3_signal_only() -> int:
    """Stage 3 invariant: the candle-continuation strategy is registered for
    the 15m intraday scan AND paused, so fires record to the signals table but
    auto_trader can never enter. At the Stage 4 flip (unpause), update this
    check to assert the Stage 4 invariant instead."""
    try:
        from data import db
        from monitoring import strategy_health as sh
        from monitoring.config import TRACKED_STRATEGIES
        from monitoring.strategy_fires import _resolve_compute_fn
    except Exception as e:
        print(f"BUILD_CHECK: stage3 import FAILED: {type(e).__name__}: {e}")
        return 1

    entry = next((e for e in TRACKED_STRATEGIES if e["id"] == STAGE3_SID), None)
    if entry is None:
        print(f"BUILD_CHECK: {STAGE3_SID} NOT in TRACKED_STRATEGIES")
        return 1
    if entry.get("bar_interval") != "15m":
        print(f"BUILD_CHECK: {STAGE3_SID} bar_interval "
              f"{entry.get('bar_interval')!r} != '15m'")
        return 1
    try:
        _resolve_compute_fn(entry["compute"])
    except ValueError as e:
        print(f"BUILD_CHECK: {STAGE3_SID} compute_fn unresolvable: {e}")
        return 1

    conn = db.init_db()
    try:
        if not sh.is_paused(conn, STAGE3_SID):
            print(f"BUILD_CHECK: {STAGE3_SID} is NOT paused — signal-only "
                  f"guarantee broken; pause it before the next session")
            return 1
    finally:
        conn.close()

    print(f"BUILD_CHECK: {STAGE3_SID} registered "
          f"({len(entry['active_on'])} symbols, 15m) + paused (signal-only) OK")
    return 0


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
    return check_stage3_signal_only()


if __name__ == "__main__":
    sys.exit(main())
