"""
news_fetcher.py — Pull recent headlines per tracked symbol from
Polygon /v2/reference/news, cache them, persist into trading.db.

Free-tier safe: 5 calls / 60s rate limit, 30-minute response cache.
Failures (network, 429, bad JSON) are logged and yield an empty list —
the daily report should never break because news fetching hiccupped.
"""

import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.cache import cache_get, cache_set  # noqa: E402
from config.utils import load_credentials, log  # noqa: E402
from data import db  # noqa: E402

NEWS_URL = "https://api.polygon.io/v2/reference/news"
NEWS_CACHE_NS = "polygon_news"
NEWS_CACHE_TTL_SEC = 1800
DEFAULT_LIMIT = 5
DEFAULT_MAX_AGE_HOURS = 168

RATE_LIMIT_CALLS = 5
RATE_LIMIT_WINDOW_SEC = 60.0

_call_history: deque = deque(maxlen=RATE_LIMIT_CALLS)


def _rate_limit_block() -> None:
    """Sleep until we can make another call without exceeding 5/60s."""
    now = time.time()
    if len(_call_history) >= RATE_LIMIT_CALLS:
        oldest = _call_history[0]
        wait = RATE_LIMIT_WINDOW_SEC - (now - oldest)
        if wait > 0:
            time.sleep(wait + 0.05)
    _call_history.append(time.time())


def _http_get(url: str, params: Dict, timeout: float = 15.0):
    """Indirection seam — tests mock this to avoid hitting Polygon."""
    import requests
    return requests.get(url, params=params, timeout=timeout)


def _normalize(symbol: str, raw: Dict) -> Dict:
    publisher = raw.get("publisher") or {}
    return {
        "polygon_id": raw.get("id"),
        "published_utc": raw.get("published_utc"),
        "symbol": symbol,
        "title": raw.get("title"),
        "url": raw.get("article_url"),
        "author": raw.get("author"),
        "publisher": publisher.get("name") if isinstance(publisher, dict) else publisher,
        "description": raw.get("description"),
        "tickers": raw.get("tickers") or [],
        "keywords": raw.get("keywords") or [],
        "insights": raw.get("insights"),
    }


def _within_age(published_utc: Optional[str], cutoff: datetime) -> bool:
    if not published_utc:
        return True
    try:
        pub = datetime.fromisoformat(published_utc.replace("Z", "+00:00"))
    except ValueError:
        return True
    return pub >= cutoff


def fetch_news_for_symbol(
    symbol: str,
    *,
    limit: int = DEFAULT_LIMIT,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    use_cache: bool = True,
) -> List[Dict]:
    """Return up to `limit` recent headlines for one symbol. Empty list on failure."""
    cache_key = f"{NEWS_CACHE_NS}:{symbol}:{limit}:{max_age_hours}"
    if use_cache:
        hit = cache_get(cache_key)
        if hit is not None:
            return hit

    try:
        creds = load_credentials("polygon")
    except Exception as e:
        log(f"news fetch: missing polygon credentials ({e})", "WARNING")
        return []
    api_key = creds.get("api_key")
    if not api_key or "PASTE_YOUR" in str(api_key):
        log("news fetch: polygon api_key not configured", "WARNING")
        return []

    _rate_limit_block()
    try:
        resp = _http_get(NEWS_URL, params={
            "ticker": symbol,
            "limit": limit,
            "sort": "published_utc",
            "order": "desc",
            "apiKey": api_key,
        })
    except Exception as e:
        log(f"news fetch network error for {symbol}: {e}", "WARNING")
        return []

    status = getattr(resp, "status_code", 0)
    if status == 429:
        log(f"news fetch rate-limited (429) on {symbol}", "WARNING")
        return []
    if status != 200:
        log(f"news fetch bad status {status} on {symbol}", "WARNING")
        return []

    try:
        payload = resp.json()
    except Exception as e:
        log(f"news fetch bad JSON for {symbol}: {e}", "WARNING")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items: List[Dict] = []
    for raw in payload.get("results") or []:
        if not _within_age(raw.get("published_utc"), cutoff):
            continue
        items.append(_normalize(symbol, raw))

    if use_cache:
        try:
            cache_set(cache_key, items, NEWS_CACHE_TTL_SEC)
        except Exception as e:
            log(f"news cache write failed for {symbol}: {e}", "WARNING")
    return items


def persist_news_items(items: Iterable[Dict]) -> int:
    """Insert each item into trading.db.news. Returns count newly inserted."""
    items = list(items)
    if not items:
        return 0
    conn = db.init_db()
    inserted = 0
    try:
        for item in items:
            if db.insert_news(conn, item) is not None:
                inserted += 1
    finally:
        conn.close()
    return inserted


def fetch_and_persist_for_universe(
    symbols: Iterable[str],
    *,
    limit: int = DEFAULT_LIMIT,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    use_cache: bool = True,
) -> Dict[str, List[Dict]]:
    """
    Fetch + persist news for each symbol (deduped, original order).
    Returns dict symbol → list of normalized news items (empty if none / fetch failed).
    """
    out: Dict[str, List[Dict]] = {}
    for sym in dict.fromkeys(symbols):
        items = fetch_news_for_symbol(
            sym, limit=limit, max_age_hours=max_age_hours, use_cache=use_cache,
        )
        if items:
            persist_news_items(items)
        out[sym] = items
    return out


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="*", help="Symbols to fetch (default: tracked universe)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    if args.symbols:
        syms = args.symbols
    else:
        from monitoring.config import TRACKED_STOCKS, TRACKED_SECTORS
        syms = TRACKED_STOCKS + TRACKED_SECTORS

    print(f"Fetching news for {len(syms)} symbols (limit={args.limit}/sym, "
          f"rate-limited 5/60s)...")
    total_returned = 0
    total_inserted = 0
    for sym in syms:
        items = fetch_news_for_symbol(sym, limit=args.limit, use_cache=not args.no_cache)
        ins = persist_news_items(items)
        total_returned += len(items)
        total_inserted += ins
        print(f"  {sym:<8}  fetched={len(items):<3}  newly_inserted={ins}")
    print(f"Done. Total fetched={total_returned}  newly inserted={total_inserted}")
