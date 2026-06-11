"""
collect_intraday_history.py — Track B data collector for the intraday
trend-following build (Stage 2, docs/INTRADAY_TREND_BUILD_PLAN.md).

OFFLINE research only. Uses the Alpaca *historical* data client
(StockHistoricalDataClient) — never the trading client, never submits orders.

Fetches HISTORICAL intraday bars (5m + 15m by default; add 1m with --with-1m)
for the Tier-1 + Tier-2 universe in data/universes/intraday_candidates.csv,
going back as far as the free IEX feed practically allows (target 6-12 months),
and caches them per interval to data/intraday_history_<interval>.pkl as
{symbol: DataFrame[open/high/low/close/volume]} (index = America/New_York
naive bar timestamps, ascending).

Coverage is documented per symbol (interval, date range, #bars) — no silent
caps. Whatever the source returns is exactly what gets logged and pickled.

CLI:
  py -3.13 -m scripts.collect_intraday_history
  py -3.13 -m scripts.collect_intraday_history --months 9 --with-1m
  py -3.13 -m scripts.collect_intraday_history --intervals 5m,15m --no-cache-write

Tier-1 + Tier-2 universe (per the plan's locked decision):
  AMD TSLA PLTR NVDA COIN  (Tier 1)
  META TQQQ SOXL SMH AMZN  (Tier 2)
"""

from __future__ import annotations

import argparse
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import load_credentials, log  # noqa: E402

# Tier-1 + Tier-2 only — the live trade vehicles. SPY/QQQ/IWM/XLK etc. are
# trend filters, not trade vehicles, so they stay out of the backtest universe.
TIER1 = ["AMD", "TSLA", "PLTR", "NVDA", "COIN"]
TIER2 = ["META", "TQQQ", "SOXL", "SMH", "AMZN"]
DEFAULT_UNIVERSE = TIER1 + TIER2

DEFAULT_INTERVALS = ["5m", "15m"]
DEFAULT_MONTHS = 9  # target 6-12; IEX free feed governs the real ceiling

_TF_MAP = {
    "1m": ("Minute", 1),
    "5m": ("Minute", 5),
    "15m": ("Minute", 15),
}

OUT_TEMPLATE = "intraday_history_{interval}.pkl"


def out_path(interval: str, *, root: Path = ROOT) -> Path:
    return root / "data" / OUT_TEMPLATE.format(interval=interval)


def coverage_for(df: pd.DataFrame) -> Dict:
    """Pure: summarise one symbol's frame — #bars and first/last bar ts.

    Empty / None frames report zero bars and null dates so the coverage
    report never silently hides a gap.
    """
    if df is None or len(df) == 0:
        return {"bars": 0, "start": None, "end": None}
    idx = df.index
    return {
        "bars": int(len(df)),
        "start": str(idx[0]),
        "end": str(idx[-1]),
    }


def coverage_report(data: Dict[str, pd.DataFrame],
                    universe: List[str]) -> List[Dict]:
    """Pure: per-symbol coverage rows for `universe`, including symbols that
    came back empty (bars=0) so missing data is explicit, not omitted."""
    rows: List[Dict] = []
    for sym in universe:
        cov = coverage_for(data.get(sym))
        rows.append({"symbol": sym, **cov})
    return rows


def _to_et_naive(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize an Alpaca bars frame to ET-naive index + OHLCV columns,
    matching backtest.data conventions. Pure given a single-symbol frame."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if isinstance(out.index, pd.MultiIndex):
        out = out.reset_index(level=0, drop=True)
    out.index = pd.to_datetime(out.index)
    try:
        out.index = out.index.tz_convert("America/New_York").tz_localize(None)
    except (TypeError, AttributeError):
        out.index = out.index.tz_localize(None)
    cols = [c for c in ("open", "high", "low", "close", "volume")
            if c in out.columns]
    return out[cols].sort_index()


def _split_multi(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Split a multi-symbol Alpaca bars frame into {symbol: ET-naive frame}."""
    out: Dict[str, pd.DataFrame] = {}
    if df is None or df.empty:
        return out
    if isinstance(df.index, pd.MultiIndex):
        for sym in df.index.get_level_values(0).unique():
            sub = df.xs(sym, level=0)
            out[str(sym).upper()] = _to_et_naive(sub)
    return out


def _alpaca_fetcher(
    symbols: List[str],
    interval: str,
    start: datetime,
    end: datetime,
) -> Dict[str, pd.DataFrame]:
    """Single historical-data request for all symbols at one interval.
    alpaca-py paginates internally, so one call returns the full window."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    unit_name, amount = _TF_MAP[interval]
    tf = TimeFrame(amount, getattr(TimeFrameUnit, unit_name))

    creds = load_credentials("alpaca")
    client = StockHistoricalDataClient(creds["api_key"], creds["secret_key"])

    req = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=tf,
        start=start.astimezone(timezone.utc),
        end=end.astimezone(timezone.utc),
        feed="iex",
    )
    bars = client.get_stock_bars(req)
    df = bars.df
    return _split_multi(df)


def collect_interval(
    symbols: List[str],
    interval: str,
    *,
    months: int = DEFAULT_MONTHS,
    now: Optional[datetime] = None,
    fetcher: Optional[Callable] = None,
) -> Dict[str, pd.DataFrame]:
    """Fetch `months` of `interval` bars for `symbols`. Returns
    {symbol: ET-naive OHLCV frame}; empties dropped. fetcher is injected
    in tests so neither alpaca nor the network is touched."""
    if interval not in _TF_MAP:
        raise ValueError(f"unsupported interval {interval!r}; "
                         f"choose from {sorted(_TF_MAP)}")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    start = now - timedelta(days=int(months * 31))
    fetch = fetcher or _alpaca_fetcher
    raw = fetch(symbols, interval, start, now)
    return {s: df for s, df in raw.items() if df is not None and not df.empty}


def write_cache(data: Dict[str, pd.DataFrame], interval: str,
                *, root: Path = ROOT) -> Path:
    path = out_path(interval, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        pickle.dump(data, fh)
    return path


def _print_coverage(interval: str, rows: List[Dict]) -> None:
    total_bars = sum(r["bars"] for r in rows)
    log(f"=== coverage {interval} (total {total_bars} bars across "
        f"{len(rows)} symbols) ===", "INFO")
    for r in rows:
        if r["bars"] == 0:
            log(f"  {r['symbol']:<6} {interval:<4} EMPTY - no bars returned",
                "WARNING")
        else:
            log(f"  {r['symbol']:<6} {interval:<4} {r['bars']:>7} bars "
                f"{r['start']} .. {r['end']}", "INFO")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intervals", type=str, default=None,
                        help="Comma list, e.g. 5m,15m. Default 5m,15m.")
    parser.add_argument("--with-1m", action="store_true",
                        help="Also collect 1m bars (IEX has shallow 1m depth).")
    parser.add_argument("--months", type=int, default=DEFAULT_MONTHS,
                        help=f"Lookback in months (default {DEFAULT_MONTHS}).")
    parser.add_argument("--no-cache-write", action="store_true",
                        help="Fetch + report coverage but do not write pickles.")
    args = parser.parse_args(argv)

    if args.intervals:
        intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    else:
        intervals = list(DEFAULT_INTERVALS)
        if args.with_1m:
            intervals = ["1m"] + intervals

    universe = DEFAULT_UNIVERSE
    log(f"intraday history collect: {len(universe)} symbols, "
        f"intervals={intervals}, months={args.months}", "INFO")

    rc = 0
    for interval in intervals:
        log(f"--- fetching {interval} ---", "INFO")
        try:
            data = collect_interval(universe, interval, months=args.months)
        except Exception as exc:  # noqa: BLE001
            log(f"{interval}: fetch failed: {exc}", "ERROR")
            rc = 1
            continue
        rows = coverage_report(data, universe)
        _print_coverage(interval, rows)
        if all(r["bars"] == 0 for r in rows):
            log(f"{interval}: ZERO bars for every symbol - source "
                f"unavailable or rate-limited", "ERROR")
            rc = 1
            continue
        if not args.no_cache_write:
            path = write_cache(data, interval)
            log(f"{interval}: wrote {path}", "SUCCESS")
    return rc


if __name__ == "__main__":
    sys.exit(main())
