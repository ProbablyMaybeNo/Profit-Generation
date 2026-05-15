"""
cache.py — sqlite-backed TTL cache for external API calls.
Cuts API calls 90%+ for stable data (FRED daily series, Polygon news, yfinance EOD).
"""

import functools
import hashlib
import pickle
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

from config.utils import get_project_root, log

CACHE_FILE = get_project_root() / "data" / "cache.db"


def _conn() -> sqlite3.Connection:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(CACHE_FILE), timeout=5.0)
    c.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        "key TEXT PRIMARY KEY, "
        "value BLOB NOT NULL, "
        "expires_at REAL NOT NULL"
        ")"
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires_at)")
    return c


def _hash_args(namespace: str, args: tuple, kwargs: dict) -> str:
    payload = pickle.dumps((args, sorted(kwargs.items())), protocol=4)
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"{namespace}:{digest}"


def cache_get(key: str) -> Optional[Any]:
    """Return cached value, or None if missing or expired."""
    with _conn() as c:
        row = c.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    value_blob, expires_at = row
    if expires_at < time.time():
        return None
    return pickle.loads(value_blob)


def cache_set(key: str, value: Any, ttl_seconds: float) -> None:
    """Store a value with a TTL."""
    expires_at = time.time() + ttl_seconds
    blob = pickle.dumps(value, protocol=4)
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, blob, expires_at),
        )


def cache_clear(namespace: Optional[str] = None) -> int:
    """Delete cache entries. Returns count deleted. Pass namespace to scope."""
    with _conn() as c:
        if namespace is None:
            cur = c.execute("DELETE FROM cache")
        else:
            cur = c.execute("DELETE FROM cache WHERE key LIKE ?", (f"{namespace}:%",))
        return cur.rowcount


def cache_purge_expired() -> int:
    """Delete all expired entries. Run periodically."""
    with _conn() as c:
        cur = c.execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))
        return cur.rowcount


def cached(ttl: float, namespace: Optional[str] = None) -> Callable:
    """
    Decorator: cache function results in sqlite with a TTL (seconds).
    Args/kwargs must be picklable. Result must be picklable.
    Namespace defaults to module.qualname.
    """
    def decorator(fn: Callable) -> Callable:
        ns = namespace or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = _hash_args(ns, args, kwargs)
            hit = cache_get(key)
            if hit is not None:
                return hit
            value = fn(*args, **kwargs)
            try:
                cache_set(key, value, ttl)
            except (pickle.PicklingError, sqlite3.Error) as e:
                log(f"cache_set failed for {ns}: {e}", "WARNING")
            return value

        wrapper.cache_clear = lambda: cache_clear(ns)  # type: ignore[attr-defined]
        return wrapper

    return decorator


if __name__ == "__main__":
    print(f"Cache file: {CACHE_FILE}")

    @cached(ttl=60, namespace="selftest")
    def slow(x):
        time.sleep(0.2)
        return x * 2

    cache_clear("selftest")
    t0 = time.time(); slow(7); t_miss = time.time() - t0
    t0 = time.time(); slow(7); t_hit = time.time() - t0
    print(f"miss: {t_miss*1000:.1f}ms  hit: {t_hit*1000:.1f}ms")
    print(f"speedup: {t_miss/max(t_hit, 1e-6):.0f}x")
    print(f"purged expired: {cache_purge_expired()}")
    cache_clear("selftest")
