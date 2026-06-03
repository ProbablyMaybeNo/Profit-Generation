"""
stops.py — ATR-based stop-loss support for the auto-trader.

Two responsibilities:

  1. compute_atr(bars, period=20) — Wilder-style Average True Range
     over the supplied daily bars. True Range per bar is:
         max(high - low, |high - prev_close|, |low - prev_close|)
     ATR is the simple mean of TR over the trailing `period` rows;
     a Wilder-smoothed flavor is also exposed for callers who care.

  2. submit_atr_stop(...) — wraps Alpaca's StopOrderRequest to place
     a STOP SELL at `entry_price - multiple * atr` (rounded to 4dp).
     If atr is None or the stop would land at or above entry, returns
     None so the caller knows to fall back to "no stop attached".

  3. reconcile_stop_fills(conn, client) — periodic job entry-point.
     Walks open paper_trades rows that have a stop_price set, asks
     the trading client for each stop order's status, and on `filled`
     status calls db.close_outcome to mark the position closed in the
     outcomes table. Idempotent — re-running is safe.

Tests inject bars / clients directly, so neither yfinance nor alpaca
need to be reachable in CI.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

from data import db


DEFAULT_ATR_PERIOD = 20


def _coerce_multiple(raw) -> float:
    """settings.stop_loss_atr_multiple → positive float, else 0.0."""
    try:
        v = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, v)


def compute_atr(bars, period: int = DEFAULT_ATR_PERIOD) -> Optional[float]:
    """ATR over the last `period` bars (simple mean of true range).

    Accepts:
      - a pandas DataFrame with columns high/low/close (case-insensitive)
      - a list of dicts with same keys
      - None / empty → returns None

    Returns None if there aren't enough bars (< period + 1).
    """
    rows = _rows_from(bars)
    if not rows or len(rows) < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(rows)):
        prev_close = rows[i - 1]["close"]
        high = rows[i]["high"]
        low = rows[i]["low"]
        tr = max(high - low,
                 abs(high - prev_close),
                 abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    window = trs[-period:]
    return round(sum(window) / period, 4)


def compute_atr_wilder(bars, period: int = DEFAULT_ATR_PERIOD) -> Optional[float]:
    """Wilder's smoothed ATR (RMA-style). Exposed for callers who
    prefer the classical formula; the auto-trader uses compute_atr."""
    rows = _rows_from(bars)
    if not rows or len(rows) < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(rows)):
        prev_close = rows[i - 1]["close"]
        high = rows[i]["high"]
        low = rows[i]["low"]
        tr = max(high - low,
                 abs(high - prev_close),
                 abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    initial = sum(trs[:period]) / period
    atr = initial
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 4)


def _rows_from(bars) -> List[Dict]:
    """Normalize bars to a list of dicts keyed high / low / close."""
    if bars is None:
        return []
    # pandas-like
    if hasattr(bars, "iterrows"):
        cols = {c.lower(): c for c in bars.columns}
        if not all(k in cols for k in ("high", "low", "close")):
            return []
        out: List[Dict] = []
        for _, row in bars.iterrows():
            out.append({
                "high": float(row[cols["high"]]),
                "low": float(row[cols["low"]]),
                "close": float(row[cols["close"]]),
            })
        return out
    # list of dicts
    if isinstance(bars, list):
        out = []
        for b in bars:
            if not isinstance(b, dict):
                continue
            try:
                out.append({
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                })
            except (KeyError, TypeError, ValueError):
                return []
        return out
    return []


def quantize_stop_price(price: Optional[float]) -> Optional[float]:
    """Round a stop/limit price to a broker-valid tick.

    US equities priced >= $1.00 must trade in 1-cent (2dp) increments;
    sub-penny ticks (e.g. 741.9597) are rejected by Alpaca with
    code 42210000 "sub-penny increment". Names priced < $1.00 may use
    finer 4dp ticks. Returns None unchanged so callers can keep their
    "no stop" sentinel.
    """
    if price is None:
        return None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    decimals = 4 if p < 1.0 else 2
    return round(p, decimals)


def stop_price_for(entry_price: float, atr: Optional[float],
                    multiple: float) -> Optional[float]:
    """Return the stop level, or None if the inputs make it nonsensical."""
    if entry_price is None or atr is None or atr <= 0 or multiple <= 0:
        return None
    stop = entry_price - multiple * atr
    # Stop must be strictly below entry — otherwise the position is
    # already in stop-out territory and we'd flip-flop.
    if stop >= entry_price or stop <= 0:
        return None
    return round(stop, 4)


def submit_atr_stop(
    client, *, symbol: str, qty: int, stop_price: float,
    client_order_id: Optional[str] = None,
):
    """Submit a SELL STOP to close a long. Returns the order object."""
    from alpaca.trading.requests import StopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    kwargs = dict(
        symbol=symbol, qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=quantize_stop_price(stop_price),
    )
    if client_order_id:
        kwargs["client_order_id"] = client_order_id
    return client.submit_order(StopOrderRequest(**kwargs))


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _open_stop_trades(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """All stop-order rows that we haven't yet seen filled."""
    return conn.execute(
        "SELECT * FROM paper_trades "
        " WHERE stop_price IS NOT NULL "
        "   AND order_type LIKE '%stop%' "
        "   AND status NOT IN ('filled', 'canceled', 'rejected', 'expired')"
    ).fetchall()


def _outcome_signal_id_for(conn: sqlite3.Connection,
                            strategy_id: str, symbol: str) -> Optional[int]:
    row = conn.execute(
        "SELECT o.signal_id "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='open' AND s.strategy_id=? AND s.symbol=? "
        " ORDER BY o.entry_ts DESC LIMIT 1",
        (strategy_id, symbol),
    ).fetchone()
    return int(row["signal_id"]) if row else None


def reconcile_stop_fills(
    conn: sqlite3.Connection,
    client,
    *,
    now_iso: Optional[str] = None,
) -> Dict:
    """For every pending stop in paper_trades, ask Alpaca for the order's
    current status. If `filled`, update the paper_trades row + close the
    matching outcome with exit_reason='stop_loss_atr'.

    Returns {checked, filled, closed}.
    """
    stops = _open_stop_trades(conn)
    n_filled = 0
    n_closed = 0
    for s in stops:
        order_id = s["alpaca_order_id"]
        if not order_id:
            continue
        try:
            order = client.get_order_by_id(order_id)
        except Exception as e:
            from config.utils import log
            log(f"reconcile_stop_fills: get_order_by_id({order_id}) failed: {e}",
                "WARNING")
            continue
        status = str(getattr(order, "status", "")).lower()
        if status != "filled":
            continue
        fill_price = float(getattr(order, "filled_avg_price", 0) or 0)
        filled_at = str(getattr(order, "filled_at", now_iso
                                  or datetime.now(timezone.utc).isoformat()))
        db.record_paper_trade(conn, {
            "alpaca_order_id": order_id,
            "strategy_id": s["strategy_id"],
            "symbol": s["symbol"],
            "side": "sell",
            "qty": s["qty"],
            "order_type": s["order_type"],
            "limit_price": s["limit_price"],
            "stop_price": s["stop_price"],
            "filled_at": filled_at,
            "fill_price": fill_price,
            "status": "filled",
            "notes": (s["notes"] or "") + "; reconciled stop fill",
        })
        n_filled += 1
        signal_id = _outcome_signal_id_for(
            conn, s["strategy_id"], s["symbol"],
        )
        if signal_id is None:
            continue
        db.close_outcome(
            conn, signal_id=signal_id,
            exit_ts=filled_at, exit_price=fill_price,
            exit_reason="stop_loss_atr",
        )
        n_closed += 1
    return {"checked": len(stops), "filled": n_filled, "closed": n_closed}
