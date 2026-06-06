"""
trend_scanner.py — Wide-universe trend scanner (milestone 5.5.3.1).

For every TRACKED_STRATEGIES entry whose `strategy_class == "trend"`,
scan ~600 symbols (S&P 500 + Nasdaq-100 + high-volume ETFs after the
liquidity filter) and commit fires into the `signals` table.

This is a SEPARATE path from the regular EOD fire-check in
`monitoring.strategy_fires`. The regular path keeps each trend
strategy's narrow `active_on` field (SPY/QQQ/IWM) for back-compat —
the wide scan deliberately BYPASSES that field and uses the universe
loader's symbol list instead.

Pipeline:
  1. Load trend universe via `monitoring.universe.load_trend_universe`.
  2. Filter by dollar volume via `monitoring.liquidity.filter_by_dollar_volume`.
  3. Fetch ~100 daily bars per symbol (batched).
  4. For each trend strategy + filtered symbol, run compute_fn and
     record `long_entry` / `long_exit` fires.

Idempotent on (strategy_id, symbol, bar_ts, bar_interval, signal_type)
— same UNIQUE constraint as every other signal producer.

Auto-trader picks up these fires through the normal `process_signals`
path (5.5.4.2 adds capacity capping).
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402
from monitoring.config import TRACKED_STRATEGIES  # noqa: E402
from monitoring.liquidity import filter_by_dollar_volume  # noqa: E402
from monitoring.strategy_fires import _resolve_compute_fn  # noqa: E402
from monitoring.universe import load_trend_universe  # noqa: E402
from monitoring import position_manager as pm_owner  # noqa: E402

DEFAULT_LOOKBACK_BARS = 100
DEFAULT_BAR_INTERVAL = "1d"
MIN_BARS_REQUIRED = 30  # need enough history for slow MA / Donchian


def trend_strategies(declarations: List[Dict]) -> List[Dict]:
    """Filter `declarations` down to entries with strategy_class == 'trend'."""
    return [
        e for e in declarations
        if e.get("strategy_class") == "trend"
    ]


def _default_bar_loader(symbols: List[str], lookback_days: int,
                       interval: str = "1d") -> Dict[str, "pd.DataFrame"]:
    """Default bar loader: batched Alpaca fetch via wide_bars (5.5.3.2).

    `lookback_days` here is the requested bar count — wide_bars handles
    the date math.
    """
    from monitoring.wide_bars import fetch_wide_daily_bars
    return fetch_wide_daily_bars(symbols, lookback_bars=lookback_days)


def scan_trend_universe(
    *,
    asof: Optional[datetime] = None,
    declarations: Optional[List[Dict]] = None,
    universe_loader: Callable[[], List[str]] = load_trend_universe,
    liquidity_filter: Callable = filter_by_dollar_volume,
    bar_loader: Optional[Callable] = None,
    conn=None,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    min_usd: float = 50_000_000.0,
    universe_override: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Run the wide-universe trend scan. Returns a list of fire records:
        {strategy_id, symbol, bar_ts, bar_interval, signal_type, close,
         signal_id (None if dupe)}

    `universe_override` (list) bypasses the loader+filter — used by the
    smoke test for a deterministic universe.
    """
    asof = asof or datetime.now()
    decls = declarations if declarations is not None else TRACKED_STRATEGIES
    targets = trend_strategies(decls)

    if not targets:
        return []

    # 1. Load + filter universe
    if universe_override is not None:
        filtered = [s.upper() for s in universe_override]
    else:
        wide = universe_loader()
        log(f"trend_scanner: loaded {len(wide)} symbols from universe")
        filtered = liquidity_filter(wide, min_usd=min_usd)
        log(f"trend_scanner: {len(filtered)} pass dollar-volume filter "
            f"(min ${min_usd:,.0f})")

    if not filtered:
        log("trend_scanner: empty universe after filter — nothing to scan",
            level="WARNING")
        return []

    # 2. Fetch bars (single batched call — the loader handles pacing).
    if bar_loader is None:
        bars = _default_bar_loader(filtered, lookback_bars + 20)
    else:
        bars = bar_loader(filtered, lookback_bars + 20)
    log(f"trend_scanner: fetched bars for {len(bars)} / {len(filtered)} symbols")

    # 3. Open DB connection if not provided.
    own_conn = conn is None
    if own_conn:
        conn = db.init_db()

    # Auto-seed any tracked strategies that aren't yet in the strategies
    # table. Prevents FK failure when scanner records signals for newly-
    # added trend declarations.
    try:
        db.ensure_strategies_seeded(conn, decls)
    except Exception as exc:  # noqa: BLE001
        log(f"trend_scanner: ensure_strategies_seeded skipped: {exc}",
            level="WARNING")

    fires: List[Dict] = []
    try:
        for entry in targets:
            sid = entry["id"]
            try:
                compute_fn = _resolve_compute_fn(entry["compute"])
            except ValueError as exc:
                log(f"trend_scanner: skip {sid} — {exc}", level="WARNING")
                continue
            for symbol in filtered:
                df = bars.get(symbol)
                if df is None or len(df) < MIN_BARS_REQUIRED:
                    continue
                try:
                    signals = compute_fn(df)
                except Exception as exc:  # noqa: BLE001
                    # One bad symbol must not kill the whole scan
                    log(f"trend_scanner: compute_fn raised for "
                        f"{sid}/{symbol}: {exc}", level="WARNING")
                    continue
                if signals is None or len(signals) == 0:
                    continue
                last = signals.iloc[-1]
                bar_ts = signals.index[-1]
                # CRITICAL: auto_trader.process_signals matches 1d signals by
                # date-only string (`asof.isoformat()` → "YYYY-MM-DD"). Live
                # Alpaca bars come back as pd.Timestamp("YYYY-MM-DDTHH:MM:SS")
                # which would never match. Normalize to date-only here so
                # scanner fires actually get picked up by the auto-trader.
                if hasattr(bar_ts, "date"):
                    bar_ts_iso = bar_ts.date().isoformat()
                elif hasattr(bar_ts, "isoformat"):
                    bar_ts_iso = bar_ts.isoformat()[:10]
                else:
                    bar_ts_iso = str(bar_ts)[:10]
                try:
                    close = float(last.get("close", df["close"].iloc[-1]))
                except Exception:  # noqa: BLE001
                    close = None
                extra = {
                    "asof": asof.isoformat(timespec="seconds"),
                    "source": "trend_scanner",
                    "bar_interval": DEFAULT_BAR_INTERVAL,
                    "wide_universe": True,
                }
                if bool(last.get("long_entry", False)):
                    sig_id = db.record_signal(
                        conn,
                        strategy_id=sid, symbol=symbol,
                        bar_ts=bar_ts_iso, signal_type="long_entry",
                        close=close, bar_interval=DEFAULT_BAR_INTERVAL,
                        extra=extra,
                    )
                    fires.append({
                        "strategy_id": sid, "symbol": symbol,
                        "bar_ts": bar_ts_iso, "bar_interval": DEFAULT_BAR_INTERVAL,
                        "signal_type": "long_entry", "close": close,
                        "signal_id": sig_id,
                    })
                # M4 (Sprint 3) — gate exit-signal recording to an owned holding
                # (per M1/M2). The wide-universe trend scanner sweeps hundreds of
                # symbols; recording a long_exit for any symbol the strategy
                # doesn't actually hold is pure spam and hands a non-owner a SELL
                # signal. Only record the exit when THIS strategy owns the symbol.
                if bool(last.get("long_exit", False)) and \
                        pm_owner.owns_symbol(conn, sid, symbol):
                    sig_id = db.record_signal(
                        conn,
                        strategy_id=sid, symbol=symbol,
                        bar_ts=bar_ts_iso, signal_type="long_exit",
                        close=close, bar_interval=DEFAULT_BAR_INTERVAL,
                        extra=extra,
                    )
                    fires.append({
                        "strategy_id": sid, "symbol": symbol,
                        "bar_ts": bar_ts_iso, "bar_interval": DEFAULT_BAR_INTERVAL,
                        "signal_type": "long_exit", "close": close,
                        "signal_id": sig_id,
                    })
    finally:
        if own_conn:
            conn.close()

    return fires


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--min-usd", type=float, default=50_000_000.0)
    parser.add_argument("--limit-universe", type=int, default=0,
                        help="If >0, scan only the first N symbols (debug)")
    args = parser.parse_args()

    override = None
    if args.limit_universe > 0:
        override = load_trend_universe()[: args.limit_universe]

    result = scan_trend_universe(
        min_usd=args.min_usd,
        universe_override=override,
    )
    summary = {
        "fires": len(result),
        "by_strategy": {},
        "by_signal_type": {},
    }
    for r in result:
        summary["by_strategy"][r["strategy_id"]] = (
            summary["by_strategy"].get(r["strategy_id"], 0) + 1
        )
        summary["by_signal_type"][r["signal_type"]] = (
            summary["by_signal_type"].get(r["signal_type"], 0) + 1
        )
    print(json.dumps(summary, indent=2))
