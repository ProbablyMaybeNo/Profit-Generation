"""
intraday_fires.py — Run intraday-bar strategies and commit fires to the
signals table.

Mirrors `monitoring.strategy_fires.check_fires` but for intraday bars.
Iterates TRACKED_STRATEGIES entries whose declaration sets
`bar_interval` to anything other than `"1d"` (e.g. `"5m"`, `"15m"`,
`"1h"`), loads recent intraday bars via `backtest.data.load_intraday_bars`,
runs each strategy's compute_fn, and records fires through `db.record_signal`.

Idempotent on (strategy_id, symbol, bar_ts, bar_interval, signal_type)
— UNIQUE constraint on the signals table prevents double-inserts when
the same bar is scanned twice (e.g. two 15-min schtask fires that land
inside the same bar window).
"""

from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

import pandas as pd

from backtest.data import load_intraday_bars
from data import db
from monitoring.config import TRACKED_CRYPTO, TRACKED_STRATEGIES
from monitoring.strategy_fires import _resolve_compute_fn

DEFAULT_LOOKBACK_BARS = 200

# 7.6 fix: the runner fires every 15 minutes. When a strategy's bar
# interval is shorter than the runner cadence, checking only the most
# recent bar misses signals that fired on intervening bars. The scan
# window is the number of most-recent bars to evaluate per run; the
# UNIQUE(strategy_id, symbol, bar_ts, bar_interval, signal_type)
# constraint on the signals table dedupes any overlap across runs.
# 15m and longer keep the original 1-bar behavior (zero behavior change
# for those strategies).
SCAN_WINDOWS = {
    "1m": 20,
    "5m": 5,
    "15m": 1,
    "30m": 1,
    "1h": 1,
    "4h": 1,
}
DEFAULT_SCAN_WINDOW = 1


def _in_window(now: datetime, window) -> bool:
    """Check `now` (ET clock) against an `active_in_window` declaration.

    Formats accepted (5.3.2):
      - None / empty       → always active
      - "HH:MM-HH:MM"      → single window
      - "HH:MM-HH:MM ET"   → trailing zone tag stripped
      - ["HH:MM-HH:MM", …] → list of windows, active if `now` is inside any
    """
    if not window:
        return True
    if isinstance(window, (list, tuple)):
        return any(_in_window(now, w) for w in window)
    # Strip trailing zone tags like " ET" so the plan's literal format works.
    spec = str(window).strip()
    for tag in (" ET", " EST", " EDT", " UTC"):
        if spec.upper().endswith(tag):
            spec = spec[: -len(tag)].strip()
            break
    try:
        start_s, end_s = spec.split("-", 1)
        start_t = dt_time.fromisoformat(start_s.strip())
        end_t = dt_time.fromisoformat(end_s.strip())
    except (ValueError, AttributeError):
        return True
    cur = now.time()
    if start_t <= end_t:
        return start_t <= cur <= end_t
    return cur >= start_t or cur <= end_t


def intraday_strategies(declarations: List[Dict]) -> List[Dict]:
    """Filter `declarations` down to entries with bar_interval != '1d'."""
    out = []
    for entry in declarations:
        interval = entry.get("bar_interval", "1d")
        if interval == "1d":
            continue
        out.append(entry)
    return out


def check_intraday_fires(
    asof: Optional[datetime] = None,
    *,
    declarations: Optional[List[Dict]] = None,
    bar_loader: Callable = load_intraday_bars,
    conn=None,
    min_bars: int = 25,
) -> List[Dict]:
    """
    Scan intraday-bar strategies once and commit fires.

    Returns a list of {strategy_id, symbol, bar_ts, bar_interval,
    signal_type, close, signal_id (or None if dupe)} dicts — one entry
    per fire detected, including duplicates that were prevented by the
    UNIQUE constraint (signal_id will be None for those).
    """
    # ET-aware "now" so the active_in_window gate compares against the
    # Eastern market clock (window strings are "HH:MM-HH:MM ET") and the bar
    # loader derives the correct UTC fetch window. A caller-supplied asof
    # (tests) is honored verbatim.
    asof = asof or datetime.now(MARKET_TZ)
    declarations = (declarations if declarations is not None
                    else TRACKED_STRATEGIES)
    targets = intraday_strategies(declarations)

    fires: List[Dict] = []
    if not targets:
        return fires

    by_interval: Dict[str, List[Dict]] = {}
    for entry in targets:
        by_interval.setdefault(entry["bar_interval"], []).append(entry)

    own_conn = conn is None
    if own_conn:
        conn = db.init_db()

    try:
        for interval, entries in by_interval.items():
            symbols = sorted({
                sym for e in entries for sym in e["active_on"]
                if sym not in TRACKED_CRYPTO
            })
            if not symbols:
                continue
            bars_by_symbol = bar_loader(
                symbols, interval, DEFAULT_LOOKBACK_BARS, now=asof,
            )
            for entry in entries:
                if not _in_window(asof, entry.get("active_in_window")):
                    continue
                sid = entry["id"]
                try:
                    compute_fn = _resolve_compute_fn(entry["compute"])
                except ValueError:
                    continue
                for symbol in entry["active_on"]:
                    if symbol in TRACKED_CRYPTO:
                        continue
                    df = bars_by_symbol.get(symbol)
                    if df is None or len(df) < min_bars:
                        continue
                    try:
                        signals = compute_fn(df)
                    except Exception:
                        continue
                    if signals is None or signals.empty:
                        continue
                    scan_n = SCAN_WINDOWS.get(interval, DEFAULT_SCAN_WINDOW)
                    window_df = signals.tail(scan_n)
                    extra = {
                        "asof": asof.isoformat(timespec="seconds"),
                        "source": "intraday_fires",
                        "bar_interval": interval,
                    }
                    for i in range(len(window_df)):
                        row = window_df.iloc[i]
                        bar_ts = window_df.index[i]
                        if isinstance(bar_ts, pd.Timestamp):
                            bar_ts_iso = bar_ts.isoformat()
                        else:
                            bar_ts_iso = str(bar_ts)
                        close = float(row.get("close", df["close"].iloc[-1]))
                        if bool(row.get("long_entry", False)):
                            sig_id = db.record_signal(
                                conn,
                                strategy_id=sid, symbol=symbol,
                                bar_ts=bar_ts_iso,
                                signal_type="long_entry",
                                close=close, bar_interval=interval,
                                extra=extra,
                            )
                            fires.append({
                                "strategy_id": sid, "symbol": symbol,
                                "bar_ts": bar_ts_iso,
                                "bar_interval": interval,
                                "signal_type": "long_entry",
                                "close": close, "signal_id": sig_id,
                            })
                        if bool(row.get("long_exit", False)):
                            sig_id = db.record_signal(
                                conn,
                                strategy_id=sid, symbol=symbol,
                                bar_ts=bar_ts_iso,
                                signal_type="long_exit",
                                close=close, bar_interval=interval,
                                extra=extra,
                            )
                            fires.append({
                                "strategy_id": sid, "symbol": symbol,
                                "bar_ts": bar_ts_iso,
                                "bar_interval": interval,
                                "signal_type": "long_exit",
                                "close": close, "signal_id": sig_id,
                            })
        return fires
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", type=str, default=None,
                        help="ISO datetime override (default: now)")
    parser.add_argument("--no-market-check", action="store_true",
                        help="Skip market_is_open check (testing / off-hours)")
    args = parser.parse_args()

    if not args.no_market_check:
        from config.utils import market_is_open
        if not market_is_open():
            print(json.dumps({"market_open": False, "scanned": False}))
            sys.exit(0)

    asof = datetime.fromisoformat(args.asof) if args.asof else None
    result = check_intraday_fires(asof=asof)
    summary = {
        "fires": len(result),
        "by_strategy": {},
        "by_interval": {},
    }
    for r in result:
        summary["by_strategy"][r["strategy_id"]] = (
            summary["by_strategy"].get(r["strategy_id"], 0) + 1
        )
        summary["by_interval"][r["bar_interval"]] = (
            summary["by_interval"].get(r["bar_interval"], 0) + 1
        )
    print(json.dumps(summary, indent=2))
