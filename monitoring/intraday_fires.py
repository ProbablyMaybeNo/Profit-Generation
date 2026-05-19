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

import pandas as pd

from backtest.data import load_intraday_bars
from data import db
from monitoring.config import TRACKED_CRYPTO, TRACKED_STRATEGIES
from monitoring.strategy_fires import _resolve_compute_fn

DEFAULT_LOOKBACK_BARS = 200


def _in_window(now: datetime, window: Optional[str]) -> bool:
    """Check `now` (ET clock) against an `active_in_window` declaration.

    Format: "HH:MM-HH:MM" (inclusive on both ends). None => always active.
    """
    if not window:
        return True
    try:
        start_s, end_s = window.split("-", 1)
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
    asof = asof or datetime.now()
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
                    last = signals.iloc[-1]
                    bar_ts = signals.index[-1]
                    if isinstance(bar_ts, pd.Timestamp):
                        bar_ts_iso = bar_ts.isoformat()
                    else:
                        bar_ts_iso = str(bar_ts)
                    close = float(last.get("close", df["close"].iloc[-1]))
                    extra = {
                        "asof": asof.isoformat(timespec="seconds"),
                        "source": "intraday_fires",
                        "bar_interval": interval,
                    }
                    if bool(last.get("long_entry", False)):
                        sig_id = db.record_signal(
                            conn,
                            strategy_id=sid, symbol=symbol,
                            bar_ts=bar_ts_iso, signal_type="long_entry",
                            close=close, bar_interval=interval, extra=extra,
                        )
                        fires.append({
                            "strategy_id": sid, "symbol": symbol,
                            "bar_ts": bar_ts_iso, "bar_interval": interval,
                            "signal_type": "long_entry", "close": close,
                            "signal_id": sig_id,
                        })
                    if bool(last.get("long_exit", False)):
                        sig_id = db.record_signal(
                            conn,
                            strategy_id=sid, symbol=symbol,
                            bar_ts=bar_ts_iso, signal_type="long_exit",
                            close=close, bar_interval=interval, extra=extra,
                        )
                        fires.append({
                            "strategy_id": sid, "symbol": symbol,
                            "bar_ts": bar_ts_iso, "bar_interval": interval,
                            "signal_type": "long_exit", "close": close,
                            "signal_id": sig_id,
                        })
        return fires
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", type=str, default=None,
                        help="ISO datetime override (default: now)")
    args = parser.parse_args()
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
