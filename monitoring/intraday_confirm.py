"""
intraday_confirm.py — 7.5.4 Intraday confirmation overlay (shadow mode).

Mirror of monitoring/sar_overlay.py's shadow-A/B pattern. Strategies that
opt in via ``intraday_confirm: "shadow"`` get a parallel record of what
their entries would have looked like if a 1m bar close above the trigger
price had been required before order submission. The real entry path is
untouched — this is observability only, for 30 days of A/B data before
the confirmation gate is considered for graduation.

Architecture:
  - parallel ``paper_trades_intraday_confirm`` table, never touches paper_trades
  - opt-in per strategy declaration (``intraday_confirm: "shadow"``)
  - graceful degrade when intraday_bars are missing for the day:
    record_intraday_confirm writes a row with ``shadow_status='no_data'``
    and ``would_have_confirmed_at=None``.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


INTRADAY_CONFIRM_MODE_SHADOW = "shadow"
INTRADAY_CONFIRM_MODE_LIVE = "live"

SHADOW_STATUS_CONFIRMED = "confirmed"
SHADOW_STATUS_NOT_CONFIRMED = "not_confirmed"
SHADOW_STATUS_NO_DATA = "no_data"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Opt-in predicates
# ---------------------------------------------------------------------------

def strategy_has_intraday_confirm_shadow(
    strategy_meta: Optional[Dict],
) -> bool:
    """True iff the strategy opts in to intraday confirmation shadow recording.

    Returns True for ``intraday_confirm: "shadow"`` AND for any live opt-in
    (because shadow rows remain useful as audit trail even when the
    confirmation gate is live).
    """
    if not isinstance(strategy_meta, dict):
        return False
    value = strategy_meta.get("intraday_confirm", False)
    if isinstance(value, str) and value.lower() == INTRADAY_CONFIRM_MODE_SHADOW:
        return True
    return bool(value)


def strategy_has_intraday_confirm_live(
    strategy_meta: Optional[Dict],
) -> bool:
    """True iff the strategy opts in to LIVE intraday confirmation.

    A value of ``"shadow"`` is observe-only and returns False. All other
    truthy values (True, "live", "yes", 1) are treated as live.
    """
    if not isinstance(strategy_meta, dict):
        return False
    value = strategy_meta.get("intraday_confirm", False)
    if isinstance(value, str) and value.lower() == INTRADAY_CONFIRM_MODE_SHADOW:
        return False
    return bool(value)


# ---------------------------------------------------------------------------
# Confirmation math
# ---------------------------------------------------------------------------

def compute_confirmation(
    bars: Sequence[Dict[str, Any]],
    *,
    trigger_price: float,
    side: str = "long",
) -> Dict[str, Any]:
    """Walk a sequence of 1m bars and return the first one that confirms.

    For a long: confirmation = first bar where ``close > trigger_price``.
    For a short: confirmation = first bar where ``close < trigger_price``.

    Returns ``{status, confirmed_at, entry_price}``:
      - status: "confirmed" / "not_confirmed" / "no_data"
      - confirmed_at: the ts_utc of the confirming bar (or None)
      - entry_price: the close of the confirming bar (or None)

    Bars expected to have keys ``ts_utc`` and ``close`` (case-insensitive).
    Returns ``status="no_data"`` when ``bars`` is empty.
    """
    if not bars:
        return {
            "status": SHADOW_STATUS_NO_DATA,
            "confirmed_at": None,
            "entry_price": None,
        }
    side_norm = (side or "long").lower()
    try:
        trigger = float(trigger_price)
    except (TypeError, ValueError):
        return {
            "status": SHADOW_STATUS_NO_DATA,
            "confirmed_at": None,
            "entry_price": None,
        }
    for bar in bars:
        close = _get(bar, "close")
        ts = _get(bar, "ts_utc")
        if close is None:
            continue
        try:
            close_f = float(close)
        except (TypeError, ValueError):
            continue
        confirmed = (
            close_f > trigger if side_norm == "long" else close_f < trigger
        )
        if confirmed:
            return {
                "status": SHADOW_STATUS_CONFIRMED,
                "confirmed_at": ts,
                "entry_price": close_f,
            }
    return {
        "status": SHADOW_STATUS_NOT_CONFIRMED,
        "confirmed_at": None,
        "entry_price": None,
    }


def _get(d: Dict, key: str) -> Any:
    """Case-insensitive key lookup. Returns None when key absent."""
    if not isinstance(d, dict):
        return None
    if key in d:
        return d[key]
    key_lower = key.lower()
    for k, v in d.items():
        if isinstance(k, str) and k.lower() == key_lower:
            return v
    return None


# ---------------------------------------------------------------------------
# Bar fetch + persisted record
# ---------------------------------------------------------------------------

def fetch_intraday_bars(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    after_ts_utc: str,
    limit: int = 390,
) -> List[Dict[str, Any]]:
    """Read intraday_bars rows for ``symbol`` newer than ``after_ts_utc``.

    Returns a list of dicts in chronological order. Empty list when no
    rows match (the caller writes a ``no_data`` shadow row in that case).
    """
    try:
        rows = conn.execute(
            "SELECT symbol, ts_utc, open, high, low, close, volume, source "
            "  FROM intraday_bars "
            " WHERE symbol = ? AND ts_utc > ? "
            " ORDER BY ts_utc ASC LIMIT ?",
            (symbol, after_ts_utc, int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _ensure_shadow_table(conn: sqlite3.Connection) -> None:
    """Idempotent — creates paper_trades_intraday_confirm if absent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades_intraday_confirm (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at              TEXT NOT NULL,
            strategy_id              TEXT NOT NULL,
            symbol                   TEXT NOT NULL,
            signal_id                INTEGER,
            daily_signal_ts          TEXT NOT NULL,
            trigger_price            REAL,
            would_have_confirmed_at  TEXT,
            hypothetical_entry_price REAL,
            shadow_status            TEXT NOT NULL,
            real_entry_price         REAL,
            shadow_pnl_at_close      REAL,
            real_pnl_at_close        REAL,
            notes                    TEXT,
            UNIQUE(strategy_id, symbol, daily_signal_ts)
        )
    """)


def record_intraday_confirm(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    symbol: str,
    daily_signal_ts: str,
    trigger_price: Optional[float],
    signal_id: Optional[int] = None,
    side: str = "long",
    bars: Optional[Sequence[Dict[str, Any]]] = None,
    real_entry_price: Optional[float] = None,
    notes: Optional[str] = None,
    now_iso: Optional[str] = None,
) -> Optional[int]:
    """Insert a single shadow record. Idempotent on
    (strategy_id, symbol, daily_signal_ts) — re-insert is a no-op.

    Computes confirmation from ``bars`` (a sequence of 1m bar dicts) when
    supplied; otherwise records ``shadow_status='no_data'``. Returns the
    inserted row id, or None when the UNIQUE clause caused a no-op.
    """
    _ensure_shadow_table(conn)
    if bars is None or trigger_price is None:
        result = {
            "status": SHADOW_STATUS_NO_DATA,
            "confirmed_at": None,
            "entry_price": None,
        }
    else:
        result = compute_confirmation(
            list(bars), trigger_price=float(trigger_price), side=side,
        )
    with conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_trades_intraday_confirm
                (recorded_at, strategy_id, symbol, signal_id,
                 daily_signal_ts, trigger_price,
                 would_have_confirmed_at, hypothetical_entry_price,
                 shadow_status, real_entry_price,
                 shadow_pnl_at_close, real_pnl_at_close, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso or _utc_now_iso(),
                strategy_id, symbol, signal_id,
                daily_signal_ts,
                None if trigger_price is None else float(trigger_price),
                result["confirmed_at"],
                result["entry_price"],
                result["status"],
                real_entry_price,
                None,  # shadow_pnl_at_close populated by EOD report
                None,  # real_pnl_at_close populated by EOD report
                notes,
            ),
        )
        return cur.lastrowid if cur.rowcount else None


# ---------------------------------------------------------------------------
# A/B aggregation
# ---------------------------------------------------------------------------

def aggregate_ab(
    conn: sqlite3.Connection,
    *,
    strategy_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return aggregate confirmation stats from paper_trades_intraday_confirm.

    When ``strategy_id`` is None, returns global aggregates + a
    per-strategy breakdown. When set, returns just that strategy's row.

    Per-scope keys:
      - count: total shadow rows
      - confirmed / not_confirmed / no_data: count by shadow_status
      - confirmation_rate: confirmed / (confirmed + not_confirmed),
        or 0.0 when denominator is 0 (no_data rows excluded — they
        don't represent a real shadow decision).
    """
    _ensure_shadow_table(conn)
    if strategy_id is None:
        rows = conn.execute(
            "SELECT strategy_id, shadow_status "
            "  FROM paper_trades_intraday_confirm"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT strategy_id, shadow_status "
            "  FROM paper_trades_intraday_confirm "
            " WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchall()
    rows_list = [dict(r) for r in rows]
    overall = _aggregate_rows(rows_list)
    if strategy_id is not None:
        return overall
    by_strategy: Dict[str, Dict] = {}
    seen: Dict[str, List] = {}
    for r in rows_list:
        seen.setdefault(r["strategy_id"], []).append(r)
    for sid, sub in seen.items():
        by_strategy[sid] = _aggregate_rows(sub)
    overall["by_strategy"] = by_strategy
    return overall


def _aggregate_rows(rows: List[Dict]) -> Dict[str, Any]:
    count = len(rows)
    confirmed = sum(
        1 for r in rows
        if r.get("shadow_status") == SHADOW_STATUS_CONFIRMED
    )
    not_confirmed = sum(
        1 for r in rows
        if r.get("shadow_status") == SHADOW_STATUS_NOT_CONFIRMED
    )
    no_data = sum(
        1 for r in rows
        if r.get("shadow_status") == SHADOW_STATUS_NO_DATA
    )
    denom = confirmed + not_confirmed
    rate = round(confirmed / denom, 6) if denom > 0 else 0.0
    return {
        "count": count,
        "confirmed": confirmed,
        "not_confirmed": not_confirmed,
        "no_data": no_data,
        "confirmation_rate": rate,
    }
