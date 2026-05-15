"""
polygon_data.py — Historical universe data from Polygon.io.

Free-tier friendly: uses /v2/aggs/grouped/locale/us/market/stocks/{date}, which
returns OHLCV for every US stock for one day in a single call. Cached aggressively
since these are immutable once a trading day closes.
"""

import time
from datetime import date, datetime, timedelta
from typing import List, Optional

import pandas as pd
from polygon import RESTClient
from polygon.exceptions import BadResponse

from config.cache import cached
from config.utils import load_credentials, log


_FREE_TIER_MIN_INTERVAL = 13.0
_last_call_at = 0.0


def _client() -> RESTClient:
    return RESTClient(load_credentials("polygon")["api_key"])


def _rate_limit():
    """Polygon free tier = 5 calls/min. Floor inter-call gap at ~13s."""
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < _FREE_TIER_MIN_INTERVAL:
        time.sleep(_FREE_TIER_MIN_INTERVAL - elapsed)
    _last_call_at = time.time()


@cached(ttl=30 * 24 * 3600, namespace="polygon.grouped_daily.raw")
def grouped_daily(d: str) -> pd.DataFrame:
    """
    Return DataFrame of all US stock daily bars for date d (ISO YYYY-MM-DD).
    Columns: ticker, open, high, low, close, volume, vwap, transactions.
    Empty DataFrame on weekends/holidays. Cached 30 days.

    Uses adjusted=False to get prices as they actually traded that day,
    matching Alpaca's intraday bars and the reality the trader saw on screen.
    """
    for attempt in range(4):
        _rate_limit()
        try:
            rows = list(_client().get_grouped_daily_aggs(date=d, adjusted=False))
            break
        except BadResponse as e:
            if "429" in str(e) or "exceed" in str(e).lower():
                wait = 30 * (attempt + 1)
                log(f"polygon 429 on {d}, sleeping {wait}s", "WARNING")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            if "429" in str(e):
                wait = 30 * (attempt + 1)
                log(f"polygon 429 on {d}, sleeping {wait}s", "WARNING")
                time.sleep(wait)
                continue
            raise
    else:
        raise RuntimeError(f"polygon grouped_daily({d}) failed after 4 retries")
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{
        "ticker": r.ticker,
        "open": r.open,
        "high": r.high,
        "low": r.low,
        "close": r.close,
        "volume": r.volume or 0,
        "vwap": r.vwap,
        "transactions": r.transactions or 0,
    } for r in rows])
    return df


def trading_days(start: str, end: str) -> List[str]:
    """
    Return list of ISO dates between start and end that have grouped daily data
    (i.e., were trading days). Caller-side cheap probe of weekends/holidays.
    """
    cur = datetime.fromisoformat(start).date()
    last = datetime.fromisoformat(end).date()
    out: List[str] = []
    while cur <= last:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


@cached(ttl=24 * 3600, namespace="polygon.news")
def news_for(ticker: str, since_iso: str, until_iso: str, limit: int = 50) -> List[dict]:
    """
    Return news articles for a ticker between two ISO datetimes.
    Each entry: {published_utc, title, publisher, description}.
    """
    c = _client()
    items = []
    for n in c.list_ticker_news(
        ticker=ticker,
        published_utc_gte=since_iso,
        published_utc_lte=until_iso,
        order="asc",
        limit=limit,
    ):
        items.append({
            "published_utc": str(n.published_utc),
            "title": n.title or "",
            "publisher": getattr(n.publisher, "name", "") if n.publisher else "",
            "description": (n.description or "")[:500],
        })
        if len(items) >= limit:
            break
    return items


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "2024-06-14"
    df = grouped_daily(d)
    print(f"{d}: {len(df)} rows")
    print(df.head(3).to_string())
    print("\nlargest gappers (close > prev = N/A here, just biggest movers within day):")
    df["range_pct"] = (df["high"] - df["low"]) / df["low"] * 100
    print(df.nlargest(5, "range_pct")[["ticker", "open", "close", "volume", "range_pct"]].to_string(index=False))
