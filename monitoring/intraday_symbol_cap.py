"""
intraday_symbol_cap.py — Per-symbol same-day round-trip cap for
intraday entries (5.5.2).

A single intraday strategy can fire repeatedly on the same symbol if
the underlying mean-reverts back into its trigger range several times
in a day. Without a cap, a choppy session could rack up dozens of
round trips on one symbol — death by 100 papercuts (slippage + commission
death spiral). The cap blocks further intraday entries on a symbol
after N completed round trips that day.

Today's per-symbol round-trip count is the min(buys, sells) for that
symbol filled today across all intraday-tagged paper_trades. The cap
applies ONLY to intraday entries — EOD signals are exempt because they
don't generate same-day round trips by definition.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional


DEFAULT_MAX_INTRADAY_ROUND_TRIPS_PER_SYMBOL = 2


def _coerce_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        return date.fromisoformat(d[:10])
    raise TypeError(f"unsupported date type: {type(d)!r}")


def intraday_round_trips_today(conn, symbol: str, *, asof) -> int:
    """Count completed round trips for `symbol` on the `asof` day across
    intraday paper_trades.

    "Intraday" = any paper_trades row whose signal_id maps back to a
    signals row with bar_interval != '1d'. EOD trades on the same symbol
    are excluded so they don't dilute the count.

    A round trip = min(buy_count, sell_count) per symbol per day.
    """
    d = _coerce_date(asof)
    rows = conn.execute(
        """
        SELECT pt.side
          FROM paper_trades pt
          LEFT JOIN signals s ON s.id = pt.signal_id
         WHERE pt.symbol = ?
           AND pt.filled_at IS NOT NULL
           AND substr(pt.filled_at, 1, 10) = ?
           AND COALESCE(s.bar_interval, '1d') != '1d'
        """,
        (symbol, d.isoformat()),
    ).fetchall()
    buys = sum(1 for r in rows if (r["side"] or "").lower() == "buy")
    sells = sum(1 for r in rows if (r["side"] or "").lower() == "sell")
    return min(buys, sells)


def check_intraday_symbol_cap(
    conn,
    *,
    symbol: str,
    asof,
    cap: int = DEFAULT_MAX_INTRADAY_ROUND_TRIPS_PER_SYMBOL,
) -> Optional[dict]:
    """Return None when the entry is allowed; a SKIP payload when the
    cap has been reached.

    cap <= 0 disables the guard (returns None unconditionally).
    """
    if cap is None or cap <= 0:
        return None
    n = intraday_round_trips_today(conn, symbol, asof=asof)
    if n < cap:
        return None
    return {
        "reason": "intraday_symbol_cap",
        "round_trips_today": n,
        "cap": int(cap),
        "symbol": symbol,
    }
