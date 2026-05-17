"""
trailing_stops.py — Three-formula trailing-stop engine (milestone 4.6.1).

Per-strategy `trailing_stop.method` setting selects one of three formulas:

  atr_trail     — stop = highest_high_since_entry − (multiplier × ATR_14)
  chandelier    — stop = highest_high_over_N_days − (multiplier × ATR_22)
                  (Chandelier exit; uses a fixed-N lookback regardless
                  of when the position was opened)
  percent_trail — stop = highest_high_since_entry × (1 − pct)

Ratchet semantics: for a LONG, the stop only moves UP — once raised,
it never loosens. Symmetric for SHORTs (stop only moves DOWN). Crossing
back below a flat bar (no new HH) leaves the stop unchanged.

State lives in `trailing_stops(strategy_id, symbol, side, method,
stop_price, extreme_price, updated_at)` — see data/db.py SCHEMA_VERSION
3. The row is initialised on entry from the position's entry_price +
the strategy's chosen method, then advanced on every bar close.

The auto-trader's `_process_exit` consults `should_exit_on_trailing_stop`
BEFORE evaluating the strategy's long_exit signal — a trailing-stop
trigger always wins.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from monitoring.stops import compute_atr, compute_atr_wilder

METHODS = ("atr_trail", "chandelier", "percent_trail")
DEFAULT_METHOD = "atr_trail"

DEFAULT_ATR_PERIOD_TRAIL = 14
DEFAULT_ATR_PERIOD_CHANDELIER = 22
DEFAULT_CHANDELIER_LOOKBACK = 22
DEFAULT_ATR_MULTIPLIER = 3.0
DEFAULT_PCT_TRAIL = 0.10  # 10%


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Pure formula math
# ---------------------------------------------------------------------------

def _highest_high(bars: List[Dict], *, since_index: int = 0) -> float:
    """Highest high across bars[since_index:]. Caller guarantees bars is
    non-empty in the slice — we don't synthesize a value for empty sets."""
    return max(float(b["high"]) for b in bars[since_index:])


def _lowest_low(bars: List[Dict], *, since_index: int = 0) -> float:
    return min(float(b["low"]) for b in bars[since_index:])


def compute_atr_trail_stop(
    bars_since_entry: List[Dict],
    *,
    entry_price: float,
    multiplier: float = DEFAULT_ATR_MULTIPLIER,
    period: int = DEFAULT_ATR_PERIOD_TRAIL,
    side: str = "long",
) -> Optional[Dict]:
    """LONG: stop = HH(since_entry) − multiplier × ATR_period.

    Requires at least `period + 1` bars to compute ATR. Returns None on
    insufficient data, with an empty extreme price. Caller is expected
    to retain the prior stop in that case (ratchet semantics).

    Return shape: {stop_price, extreme_price} or None.
    """
    if not bars_since_entry:
        return None
    atr = compute_atr(bars_since_entry, period=period)
    if atr is None or atr <= 0:
        return None
    if side == "long":
        extreme = _highest_high(bars_since_entry)
        stop = extreme - multiplier * atr
    else:  # short
        extreme = _lowest_low(bars_since_entry)
        stop = extreme + multiplier * atr
    return {"stop_price": round(stop, 4),
            "extreme_price": round(extreme, 4)}


def compute_chandelier_stop(
    bars: List[Dict],
    *,
    lookback: int = DEFAULT_CHANDELIER_LOOKBACK,
    multiplier: float = DEFAULT_ATR_MULTIPLIER,
    period: int = DEFAULT_ATR_PERIOD_CHANDELIER,
    side: str = "long",
) -> Optional[Dict]:
    """Chandelier exit. LONG: stop = HH(last `lookback` bars) − M × ATR_period.

    Note this uses a FIXED LOOKBACK window — independent of when the
    position opened. That's the classical Chandelier definition.
    """
    if len(bars) < max(lookback, period + 1):
        return None
    atr = compute_atr(bars, period=period)
    if atr is None or atr <= 0:
        return None
    window = bars[-lookback:]
    if side == "long":
        extreme = _highest_high(window)
        stop = extreme - multiplier * atr
    else:
        extreme = _lowest_low(window)
        stop = extreme + multiplier * atr
    return {"stop_price": round(stop, 4),
            "extreme_price": round(extreme, 4)}


def compute_percent_trail_stop(
    bars_since_entry: List[Dict],
    *,
    pct: float = DEFAULT_PCT_TRAIL,
    side: str = "long",
) -> Optional[Dict]:
    """LONG: stop = HH(since_entry) × (1 − pct). Pure — no ATR needed."""
    if not bars_since_entry or pct <= 0 or pct >= 1:
        return None
    if side == "long":
        extreme = _highest_high(bars_since_entry)
        stop = extreme * (1.0 - pct)
    else:
        extreme = _lowest_low(bars_since_entry)
        stop = extreme * (1.0 + pct)
    return {"stop_price": round(stop, 4),
            "extreme_price": round(extreme, 4)}


def compute_stop(
    method: str,
    bars: List[Dict],
    *,
    entry_price: float,
    side: str = "long",
    multiplier: float = DEFAULT_ATR_MULTIPLIER,
    atr_period: Optional[int] = None,
    chandelier_lookback: int = DEFAULT_CHANDELIER_LOOKBACK,
    pct: float = DEFAULT_PCT_TRAIL,
) -> Optional[Dict]:
    """Dispatch on `method`. `bars` MUST be entry-to-now for atr_trail
    and percent_trail; whole-history for chandelier."""
    method = (method or DEFAULT_METHOD).lower()
    if method == "atr_trail":
        return compute_atr_trail_stop(
            bars, entry_price=entry_price, multiplier=multiplier,
            period=atr_period or DEFAULT_ATR_PERIOD_TRAIL, side=side,
        )
    if method == "chandelier":
        return compute_chandelier_stop(
            bars, lookback=chandelier_lookback, multiplier=multiplier,
            period=atr_period or DEFAULT_ATR_PERIOD_CHANDELIER, side=side,
        )
    if method == "percent_trail":
        return compute_percent_trail_stop(bars, pct=pct, side=side)
    raise ValueError(f"unknown trailing-stop method: {method!r}")


# ---------------------------------------------------------------------------
# Ratchet
# ---------------------------------------------------------------------------

def ratchet(existing: Optional[float], proposed: float,
             *, side: str = "long") -> float:
    """LONG: stop only moves UP. SHORT: stop only moves DOWN. Returns
    the chosen stop level."""
    if existing is None:
        return proposed
    if side == "long":
        return max(existing, proposed)
    return min(existing, proposed)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def get_stop(
    conn: sqlite3.Connection, *, strategy_id: str, symbol: str,
) -> Optional[Dict]:
    row = conn.execute(
        "SELECT method, stop_price, extreme_price, side, updated_at "
        "  FROM trailing_stops "
        " WHERE strategy_id=? AND symbol=?",
        (strategy_id, symbol),
    ).fetchone()
    if not row:
        return None
    return {
        "method": row["method"],
        "stop_price": float(row["stop_price"]),
        "extreme_price": float(row["extreme_price"]),
        "side": row["side"],
        "updated_at": row["updated_at"],
    }


def upsert_stop(
    conn: sqlite3.Connection,
    *,
    strategy_id: str, symbol: str,
    method: str, stop_price: float, extreme_price: float,
    side: str = "long",
    now_iso: Optional[str] = None,
) -> Dict:
    now = now_iso or _utc_now_iso()
    with conn:
        conn.execute(
            "INSERT INTO trailing_stops"
            " (strategy_id, symbol, side, method, stop_price, "
            "  extreme_price, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(strategy_id, symbol) DO UPDATE SET "
            "  side=excluded.side, method=excluded.method, "
            "  stop_price=excluded.stop_price, "
            "  extreme_price=excluded.extreme_price, "
            "  updated_at=excluded.updated_at",
            (strategy_id, symbol, side, method,
             float(stop_price), float(extreme_price), now),
        )
    return {"strategy_id": strategy_id, "symbol": symbol, "side": side,
            "method": method, "stop_price": float(stop_price),
            "extreme_price": float(extreme_price), "updated_at": now}


def clear_stop(
    conn: sqlite3.Connection, *, strategy_id: str, symbol: str,
) -> bool:
    """Remove the trailing_stops row (e.g. when position closes).
    Returns True iff a row was deleted."""
    with conn:
        cur = conn.execute(
            "DELETE FROM trailing_stops WHERE strategy_id=? AND symbol=?",
            (strategy_id, symbol),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Bar-close advancement (the entry point auto_trader calls per bar)
# ---------------------------------------------------------------------------

def advance_stop(
    conn: sqlite3.Connection,
    *,
    strategy_id: str, symbol: str, entry_price: float,
    bars: List[Dict],
    method: str = DEFAULT_METHOD,
    side: str = "long",
    multiplier: float = DEFAULT_ATR_MULTIPLIER,
    pct: float = DEFAULT_PCT_TRAIL,
    chandelier_lookback: int = DEFAULT_CHANDELIER_LOOKBACK,
    atr_period: Optional[int] = None,
    now_iso: Optional[str] = None,
) -> Optional[Dict]:
    """Recompute the stop given the latest bar set, ratchet vs the
    existing stop, persist, and return the new state. Returns None
    when there isn't enough data (the existing stop is left untouched).
    """
    proposed = compute_stop(
        method, bars, entry_price=entry_price, side=side,
        multiplier=multiplier, pct=pct,
        chandelier_lookback=chandelier_lookback,
        atr_period=atr_period,
    )
    if proposed is None:
        return None
    existing = get_stop(conn, strategy_id=strategy_id, symbol=symbol)
    if existing is None:
        # First update — straight insert. Initial stop respects 2.3.4's
        # entry-time floor when the caller wires it in.
        return upsert_stop(
            conn, strategy_id=strategy_id, symbol=symbol,
            method=method, stop_price=proposed["stop_price"],
            extreme_price=proposed["extreme_price"],
            side=side, now_iso=now_iso,
        )
    new_stop = ratchet(existing["stop_price"], proposed["stop_price"],
                        side=side)
    # Extreme always rolls to the higher (long) / lower (short) — even
    # when stop ratchet declines to move (it never does for HH=existing,
    # but stays sane).
    if side == "long":
        new_extreme = max(existing["extreme_price"], proposed["extreme_price"])
    else:
        new_extreme = min(existing["extreme_price"], proposed["extreme_price"])
    return upsert_stop(
        conn, strategy_id=strategy_id, symbol=symbol,
        method=existing["method"],  # method is locked at entry
        stop_price=new_stop, extreme_price=new_extreme,
        side=existing["side"], now_iso=now_iso,
    )


# ---------------------------------------------------------------------------
# Auto-trader integration
# ---------------------------------------------------------------------------

def should_exit_on_trailing_stop(
    conn: sqlite3.Connection,
    *,
    strategy_id: str, symbol: str,
    current_price: float,
) -> bool:
    """True iff a trailing stop is in force AND the current price has
    crossed it. The auto-trader's exit-eligibility check calls this
    BEFORE evaluating the strategy's long_exit signal."""
    row = get_stop(conn, strategy_id=strategy_id, symbol=symbol)
    if row is None:
        return False
    side = row.get("side") or "long"
    stop = row["stop_price"]
    if side == "long":
        return current_price <= stop
    return current_price >= stop
