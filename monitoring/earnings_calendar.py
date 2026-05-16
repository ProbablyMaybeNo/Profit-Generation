"""
earnings_calendar.py — Fetch upcoming earnings dates per symbol via
yfinance and persist them to trading.db.earnings.

yfinance's `Ticker.calendar` returns a dict shaped roughly like:
  {"Earnings Date": [date(2026, 6, 12), date(2026, 6, 13)],
   "Earnings Average": ..., ...}

We harvest only the "Earnings Date" entries — a one- or two-element list
of date / datetime / pandas.Timestamp / ISO-string items.

The fetcher is conservative: any failure (network, missing yfinance,
empty calendar, bad payload) silently returns 0 rows persisted. The
veto path in auto_trader.py degrades to "no veto" when the table is
empty — strategies still trade, they just don't get the earnings
protection until the next successful fetch.
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


DEFAULT_HORIZON_DAYS = 90


def _get_ticker(symbol: str):
    """Indirection seam — tests monkeypatch this to bypass yfinance."""
    import yfinance as yf
    return yf.Ticker(symbol)


def _coerce_date(value) -> Optional[date]:
    """Convert whatever yfinance hands us into a date (or None)."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except Exception:
            pass
    s = str(value)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _extract_dates_from_calendar(calendar) -> List[date]:
    """Pull the "Earnings Date" list out of a yfinance calendar payload."""
    if calendar is None:
        return []
    # Dict shape (recent yfinance).
    raw = None
    if isinstance(calendar, dict):
        for key in ("Earnings Date", "earnings_date", "earningsDate"):
            if key in calendar:
                raw = calendar[key]
                break
    else:
        try:
            raw = calendar.loc["Earnings Date"].values.tolist()
        except Exception:
            raw = None
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out: List[date] = []
    for item in raw:
        d = _coerce_date(item)
        if d is not None:
            out.append(d)
    return out


def fetch_next_earnings(
    symbol: str, *, get_ticker: Optional[Callable] = None,
) -> List[date]:
    """Return upcoming earnings dates for one symbol (empty list on failure)."""
    factory = get_ticker or _get_ticker
    try:
        ticker = factory(symbol)
    except Exception as e:
        log(f"earnings fetch: cannot init ticker for {symbol} ({e})", "WARNING")
        return []
    try:
        calendar = ticker.calendar
    except Exception as e:
        log(f"earnings fetch: {symbol} calendar lookup failed ({e})",
            "WARNING")
        return []
    dates = _extract_dates_from_calendar(calendar)
    today = date.today()
    return [d for d in dates if d >= today]


def persist_earnings_dates(
    symbol: str, dates: Iterable[date], *, source: str = "yfinance",
) -> int:
    """Persist upcoming earnings dates. Returns count newly inserted."""
    dates = list(dates)
    if not dates:
        return 0
    conn = db.init_db()
    inserted = 0
    try:
        for d in dates:
            iso = d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]
            inserted += db.upsert_earnings_date(
                conn, symbol=symbol, earnings_date=iso, source=source,
            ) or 0
    finally:
        conn.close()
    return inserted


def fetch_and_persist_for_universe(
    symbols: Iterable[str],
    *,
    get_ticker: Optional[Callable] = None,
) -> Dict[str, int]:
    """Fetch + persist upcoming earnings for each symbol.
    Returns {symbol: newly_inserted_count}."""
    out: Dict[str, int] = {}
    for sym in dict.fromkeys(symbols):
        dates = fetch_next_earnings(sym, get_ticker=get_ticker)
        out[sym] = persist_earnings_dates(sym, dates)
    return out


def is_within_earnings_window(
    conn,
    symbol: str,
    *,
    asof: Optional[date] = None,
    window_trading_days: int = 2,
) -> Optional[dict]:
    """Return a dict if `symbol` has an earnings event within the next
    `window_trading_days` trading days from `asof`, otherwise None.

    Trading-day counting reuses snapshots.snapshot_date (same logic as
    auto_trader._trading_days_between) so weekends + holidays are
    skipped naturally. The veto fires for events on the asof date too —
    "within 2 trading days" includes same-day.
    """
    if window_trading_days <= 0:
        return None
    asof_d = asof or date.today()
    next_iso = db.next_earnings_date_on_or_after(
        conn, symbol, asof_d.isoformat(),
    )
    if not next_iso:
        return None
    from monitoring import auto_trader as at
    days_between = at._trading_days_between(
        conn, asof_d.isoformat(), next_iso,
    )
    if next_iso == asof_d.isoformat():
        days_between = 0
    if days_between > window_trading_days:
        return None
    return {
        "symbol": symbol,
        "earnings_date": next_iso,
        "trading_days_until": days_between,
        "window_trading_days": window_trading_days,
        "reason": (
            f"earnings on {next_iso} ({days_between} trading day(s) away, "
            f"within {window_trading_days}-day veto window)"
        ),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="*",
                        help="Symbols to fetch (default: tracked universe)")
    args = parser.parse_args()
    if args.symbols:
        syms = args.symbols
    else:
        from monitoring.config import TRACKED_STOCKS, TRACKED_SECTORS
        syms = TRACKED_STOCKS + TRACKED_SECTORS
    log(f"Fetching earnings for {len(syms)} symbols...", "INFO")
    result = fetch_and_persist_for_universe(syms)
    total = sum(result.values())
    for sym, n in result.items():
        log(f"  {sym:<8}  newly_inserted={n}", "INFO")
    log(f"Done. Total inserted={total}", "SUCCESS")
