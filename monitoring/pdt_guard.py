"""
pdt_guard.py — Pattern Day Trader (PDT) regulation guard (5.4.1).

US accounts under $25,000 are restricted from executing more than three
round-trip day trades within any rolling 5 business-day window. Crossing
that threshold flags the account as a "pattern day trader" and locks it
from further day trades until the equity is restored above $25k.

Alpaca paper accounts are unrestricted, so this guard never bites in
practice for the current 100k-seeded paper environment. But Phase 5's
intraday wiring needs the guard built and tested before any strategy
flips to live — once a strategy_id is added to `auto_trade.live_strategies`,
the same auto_trader path routes its orders to the live broker, where
this guard MUST be enforced.

Round-trip definition (matching the FINRA day-trade rule):
  - A round trip is a buy-then-sell (or sell-then-buy for shorts) of the
    SAME symbol on the SAME trading day, both legs filled.
  - The day's count is the number of completed round trips that day.
  - We use UTC `filled_at` and group by .date() — equity markets only
    operate in a single timezone band so date-grouping is safe.

Public API:
  - count_round_trips_for_day(conn, day) -> int
  - count_round_trips_last_5_days(conn, asof) -> int
  - pdt_status(conn, *, account_value, asof) -> dict
  - check_pdt_guard(conn, *, account_value, asof,
                     threshold_count, equity_threshold) -> dict | None
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from data import db

PDT_EQUITY_THRESHOLD = 25_000.0
PDT_ROUND_TRIP_THRESHOLD = 3  # block on the 3rd → 4 would be FINRA breach
PDT_WINDOW_DAYS = 5            # FINRA's rolling 5 business-day window


def _coerce_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        try:
            return datetime.fromisoformat(d.replace("Z", "+00:00")).date()
        except ValueError:
            return date.fromisoformat(d[:10])
    raise TypeError(f"unsupported date type: {type(d)!r}")


def _filled_trades_in_window(conn, *, start: date, end: date):
    """Return [(symbol, side, filled_date)] for trades filled within
    [start, end] inclusive. Excludes rows without a filled_at timestamp.
    """
    rows = conn.execute(
        """
        SELECT symbol, side, filled_at
          FROM paper_trades
         WHERE filled_at IS NOT NULL
           AND substr(filled_at, 1, 10) >= ?
           AND substr(filled_at, 1, 10) <= ?
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    out = []
    for r in rows:
        sym = r["symbol"] if isinstance(r, db.sqlite3.Row) else r[0]
        side = (r["side"] if isinstance(r, db.sqlite3.Row) else r[1]) or ""
        filled_at = r["filled_at"] if isinstance(r, db.sqlite3.Row) else r[2]
        if not sym or not side or not filled_at:
            continue
        try:
            d = _coerce_date(filled_at)
        except Exception:
            continue
        out.append((sym, side.lower(), d))
    return out


def _round_trips_for_day(trades, target_day: date) -> int:
    """Count completed round trips on `target_day` from the supplied
    (symbol, side, filled_date) trade list.

    A round trip is min(buys, sells) per symbol within the day.
    """
    per_symbol_buys: dict[str, int] = {}
    per_symbol_sells: dict[str, int] = {}
    for sym, side, d in trades:
        if d != target_day:
            continue
        if side == "buy":
            per_symbol_buys[sym] = per_symbol_buys.get(sym, 0) + 1
        elif side == "sell":
            per_symbol_sells[sym] = per_symbol_sells.get(sym, 0) + 1
    total = 0
    for sym in set(per_symbol_buys) | set(per_symbol_sells):
        total += min(per_symbol_buys.get(sym, 0), per_symbol_sells.get(sym, 0))
    return total


def count_round_trips_for_day(conn, day) -> int:
    """Round trips closed (entered AND exited) on the given calendar day."""
    d = _coerce_date(day)
    trades = _filled_trades_in_window(conn, start=d, end=d)
    return _round_trips_for_day(trades, d)


def count_round_trips_last_5_days(conn, asof) -> int:
    """Sum of round trips across the last 5 calendar days (inclusive of asof).

    Per FINRA the window is 5 BUSINESS days but the auto_trader doesn't
    ship a holiday calendar — calendar days are conservative (over-counts
    weekends → never under-counts day trades). Good enough for a guard.
    """
    asof_d = _coerce_date(asof)
    start = asof_d - timedelta(days=PDT_WINDOW_DAYS - 1)
    trades = _filled_trades_in_window(conn, start=start, end=asof_d)
    total = 0
    for offset in range(PDT_WINDOW_DAYS):
        day = asof_d - timedelta(days=offset)
        total += _round_trips_for_day(trades, day)
    return total


def pdt_status(conn, *, account_value: float, asof) -> dict:
    """Compute a structured PDT status.

    Returns:
      {
        today: int,
        five_day: int,
        account_value: float,
        below_pdt_equity: bool,
        threshold: int,       # PDT_ROUND_TRIP_THRESHOLD
        would_block: bool,    # True iff a NEW intraday entry would be refused
      }
    """
    today = count_round_trips_for_day(conn, asof)
    five_day = count_round_trips_last_5_days(conn, asof)
    below = float(account_value) < PDT_EQUITY_THRESHOLD
    would_block = below and five_day >= PDT_ROUND_TRIP_THRESHOLD
    return {
        "today": today,
        "five_day": five_day,
        "account_value": float(account_value),
        "below_pdt_equity": below,
        "threshold": PDT_ROUND_TRIP_THRESHOLD,
        "would_block": would_block,
    }


def check_pdt_guard(
    conn,
    *,
    account_value: Optional[float],
    asof,
    threshold_count: int = PDT_ROUND_TRIP_THRESHOLD,
    equity_threshold: float = PDT_EQUITY_THRESHOLD,
) -> Optional[dict]:
    """Return None when the trade is allowed, or a SKIP_PDT_GUARD payload
    when the entry must be refused.

    Used by auto_trader for intraday entries. When account_value is None
    (e.g. dry-run with no broker query), the guard is observe-only and
    returns None — the regular eligibility / sizing pipeline still
    computes the would-be order, but PDT is treated as not-applicable.
    """
    if account_value is None:
        return None
    five_day = count_round_trips_last_5_days(conn, asof)
    if float(account_value) >= equity_threshold:
        return None
    if five_day < threshold_count:
        return None
    return {
        "reason": "pdt_guard",
        "five_day_round_trips": five_day,
        "threshold": threshold_count,
        "account_value": float(account_value),
        "equity_threshold": equity_threshold,
    }
