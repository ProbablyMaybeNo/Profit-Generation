"""
strategy_fires.py — Check each TRACKED_STRATEGIES entry against today's bars.

Returns a list of (strategy_id, symbol) tuples for strategies that produced
a long_entry signal on today's most recent bar.
"""

from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

import pandas as pd

import importlib
import re

from backtest.data import load_bars
from monitoring.config import TRACKED_STRATEGIES

# PG-014 (3.5.1): unify with intraday_monitor._resolve_compute_fn so EOD
# fire checks and intraday scans agree on which strategies are
# resolvable. Adding a new compute module here = adding it in one place,
# not two.
_COMPUTE_FN_MODULES = [
    "strategies.mean_reversion.botnet101",
    "strategies.trend",
    "strategies.trend.donchian_breakout_20",
    "strategies.trend.ma_cross_20_50",
    "strategies.trend.new_high_volume",
    "strategies.intraday.mean_reversion_intraday",
    "strategies.intraday.orb_1m",
    "strategies.intraday.momentum_1m",
    "strategies.intraday.vwap_reclaim_1m",
    "strategies.intraday.candle_continuation",
    "strategies.orb.orbo_intraday",
    "strategies.orb.orb_pivots_intraday",
    "strategies.breakout.donchian_retest",
    "strategies.breakout.donchian_retest_short",
]


def _resolve_compute_fn(name: str):
    """Resolve a compute_fn by name. Looks in:
      1. Static module list (botnet101).
      2. strategies.generated.* — codegen-emitted single-strategy modules.
    Returns the first match, raises ValueError if none found.
    """
    # 1. Static modules.
    for mod_path in _COMPUTE_FN_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except Exception:
            continue
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    # 2. Generated modules — one file per strategy. Same convention as
    #    validate_strategy._safe_filename: lowercase + underscores. The
    #    convention is `strategies.generated.<stem>` exposing
    #    `compute_<stem>`, so we map the function name to its stem by
    #    stripping the leading `compute_`.
    bare = name[len("compute_"):] if name.startswith("compute_") else name
    safe = re.sub(r"[^a-zA-Z0-9_]+", "_", bare).strip("_").lower()
    seen = set()
    for candidate in (f"strategies.generated.{safe}",
                       f"strategies.generated.{bare}",
                       f"strategies.generated.{name}"):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            mod = importlib.import_module(candidate)
        except Exception:
            continue
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
        # Fall back to a single compute_* function in the module.
        compute_fns = [getattr(mod, n) for n in dir(mod)
                        if n.startswith("compute_") and callable(getattr(mod, n))]
        if len(compute_fns) == 1:
            return compute_fns[0]
    # Back-compat: keep the original error message so existing tests pass.
    raise ValueError(f"compute fn {name!r} not found in botnet101 module")


def check_fires(as_of: date) -> List[Dict]:
    """
    For each tracked strategy on each of its active symbols, fetch the last 60
    daily bars ending on `as_of` and evaluate whether `long_entry` is True
    on the most recent bar.

    Returns: list of {strategy_id, symbol, fired (bool), bar_date, close}
    """
    end = (as_of + timedelta(days=1)).isoformat()
    # F1 (audit 2026-06-03): the old 120-day window (~84 trading bars) starved
    # any strategy gating on a 200-bar SMA (rsi2/rsi14-oversold) — sma200 was
    # 100% NaN on the loaded history so long_entry could never be True. Widen
    # to 320 calendar days (~220 trading bars) so a 200-bar SMA is non-NaN on
    # the latest bar. Bollinger (<=34 bars) is unaffected by the wider window.
    start = (as_of - timedelta(days=320)).isoformat()
    fires: List[Dict] = []

    # Filter out intraday-class strategies — their compute_fns are designed for
    # 5m/15m bars; calling them on 1d bars produces spurious signals (e.g.,
    # intraday-mr-3bar-low-15m fired on 1d bars 2026-05-19). Intraday strategies
    # have their own scanner via monitoring.intraday_fires.
    eod_strategies = [e for e in TRACKED_STRATEGIES
                      if isinstance(e, dict) and e.get("bar_interval", "1d") == "1d"]
    needed_symbols = sorted({s for entry in eod_strategies for s in entry["active_on"]})

    cache: Dict[str, pd.DataFrame] = {}
    for sym in needed_symbols:
        try:
            data = load_bars([sym], start=start, end=end, interval="1d", source="yf")
        except Exception as e:
            fires.append({
                "strategy_id": "load_error",
                "symbol": sym,
                "fired": False,
                "error": str(e)[:200],
            })
            continue
        if sym in data and not data[sym].empty:
            cache[sym] = data[sym]

    for entry in eod_strategies:
        sid = entry["id"]
        compute_fn = _resolve_compute_fn(entry["compute"])
        for symbol in entry["active_on"]:
            df = cache.get(symbol)
            if df is None or df.empty:
                continue
            try:
                signals = compute_fn(df)
            except Exception as e:
                fires.append({
                    "strategy_id": sid,
                    "symbol": symbol,
                    "fired": False,
                    "error": f"compute_error: {e!s:.200s}",
                })
                continue
            if signals.empty:
                continue
            last = signals.iloc[-1]
            fires.append({
                "strategy_id": sid,
                "symbol": symbol,
                "bar_date": str(signals.index[-1].date()),
                "close": float(last.get("close", df.iloc[-1]["close"])),
                "fired": bool(last.get("long_entry", False)),
                "long_exit_signal": bool(last.get("long_exit", False)),
            })

    return fires


if __name__ == "__main__":
    import json, sys
    today = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    results = check_fires(today)
    fired = [r for r in results if r.get("fired")]
    print(f"Checked {len(results)} (strategy,symbol) pairs as of {today}")
    print(f"Fires today: {len(fired)}")
    for r in fired:
        print(f"  FIRE: {r['strategy_id']} on {r['symbol']} (close={r.get('close', 'N/A')})")
    errs = [r for r in results if "error" in r]
    if errs:
        print(f"\n{len(errs)} errors")
        for r in errs[:5]:
            print(f"  {r['strategy_id']} {r['symbol']}: {r['error']}")
