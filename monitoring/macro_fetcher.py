"""
macro_fetcher.py — Daily pull of FRED macro series into trading.db.macro.

Three series ship by default:
  VIXCLS  — CBOE Volatility Index close (the slicer's quartile source)
  T10Y2Y  — 10y-2y Treasury spread (recession indicator)
  DTWEXBGS — Trade-weighted broad dollar index (FRED's free DXY analog)

The fetcher is conservative: failures (network, missing creds, NaN bars)
never raise — they log a warning and return zero rows persisted. Persistence
is idempotent on (series_id, bar_date), so re-running the fetch every day is
safe and cheap.
"""

from __future__ import annotations

import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import load_credentials, log  # noqa: E402
from data import db  # noqa: E402


DEFAULT_SERIES: Tuple[str, ...] = ("VIXCLS", "T10Y2Y", "DTWEXBGS")

DEFAULT_LOOKBACK_DAYS = 365

SERIES_LABELS: Dict[str, str] = {
    "VIXCLS":   "VIX",
    "T10Y2Y":   "T10Y2Y",
    "DTWEXBGS": "DXY",
}


def _get_fred():
    """Return an authenticated fredapi.Fred client, or raise.

    Indirection seam — tests monkeypatch this to avoid hitting FRED.
    """
    from fredapi import Fred  # local import: optional dep, conda 'trading' env
    creds = load_credentials("fred")
    api_key = creds.get("api_key")
    if not api_key or "PASTE_YOUR" in str(api_key):
        raise RuntimeError("fred api_key not configured in credentials.json")
    return Fred(api_key=api_key)


def _iter_series_points(
    series, observation_start: Optional[date] = None,
) -> Iterable[Tuple[str, float]]:
    """Yield (YYYY-MM-DD, value) for each non-NaN bar in a fredapi Series.

    Tolerates pandas Series, dicts, or any iterable of (key, value) — the
    real fredapi return is a pandas Series indexed by Timestamp, but the
    test path passes a plain dict so we don't drag pandas in.
    """
    if hasattr(series, "items"):
        items = series.items()
    else:
        items = iter(series)
    for key, value in items:
        if value is None:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if math.isnan(v):
            continue
        if hasattr(key, "strftime"):
            bar_date = key.strftime("%Y-%m-%d")
        else:
            bar_date = str(key)[:10]
        if observation_start is not None:
            try:
                d = date.fromisoformat(bar_date)
            except ValueError:
                continue
            if d < observation_start:
                continue
        yield bar_date, v


def fetch_series_points(
    series_id: str,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    fred=None,
) -> List[Tuple[str, float]]:
    """Pull one FRED series, return [(bar_date, value), ...]. Empty on failure."""
    try:
        client = fred if fred is not None else _get_fred()
    except Exception as e:
        log(f"macro fetch: cannot init FRED client ({e})", "WARNING")
        return []
    cutoff = date.today() - timedelta(days=lookback_days)
    client_side_cutoff: Optional[date] = None
    try:
        series = client.get_series(series_id, observation_start=cutoff.isoformat())
    except TypeError:
        try:
            series = client.get_series(series_id)
        except Exception as e:
            log(f"macro fetch: {series_id} failed ({e})", "WARNING")
            return []
        client_side_cutoff = cutoff
    except Exception as e:
        log(f"macro fetch: {series_id} failed ({e})", "WARNING")
        return []
    return list(_iter_series_points(series, observation_start=client_side_cutoff))


def persist_series_points(
    series_id: str, points: Iterable[Tuple[str, float]],
) -> int:
    """Upsert all (bar_date, value) into macro. Returns count newly changed."""
    points = list(points)
    if not points:
        return 0
    conn = db.init_db()
    inserted = 0
    try:
        for bar_date, value in points:
            inserted += db.upsert_macro_value(
                conn, series_id=series_id, bar_date=bar_date, value=value,
            ) or 0
    finally:
        conn.close()
    return inserted


def fetch_and_persist(
    series_ids: Iterable[str] = DEFAULT_SERIES,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    fred=None,
) -> Dict[str, int]:
    """Pull each series and persist. Returns {series_id: rows_changed}."""
    out: Dict[str, int] = {}
    for sid in series_ids:
        points = fetch_series_points(sid, lookback_days=lookback_days, fred=fred)
        out[sid] = persist_series_points(sid, points)
    return out


def latest_snapshot(series_ids: Iterable[str] = DEFAULT_SERIES) -> List[Dict]:
    """Return the latest stored value for each series — what the dashboard renders."""
    conn = db.init_db()
    try:
        out: List[Dict] = []
        for sid in series_ids:
            row = db.latest_macro_value(conn, sid)
            if row is None:
                out.append({
                    "series_id": sid,
                    "label": SERIES_LABELS.get(sid, sid),
                    "value": None,
                    "bar_date": None,
                    "available": False,
                })
                continue
            out.append({
                "series_id": sid,
                "label": SERIES_LABELS.get(sid, sid),
                "value": float(row["value"]),
                "bar_date": row["bar_date"],
                "available": True,
            })
        return out
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("series", nargs="*",
                        help="FRED series IDs (default: VIXCLS T10Y2Y DTWEXBGS)")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = parser.parse_args()
    series_ids = tuple(args.series) if args.series else DEFAULT_SERIES
    log(f"Fetching {len(series_ids)} FRED series (lookback={args.lookback_days}d)...",
        "INFO")
    result = fetch_and_persist(series_ids, lookback_days=args.lookback_days)
    for sid, n in result.items():
        log(f"  {sid:<10}  rows_changed={n}", "INFO")
    snapshot = latest_snapshot(series_ids)
    log("Latest snapshot:", "INFO")
    for r in snapshot:
        if r["available"]:
            log(f"  {r['label']:<8} {r['value']:.3f} (as of {r['bar_date']})", "INFO")
        else:
            log(f"  {r['label']:<8} (no data)", "WARNING")
