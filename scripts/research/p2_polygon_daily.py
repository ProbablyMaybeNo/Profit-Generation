"""
p2_polygon_daily.py — Long-history daily-bar loader via Polygon list_aggs (one call per symbol).

OFFLINE RESEARCH ONLY. Pulls multi-year daily OHLCV for the ETF universe + candidates,
caches each per-symbol frame (cache.py) so re-runs are free. Mirrors the project's
existing DataFrame contract: index=ts (naive), cols=open/high/low/close/volume.

Rate-limited to Polygon free tier (5 calls/min => ~13s floor). One list_aggs call
covers the entire date span for a symbol.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.cache import cache_get, cache_set  # noqa: E402
from config.utils import load_credentials, log  # noqa: E402

_FREE_TIER_MIN_INTERVAL = 13.0
_last_call_at = 0.0
_CACHE_TTL = 30 * 24 * 3600  # 30 days; daily bars are immutable once the day closes


def _client():
    from polygon import RESTClient
    return RESTClient(load_credentials("polygon")["api_key"])


def _rate_limit():
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < _FREE_TIER_MIN_INTERVAL:
        time.sleep(_FREE_TIER_MIN_INTERVAL - elapsed)
    _last_call_at = time.time()


def load_daily(symbol: str, start: str, end: str, client=None) -> pd.DataFrame:
    """Daily bars for one symbol over [start, end]. Cached per (symbol,start,end)."""
    key = f"p2.polygon.daily:{symbol}:{start}:{end}"
    hit = cache_get(key)
    if hit is not None:
        return hit

    c = client or _client()
    rows = []
    for attempt in range(4):
        _rate_limit()
        try:
            aggs = list(c.list_aggs(symbol, 1, "day", start, end, limit=50000))
            break
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "429" in msg or "exceed" in msg.lower():
                wait = 30 * (attempt + 1)
                log(f"polygon 429 on {symbol}, sleeping {wait}s", "WARNING")
                time.sleep(wait)
                continue
            log(f"polygon error on {symbol}: {msg[:200]}", "ERROR")
            return pd.DataFrame()
    else:
        log(f"polygon list_aggs({symbol}) failed after retries", "ERROR")
        return pd.DataFrame()

    for a in aggs:
        rows.append({
            "ts": datetime.utcfromtimestamp(a.timestamp / 1000),
            "open": a.open, "high": a.high, "low": a.low,
            "close": a.close, "volume": a.volume or 0.0,
        })
    if not rows:
        df = pd.DataFrame()
    else:
        df = pd.DataFrame(rows).set_index("ts").sort_index()
        df.index = pd.to_datetime(df.index).normalize()
    cache_set(key, df, _CACHE_TTL)
    return df


def load_many(symbols: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
    c = _client()
    out: Dict[str, pd.DataFrame] = {}
    for i, s in enumerate(symbols, 1):
        df = load_daily(s, start, end, client=c)
        if not df.empty:
            out[s] = df
            log(f"[{i}/{len(symbols)}] {s}: {len(df)} bars "
                f"{df.index[0].date()}..{df.index[-1].date()}", "INFO")
        else:
            log(f"[{i}/{len(symbols)}] {s}: EMPTY", "WARNING")
    return out


# Proven EOD core (n>=70 closed outcomes) + candidate sector/industry/broad ETFs.
PROVEN_CORE = ["GDX", "KRE", "XHB", "XBI", "XME", "IWM", "XOP", "XLE", "QQQ"]
CANDIDATE_ETFS = [
    "SPY", "DIA", "XLK", "XLF", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
    "XLRE", "XLC", "SMH", "GLD", "SLV", "USO", "TLT", "IEF", "HYG", "LQD",
    "EEM", "EFA", "ARKK",
]
ALL_SYMBOLS = PROVEN_CORE + [s for s in CANDIDATE_ETFS if s not in PROVEN_CORE]


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2019-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-01"
    log(f"Pulling daily bars {start}..{end} for {len(ALL_SYMBOLS)} symbols", "INFO")
    data = load_many(ALL_SYMBOLS, start, end)
    print(f"\nLoaded {len(data)}/{len(ALL_SYMBOLS)} symbols")
    spans = {s: (df.index[0].date().isoformat(), df.index[-1].date().isoformat(), len(df))
             for s, df in data.items()}
    import json
    print(json.dumps(spans, indent=2))
