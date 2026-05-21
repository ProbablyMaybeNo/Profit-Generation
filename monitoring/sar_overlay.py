"""
sar_overlay.py — 6.4.1 Parabolic SAR exit overlay.

Wilder's Parabolic SAR (Stop And Reverse) computed per-position. As a
standalone signal SAR whipsaws — but as an *overlay* on top of a
trailing stop (4.6.1), it can lock in profit when momentum stalls
before the trailing stop would have triggered. Strategies opt in via
`sar_overlay: true` in their TRACKED_STRATEGIES declaration.

Wilder's defaults:
  - initial acceleration factor (AF): 0.02
  - AF increment per new extreme point (EP): 0.02
  - AF cap: 0.20

For a LONG:
  - SAR starts at the lowest low over the prior bars (or the previous
    SAR if reversing from a short trend).
  - SAR_t = SAR_{t-1} + AF × (EP - SAR_{t-1})
  - EP (extreme point) = highest high since the trend started.
  - When AF reaches a new high (i.e., a new EP is set), AF += 0.02
    (capped at 0.2).
  - The SAR for any bar is constrained to NOT exceed the LOW of either
    of the prior two bars — prevents stop-out on routine pullbacks
    inside a strong trend (Wilder's rule).
  - A "flip" occurs when the bar's low touches or crosses below SAR —
    that's the exit trigger for the long.

Mirror for SHORT.

The overlay engine combines this with `should_exit_on_trailing_stop`:

    should_exit = trailing_stop_hit OR sar_flip

State (extreme_point, acceleration_factor, current_sar, direction)
is persisted in a `sar_state` table keyed by (strategy_id, symbol).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional


DEFAULT_AF_START = 0.02
DEFAULT_AF_INCREMENT = 0.02
DEFAULT_AF_MAX = 0.20

DIRECTION_LONG = "long"
DIRECTION_SHORT = "short"

# 6.4.2 — opt-in modes for a strategy's `sar_overlay` declaration.
#   "shadow" → observe only; do NOT affect the live exit decision.
#              SAR flips are recorded to paper_trades_sar_overlay
#              alongside the real exit, for 30-day A/B comparison.
#   True / "live" → fold SAR flip into the exit decision (legacy 6.4.1).
#   False / missing → no overlay at all.
SAR_OVERLAY_MODE_SHADOW = "shadow"
SAR_OVERLAY_MODE_LIVE = "live"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_sar_table(conn: sqlite3.Connection) -> None:
    """Idempotent — creates the sar_state table if absent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sar_state (
            strategy_id   TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            direction     TEXT NOT NULL DEFAULT 'long',
            sar           REAL NOT NULL,
            extreme_point REAL NOT NULL,
            af            REAL NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY(strategy_id, symbol)
        )
    """)


# ---------------------------------------------------------------------------
# Pure SAR math — sequence over a series of OHLC bars
# ---------------------------------------------------------------------------

def compute_sar_series(
    bars: List[Dict],
    *,
    direction: str = DIRECTION_LONG,
    af_start: float = DEFAULT_AF_START,
    af_increment: float = DEFAULT_AF_INCREMENT,
    af_max: float = DEFAULT_AF_MAX,
) -> List[Optional[float]]:
    """Compute the SAR series for a list of OHLC bar dicts.

    bars: list of {high, low, close} dicts (any case). Length >= 2
    direction: initial trend direction. The function does NOT handle
      trend reversals — that's the overlay engine's job. It computes
      a continuous SAR sequence assuming the trend doesn't reverse.
      The first bar's SAR is the initial seed (the lowest low for a
      long; highest high for a short) and is returned as None to mark
      "no SAR yet".

    Returns a list of length len(bars). bars[0] gets None (no SAR yet
    — the seed bar). bars[i>=1] get the computed SAR for that bar.
    """
    n = len(bars)
    if n < 2:
        return [None] * n
    side = (direction or DIRECTION_LONG).lower()
    if side not in (DIRECTION_LONG, DIRECTION_SHORT):
        side = DIRECTION_LONG

    # Seed:
    #   long  → SAR = lowest low of bar 0, EP = highest high so far
    #   short → SAR = highest high of bar 0, EP = lowest low so far
    if side == DIRECTION_LONG:
        sar = float(_get(bars[0], "low"))
        ep = float(_get(bars[0], "high"))
    else:
        sar = float(_get(bars[0], "high"))
        ep = float(_get(bars[0], "low"))
    af = af_start
    out: List[Optional[float]] = [None]
    for i in range(1, n):
        # Step 1: compute the NEW SAR for bar i from prior state.
        new_sar = sar + af * (ep - sar)
        # Wilder's constraint: SAR can't penetrate the LOW (long) of
        # the prior two bars — limit it.
        if side == DIRECTION_LONG:
            if i >= 2:
                limit = min(float(_get(bars[i - 1], "low")),
                            float(_get(bars[i - 2], "low")))
            else:
                limit = float(_get(bars[i - 1], "low"))
            if new_sar > limit:
                new_sar = limit
        else:
            if i >= 2:
                limit = max(float(_get(bars[i - 1], "high")),
                            float(_get(bars[i - 2], "high")))
            else:
                limit = float(_get(bars[i - 1], "high"))
            if new_sar < limit:
                new_sar = limit
        # Step 2: update EP + AF based on bar i's price.
        high = float(_get(bars[i], "high"))
        low = float(_get(bars[i], "low"))
        if side == DIRECTION_LONG:
            if high > ep:
                ep = high
                af = min(af + af_increment, af_max)
        else:
            if low < ep:
                ep = low
                af = min(af + af_increment, af_max)
        out.append(round(new_sar, 6))
        sar = new_sar
    return out


def _get(d: Dict, key: str):
    """Case-insensitive lookup."""
    if key in d:
        return d[key]
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    raise KeyError(key)


# ---------------------------------------------------------------------------
# Persisted state — one row per open position
# ---------------------------------------------------------------------------

def init_sar(
    conn: sqlite3.Connection,
    *,
    strategy_id: str, symbol: str,
    bars: List[Dict],
    direction: str = DIRECTION_LONG,
    af_start: float = DEFAULT_AF_START,
    af_increment: float = DEFAULT_AF_INCREMENT,
    af_max: float = DEFAULT_AF_MAX,
    now_iso: Optional[str] = None,
) -> Dict:
    """Initialise SAR state for a newly-opened position. Computes the
    SAR series across `bars` (expected to include the entry bar +
    enough history for the seed), then persists the LATEST SAR / EP /
    AF as the row to advance from on subsequent bars.

    Returns the persisted state dict {sar, extreme_point, af, direction}.
    """
    _ensure_sar_table(conn)
    series = compute_sar_series(
        bars, direction=direction,
        af_start=af_start, af_increment=af_increment, af_max=af_max,
    )
    side = (direction or DIRECTION_LONG).lower()
    # The latest SAR is the last non-None entry in the series; fall
    # back to the seed value if the series is too short.
    last_sar = next(
        (s for s in reversed(series) if s is not None),
        None,
    )
    if last_sar is None:
        # Only one bar — use the seed directly.
        if side == DIRECTION_LONG:
            last_sar = float(_get(bars[0], "low"))
            ep = float(_get(bars[0], "high"))
        else:
            last_sar = float(_get(bars[0], "high"))
            ep = float(_get(bars[0], "low"))
        af = af_start
    else:
        # Recompute final EP + AF by walking again (we threw them away).
        # Cheap to do — a fresh walk is O(N).
        ep, af = _recompute_ep_af(
            bars, side, af_start, af_increment, af_max,
        )
    return _upsert_state(
        conn,
        strategy_id=strategy_id, symbol=symbol,
        direction=side,
        sar=float(last_sar), extreme_point=float(ep), af=float(af),
        now_iso=now_iso or _utc_now_iso(),
    )


def _recompute_ep_af(
    bars: List[Dict], side: str,
    af_start: float, af_increment: float, af_max: float,
) -> tuple:
    if side == DIRECTION_LONG:
        ep = float(_get(bars[0], "high"))
    else:
        ep = float(_get(bars[0], "low"))
    af = af_start
    for i in range(1, len(bars)):
        h = float(_get(bars[i], "high"))
        lo = float(_get(bars[i], "low"))
        if side == DIRECTION_LONG and h > ep:
            ep = h
            af = min(af + af_increment, af_max)
        elif side == DIRECTION_SHORT and lo < ep:
            ep = lo
            af = min(af + af_increment, af_max)
    return ep, af


def advance_sar(
    conn: sqlite3.Connection,
    *,
    strategy_id: str, symbol: str,
    bar: Dict,
    af_increment: float = DEFAULT_AF_INCREMENT,
    af_max: float = DEFAULT_AF_MAX,
    now_iso: Optional[str] = None,
) -> Optional[Dict]:
    """Advance SAR state by one bar. Returns the updated row or None
    if no SAR state exists for (strategy_id, symbol)."""
    _ensure_sar_table(conn)
    row = get_sar_state(conn, strategy_id=strategy_id, symbol=symbol)
    if row is None:
        return None
    side = row["direction"]
    sar = float(row["sar"])
    ep = float(row["extreme_point"])
    af = float(row["af"])
    new_sar = sar + af * (ep - sar)
    high = float(_get(bar, "high"))
    low = float(_get(bar, "low"))
    if side == DIRECTION_LONG:
        if high > ep:
            ep = high
            af = min(af + af_increment, af_max)
    else:
        if low < ep:
            ep = low
            af = min(af + af_increment, af_max)
    return _upsert_state(
        conn,
        strategy_id=strategy_id, symbol=symbol, direction=side,
        sar=round(new_sar, 6), extreme_point=round(ep, 6),
        af=round(af, 6),
        now_iso=now_iso or _utc_now_iso(),
    )


def get_sar_state(
    conn: sqlite3.Connection,
    *,
    strategy_id: str, symbol: str,
) -> Optional[Dict]:
    """Return the current SAR state row, or None if absent."""
    _ensure_sar_table(conn)
    row = conn.execute(
        "SELECT * FROM sar_state WHERE strategy_id=? AND symbol=?",
        (strategy_id, symbol),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def clear_sar_state(
    conn: sqlite3.Connection,
    *,
    strategy_id: str, symbol: str,
) -> None:
    """Remove SAR state — call when the position closes."""
    _ensure_sar_table(conn)
    with conn:
        conn.execute(
            "DELETE FROM sar_state WHERE strategy_id=? AND symbol=?",
            (strategy_id, symbol),
        )


def _upsert_state(
    conn: sqlite3.Connection,
    *,
    strategy_id: str, symbol: str,
    direction: str, sar: float, extreme_point: float, af: float,
    now_iso: str,
) -> Dict:
    with conn:
        conn.execute("""
            INSERT INTO sar_state
                (strategy_id, symbol, direction, sar, extreme_point,
                 af, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, symbol) DO UPDATE SET
                direction=excluded.direction,
                sar=excluded.sar,
                extreme_point=excluded.extreme_point,
                af=excluded.af,
                updated_at=excluded.updated_at
        """, (strategy_id, symbol, direction, sar, extreme_point, af,
              now_iso))
    return {
        "strategy_id": strategy_id, "symbol": symbol,
        "direction": direction,
        "sar": sar, "extreme_point": extreme_point, "af": af,
        "updated_at": now_iso,
    }


# ---------------------------------------------------------------------------
# Overlay engine — auto-trader integration
# ---------------------------------------------------------------------------

def is_sar_flip(
    *,
    sar: float, direction: str,
    bar_low: float, bar_high: float,
) -> bool:
    """True iff the bar's price crossed the SAR — a "flip" / exit
    signal. For longs: low ≤ SAR. For shorts: high ≥ SAR.
    """
    side = (direction or DIRECTION_LONG).lower()
    if side == DIRECTION_LONG:
        return bar_low <= sar
    return bar_high >= sar


def should_exit_with_sar_overlay(
    conn: sqlite3.Connection,
    *,
    strategy_id: str, symbol: str,
    current_price: float,
    bar_low: Optional[float] = None,
    bar_high: Optional[float] = None,
    trailing_stop_hit: bool = False,
) -> Dict:
    """6.4.1 overlay engine — returns {should_exit, reason, ...}.

    Precedence: whichever fires first wins. If both fire on the same
    bar, `reason` reports both (trailing_stop wins for the exit
    reason field but sar_flip is also flagged).

    `current_price` is used when bar_low / bar_high aren't supplied
    (e.g. minute-cadence checks). Callers with bar data should pass
    bar_low and bar_high — SAR triggers on the bar's range, not its
    close.
    """
    sar_row = get_sar_state(conn, strategy_id=strategy_id, symbol=symbol)
    out = {
        "should_exit": False,
        "reason": None,
        "trailing_stop_hit": bool(trailing_stop_hit),
        "sar_flip": False,
        "sar": (sar_row or {}).get("sar"),
        "sar_direction": (sar_row or {}).get("direction"),
    }
    if sar_row is not None:
        lo = bar_low if bar_low is not None else current_price
        hi = bar_high if bar_high is not None else current_price
        out["sar_flip"] = is_sar_flip(
            sar=float(sar_row["sar"]),
            direction=sar_row["direction"],
            bar_low=lo, bar_high=hi,
        )
    if out["trailing_stop_hit"] and out["sar_flip"]:
        out["should_exit"] = True
        out["reason"] = "trailing_stop_hit+sar_flip"
    elif out["trailing_stop_hit"]:
        out["should_exit"] = True
        out["reason"] = "trailing_stop_hit"
    elif out["sar_flip"]:
        out["should_exit"] = True
        out["reason"] = "sar_flip"
    return out


def strategy_has_sar_overlay(strategy_meta: Optional[Dict]) -> bool:
    """True iff the strategy declaration opts in to the LIVE SAR overlay.

    A value of ``"shadow"`` (6.4.2 — observational A/B record only) is
    NOT live and returns False here. Use ``strategy_has_sar_shadow`` to
    detect shadow-mode opt-in. All other truthy values (True, "live",
    "yes", 1) are treated as live for backward compatibility.
    """
    if not isinstance(strategy_meta, dict):
        return False
    value = strategy_meta.get("sar_overlay", False)
    if isinstance(value, str) and value.lower() == SAR_OVERLAY_MODE_SHADOW:
        return False
    return bool(value)


def strategy_has_sar_shadow(strategy_meta: Optional[Dict]) -> bool:
    """True iff the strategy opts in to SAR shadow recording.

    Returns True for ``sar_overlay: "shadow"`` (the 6.4.2 A/B observability
    mode) and also for any live opt-in — because if SAR is live, the
    shadow record is still useful as an audit of what actually fired.
    """
    if not isinstance(strategy_meta, dict):
        return False
    value = strategy_meta.get("sar_overlay", False)
    if isinstance(value, str) and value.lower() == SAR_OVERLAY_MODE_SHADOW:
        return True
    return bool(value)


# ---------------------------------------------------------------------------
# 6.4.2 — shadow A/B record + analytics
# ---------------------------------------------------------------------------

def _ensure_shadow_table(conn: sqlite3.Connection) -> None:
    """Idempotent — creates paper_trades_sar_overlay if absent.

    Matches the canonical DDL in data/db.py (kept in sync). Present here
    so tests / standalone callers don't have to import data.db just to
    write a shadow row.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades_sar_overlay (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at       TEXT NOT NULL,
            strategy_id       TEXT NOT NULL,
            symbol            TEXT NOT NULL,
            side              TEXT NOT NULL DEFAULT 'long',
            entry_order_id    TEXT,
            entry_price       REAL,
            qty               REAL,
            shadow_exit_price REAL NOT NULL,
            shadow_sar        REAL,
            shadow_reason     TEXT NOT NULL,
            real_exit_price   REAL,
            real_exit_reason  TEXT,
            shadow_pnl        REAL,
            real_pnl          REAL,
            notes             TEXT,
            UNIQUE(strategy_id, symbol, entry_order_id, recorded_at)
        )
    """)


def record_shadow_exit(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    symbol: str,
    side: str = DIRECTION_LONG,
    entry_order_id: Optional[str] = None,
    entry_price: Optional[float] = None,
    qty: Optional[float] = None,
    shadow_exit_price: float,
    shadow_sar: Optional[float] = None,
    shadow_reason: str = "sar_flip",
    real_exit_price: Optional[float] = None,
    real_exit_reason: Optional[str] = None,
    notes: Optional[str] = None,
    now_iso: Optional[str] = None,
) -> Optional[int]:
    """Persist a single shadow exit row to paper_trades_sar_overlay.

    Computes shadow_pnl and real_pnl when entry_price + qty are supplied.
    For longs: pnl = (exit_price - entry_price) × qty.
    For shorts: pnl = (entry_price - exit_price) × qty.

    Returns the inserted row id, or None when the UNIQUE clause caused a
    no-op (duplicate shadow entry at the same recorded_at — caller can
    safely ignore).
    """
    _ensure_shadow_table(conn)
    side_norm = (side or DIRECTION_LONG).lower()
    shadow_pnl = _calc_pnl(
        entry_price=entry_price, exit_price=shadow_exit_price,
        qty=qty, side=side_norm,
    )
    real_pnl = _calc_pnl(
        entry_price=entry_price, exit_price=real_exit_price,
        qty=qty, side=side_norm,
    )
    with conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_trades_sar_overlay
                (recorded_at, strategy_id, symbol, side,
                 entry_order_id, entry_price, qty,
                 shadow_exit_price, shadow_sar, shadow_reason,
                 real_exit_price, real_exit_reason,
                 shadow_pnl, real_pnl, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso or _utc_now_iso(),
                strategy_id, symbol, side_norm,
                entry_order_id, entry_price, qty,
                float(shadow_exit_price), shadow_sar, shadow_reason,
                real_exit_price, real_exit_reason,
                shadow_pnl, real_pnl, notes,
            ),
        )
        return cur.lastrowid if cur.rowcount else None


def _calc_pnl(
    *, entry_price: Optional[float], exit_price: Optional[float],
    qty: Optional[float], side: str,
) -> Optional[float]:
    if entry_price is None or exit_price is None or qty is None:
        return None
    try:
        ep = float(entry_price)
        xp = float(exit_price)
        q = float(qty)
    except (TypeError, ValueError):
        return None
    if side == DIRECTION_SHORT:
        return round((ep - xp) * q, 6)
    return round((xp - ep) * q, 6)


def aggregate_ab(
    conn: sqlite3.Connection,
    *,
    strategy_id: Optional[str] = None,
) -> Dict:
    """Return aggregate A/B comparison stats from paper_trades_sar_overlay.

    When ``strategy_id`` is None, returns global aggregates + a
    per-strategy breakdown. When set, returns just that strategy's row.

    Aggregates per scope:
      - count: number of shadow events recorded
      - shadow_total_pnl / real_total_pnl: sum of pnl across rows where
        both sides have a numeric pnl (rows missing either are skipped
        — they can't contribute to the delta)
      - pnl_delta: shadow_total_pnl - real_total_pnl. Positive → SAR
        overlay would have made more.
      - shadow_wins / real_wins: count of rows where pnl > 0.
      - shadow_win_rate / real_win_rate: shadow_wins / count_with_both_pnl
      - win_rate_delta: shadow_win_rate - real_win_rate
    """
    _ensure_shadow_table(conn)
    if strategy_id is None:
        rows = conn.execute(
            "SELECT strategy_id, shadow_pnl, real_pnl "
            "  FROM paper_trades_sar_overlay"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT strategy_id, shadow_pnl, real_pnl "
            "  FROM paper_trades_sar_overlay WHERE strategy_id=?",
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


def _aggregate_rows(rows: List[Dict]) -> Dict:
    count = len(rows)
    paired = [
        r for r in rows
        if r.get("shadow_pnl") is not None and r.get("real_pnl") is not None
    ]
    shadow_total = sum(float(r["shadow_pnl"]) for r in paired)
    real_total = sum(float(r["real_pnl"]) for r in paired)
    shadow_wins = sum(1 for r in paired if float(r["shadow_pnl"]) > 0)
    real_wins = sum(1 for r in paired if float(r["real_pnl"]) > 0)
    n_paired = len(paired)
    if n_paired > 0:
        shadow_wr = shadow_wins / n_paired
        real_wr = real_wins / n_paired
        wr_delta = shadow_wr - real_wr
    else:
        shadow_wr = real_wr = wr_delta = 0.0
    return {
        "count": count,
        "count_with_both_pnl": n_paired,
        "shadow_total_pnl": round(shadow_total, 6),
        "real_total_pnl": round(real_total, 6),
        "pnl_delta": round(shadow_total - real_total, 6),
        "shadow_wins": shadow_wins,
        "real_wins": real_wins,
        "shadow_win_rate": round(shadow_wr, 6),
        "real_win_rate": round(real_wr, 6),
        "win_rate_delta": round(wr_delta, 6),
    }
