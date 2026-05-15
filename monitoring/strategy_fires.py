"""
strategy_fires.py — Check each TRACKED_STRATEGIES entry against today's bars.

Returns a list of (strategy_id, symbol) tuples for strategies that produced
a long_entry signal on today's most recent bar.
"""

from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

import pandas as pd

from backtest.data import load_bars
from monitoring.config import TRACKED_STRATEGIES
from strategies.mean_reversion import botnet101


def _resolve_compute_fn(name: str):
    fn = getattr(botnet101, name, None)
    if fn is None:
        raise ValueError(f"compute fn {name!r} not found in botnet101 module")
    return fn


def check_fires(as_of: date) -> List[Dict]:
    """
    For each tracked strategy on each of its active symbols, fetch the last 60
    daily bars ending on `as_of` and evaluate whether `long_entry` is True
    on the most recent bar.

    Returns: list of {strategy_id, symbol, fired (bool), bar_date, close}
    """
    end = (as_of + timedelta(days=1)).isoformat()
    start = (as_of - timedelta(days=120)).isoformat()
    fires: List[Dict] = []

    needed_symbols = sorted({s for entry in TRACKED_STRATEGIES for s in entry["active_on"]})

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

    for entry in TRACKED_STRATEGIES:
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
