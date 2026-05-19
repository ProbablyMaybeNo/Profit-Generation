"""
wide_bars.py — Batched daily-bar fetcher for the wide trend scanner
(milestone 5.5.3.2).

The default per-symbol loop in `backtest.data.load_bars` makes 600
sequential HTTP calls — too slow for a 5-min EOD budget. This module
fetches in batches of 20-50 symbols per Alpaca request (Alpaca's
StockBarsRequest natively accepts a list), with a per-symbol cache
keyed to the most recent completed bar close so repeat invocations
within the same trading day are free.

The cache lives in `data/cache.db` (the shared `config.cache` store),
namespaced separately from intraday + EOD per-symbol caches.

Failure isolation: if one batch fails, the function falls back to
single-symbol fetches for that batch (slower but bounded), so a
single bad symbol can't poison the whole 600-symbol scan.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.cache import cache_get, cache_set  # noqa: E402
from config.utils import load_credentials, log  # noqa: E402


DEFAULT_BATCH_SIZE = 50  # symbols per Alpaca request
DEFAULT_LOOKBACK_BARS = 100
CACHE_NAMESPACE = "wide_bars.alpaca"
CACHE_TTL_SEC = 36 * 3600  # 36h — survives the bar-close cliff comfortably


def _cache_key(symbol: str, bar_close_date: str, lookback: int) -> str:
    return f"{CACHE_NAMESPACE}|{symbol}|{bar_close_date}|{lookback}"


def _last_completed_close(now: Optional[datetime] = None) -> str:
    """ISO date string of the last completed US-market daily close.

    Conservative: any time before 16:30 ET on a given day, treat the
    previous date as the most recent close. Off-by-one wrong on
    weekends/holidays is fine — the cache namespace still partitions
    correctly per calendar date.
    """
    now = now or datetime.now()
    if now.hour < 16 or (now.hour == 16 and now.minute < 30):
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def fetch_wide_daily_bars(
    symbols: List[str],
    *,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    alpaca_fetcher: Optional[Callable] = None,
    as_of: Optional[datetime] = None,
    bypass_cache: bool = False,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch ~`lookback_bars` daily bars for each symbol. Returns
    {symbol: DataFrame[open/high/low/close/volume]}.

    Cached per-symbol, keyed to the most recent completed bar-close
    date. Within the same trading day, the second call returns
    cache hits for any symbol fetched earlier.
    """
    syms = list(dict.fromkeys(s.upper() for s in symbols))
    if not syms:
        return {}

    bar_date = _last_completed_close(as_of)

    out: Dict[str, pd.DataFrame] = {}
    missing: List[str] = []

    # Try cache first
    if not bypass_cache:
        for sym in syms:
            cached = cache_get(_cache_key(sym, bar_date, lookback_bars))
            if isinstance(cached, pd.DataFrame) and not cached.empty:
                out[sym] = cached
                continue
            missing.append(sym)
    else:
        missing = list(syms)

    if not missing:
        return out

    fetcher = alpaca_fetcher or _alpaca_batch_fetcher

    # Fetch in batches
    for i in range(0, len(missing), batch_size):
        batch = missing[i:i + batch_size]
        try:
            batch_result = fetcher(batch, lookback_bars=lookback_bars,
                                   as_of=as_of)
        except Exception as exc:  # noqa: BLE001
            log(f"wide_bars: batch fetch failed (size={len(batch)}, {exc}) "
                f"— falling back to per-symbol",
                level="WARNING")
            batch_result = {}
            for sym in batch:
                try:
                    one = fetcher([sym], lookback_bars=lookback_bars,
                                  as_of=as_of)
                    batch_result.update(one)
                except Exception as inner:  # noqa: BLE001
                    log(f"wide_bars: per-symbol fallback failed for {sym}: "
                        f"{inner}", level="WARNING")

        for sym, df in batch_result.items():
            if df is None or df.empty:
                continue
            out[sym] = df
            cache_set(
                _cache_key(sym, bar_date, lookback_bars),
                df,
                CACHE_TTL_SEC,
            )

    return out


# ---------------------------------------------------------------------------
# Alpaca batch fetcher
# ---------------------------------------------------------------------------


def _alpaca_batch_fetcher(
    symbols: List[str],
    *,
    lookback_bars: int,
    as_of: Optional[datetime] = None,
) -> Dict[str, pd.DataFrame]:
    """Fetch `lookback_bars` daily bars for `symbols` in a single Alpaca call."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    creds = load_credentials("alpaca")
    client = StockHistoricalDataClient(creds["api_key"], creds["secret_key"])
    end = (as_of or datetime.now()).replace(tzinfo=timezone.utc)
    # Pad lookback with weekends/holidays
    start = end - timedelta(days=int(lookback_bars * 1.7) + 10)

    req = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=start,
        end=end,
        feed="iex",
    )
    bars = client.get_stock_bars(req)
    df = bars.df
    if df is None or df.empty:
        return {}

    # MultiIndex (symbol, timestamp). Split per symbol.
    out: Dict[str, pd.DataFrame] = {}
    if isinstance(df.index, pd.MultiIndex):
        for sym in df.index.get_level_values(0).unique():
            sub = df.xs(sym, level=0).copy()
            sub.index = pd.to_datetime(sub.index)
            try:
                sub.index = sub.index.tz_convert("America/New_York").tz_localize(None)
            except Exception:  # noqa: BLE001
                sub.index = sub.index.tz_localize(None)
            sub = sub.rename(columns={"open": "open", "high": "high",
                                       "low": "low", "close": "close",
                                       "volume": "volume"})
            cols = [c for c in ("open", "high", "low", "close", "volume")
                    if c in sub.columns]
            out[sym.upper()] = sub[cols].sort_index().tail(lookback_bars)
    else:
        # Single-symbol response — no symbol level in index
        if len(symbols) == 1:
            sym = symbols[0].upper()
            df2 = df.copy()
            df2.index = pd.to_datetime(df2.index)
            try:
                df2.index = df2.index.tz_convert("America/New_York").tz_localize(None)
            except Exception:  # noqa: BLE001
                df2.index = df2.index.tz_localize(None)
            out[sym] = df2[[c for c in ("open", "high", "low", "close", "volume")
                            if c in df2.columns]].sort_index().tail(lookback_bars)
    return out


