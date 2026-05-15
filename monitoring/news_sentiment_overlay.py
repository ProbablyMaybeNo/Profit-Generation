"""
news_sentiment_overlay.py — Slice closed outcomes by the sentiment of
news on the traded symbol around the entry date.

For each closed 1d outcome, look up news in the `news` table on the
trade's symbol published within ±1 day of entry_ts. Parse Polygon's
per-ticker insights JSON and count positive / neutral / negative
labels that match the symbol (insights without a matching ticker are
ignored). The trade is bucketed by its dominant sentiment — the label
with the highest count, with ties resolved as 'neutral' so a single
positive vs. single negative doesn't force a class. Trades with no
news in the window go to bucket 'no_news'.

The output is a flat list of (strategy_id, sentiment_bucket) rows with
(n, mean_ret) — the dashboard renders it as a table per strategy.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


WINDOW_DAYS = 1
BUCKETS = ("positive", "neutral", "negative", "no_news")


def _parse_iso_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_pub_date(s: Optional[str]) -> Optional[date]:
    """Parse polygon's published_utc (RFC3339)."""
    if not s:
        return None
    try:
        # Drop trailing Z and tz suffix — we only need the date.
        head = s.replace("Z", "+00:00")
        return datetime.fromisoformat(head).date()
    except ValueError:
        return _parse_iso_date(s)


def _safe_stats(returns: Sequence[float]) -> Dict:
    rets = list(returns)
    n = len(rets)
    if n == 0:
        return {"n": 0, "mean": 0.0, "win_rate": 0.0,
                "median": 0.0, "stdev": 0.0,
                "min": 0.0, "max": 0.0}
    mean = sum(rets) / n
    sd = statistics.stdev(rets) if n > 1 else 0.0
    wr = sum(1 for r in rets if r > 0) / n
    return {
        "n": n,
        "mean": round(mean, 4),
        "win_rate": round(wr, 4),
        "median": round(statistics.median(rets), 4),
        "stdev": round(sd, 4),
        "min": round(min(rets), 4),
        "max": round(max(rets), 4),
    }


# ---------------------------------------------------------------------------
# DB pulls
# ---------------------------------------------------------------------------

def fetch_closed_outcomes(conn: sqlite3.Connection) -> List[Dict]:
    rows = conn.execute(
        "SELECT s.strategy_id, s.symbol, o.entry_ts, o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval = '1d'"
    ).fetchall()
    return [
        {"strategy_id": r["strategy_id"], "symbol": r["symbol"],
         "entry_ts": r["entry_ts"], "return_pct": float(r["return_pct"])}
        for r in rows
    ]


def fetch_news_by_symbol(conn: sqlite3.Connection) -> Dict[str, List[Dict]]:
    """Return {symbol: [{published_date, sentiment_raw}]}.

    Only rows with a non-null sentiment payload are returned — others
    can't be classified.
    """
    rows = conn.execute(
        "SELECT symbol, published_utc, sentiment FROM news "
        " WHERE sentiment IS NOT NULL"
    ).fetchall()
    out: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        d = _parse_pub_date(r["published_utc"])
        if d is None:
            continue
        out[r["symbol"]].append({"date": d, "sentiment_raw": r["sentiment"]})
    return dict(out)


# ---------------------------------------------------------------------------
# Sentiment parsing
# ---------------------------------------------------------------------------

def extract_sentiment_labels(raw: Optional[str], symbol: str) -> List[str]:
    """Return the sentiment labels (positive/neutral/negative) in `raw`
    that mention `symbol` in their `ticker` field.

    raw is the JSON-encoded payload from news.sentiment (Polygon's
    insights list). Malformed or empty payloads yield [].
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: List[str] = []
    sym_u = (symbol or "").upper()
    for ent in data:
        if not isinstance(ent, dict):
            continue
        t = (ent.get("ticker") or "").upper()
        if t != sym_u:
            continue
        s = (ent.get("sentiment") or "").lower()
        if s in ("positive", "neutral", "negative"):
            out.append(s)
    return out


def dominant_label(labels: Iterable[str]) -> Optional[str]:
    """Majority label among positive/neutral/negative. Ties resolve to
    'neutral'. Empty input → None."""
    cnt = Counter(labels)
    if not cnt:
        return None
    # Pick the label with the highest count. Ties between positive/negative
    # are 'neutral'; ties with neutral keep neutral.
    top = cnt.most_common()
    best_count = top[0][1]
    tied = [lab for lab, c in top if c == best_count]
    if len(tied) == 1:
        return tied[0]
    if "neutral" in tied:
        return "neutral"
    # positive vs negative tie with no neutral → cancel out.
    return "neutral"


def bucket_for_trade(
    symbol: str, entry_d: date, news_for_sym: Sequence[Dict],
    *, window_days: int = WINDOW_DAYS,
) -> str:
    """Aggregate ±window_days of news on this symbol around entry_d into
    one of: positive / neutral / negative / no_news."""
    if not news_for_sym:
        return "no_news"
    lo = entry_d - timedelta(days=window_days)
    hi = entry_d + timedelta(days=window_days)
    labels: List[str] = []
    for n in news_for_sym:
        if lo <= n["date"] <= hi:
            labels.extend(extract_sentiment_labels(n["sentiment_raw"], symbol))
    if not labels:
        return "no_news"
    dominant = dominant_label(labels)
    return dominant or "no_news"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def slice_outcomes_by_sentiment(
    outcomes: Iterable[Dict],
    news_by_sym: Dict[str, List[Dict]],
    *, window_days: int = WINDOW_DAYS,
) -> List[Dict]:
    """Return one row per (strategy_id, sentiment_bucket) with stats."""
    buckets: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for o in outcomes:
        entry_d = _parse_iso_date(o.get("entry_ts"))
        if entry_d is None:
            continue
        sid = o["strategy_id"]
        sym = o["symbol"]
        b = bucket_for_trade(
            sym, entry_d, news_by_sym.get(sym, []),
            window_days=window_days,
        )
        buckets[(sid, b)].append(o["return_pct"])

    bucket_order = {b: i for i, b in enumerate(BUCKETS)}
    out: List[Dict] = []
    for (sid, b), rets in buckets.items():
        stats = _safe_stats(rets)
        out.append({"strategy_id": sid, "sentiment": b, **stats})
    out.sort(key=lambda r: (r["strategy_id"], bucket_order.get(r["sentiment"], 99)))
    return out


def compute_overlay(
    conn: sqlite3.Connection, *, window_days: int = WINDOW_DAYS,
) -> Dict:
    """Full rollup for the dashboard endpoint + CLI."""
    outcomes = fetch_closed_outcomes(conn)
    news_by_sym = fetch_news_by_symbol(conn)
    rows = slice_outcomes_by_sentiment(
        outcomes, news_by_sym, window_days=window_days,
    )
    return {
        "rows": rows,
        "n_trades_total": len(outcomes),
        "n_news_total": sum(len(v) for v in news_by_sym.values()),
        "window_days": window_days,
        "buckets": list(BUCKETS),
        "news_unavailable": not bool(news_by_sym),
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }
