"""
position_manager.py — single per-symbol order/position reservation layer.

Sprint 2 / M1. Root cause of the unintended-short bug: multiple intraday
strategies own the same broker symbol (e.g. NVDA under intraday-orb-pivots-5m
+ intraday-orbo-5m + intraday-1m-orb) and each fires its own exit / stop /
flatten against the SAME shared broker position. With no single owner, the
SELLs stack: shares already reserved by a resting stop (held_for_orders) get
sold again, the position is oversold past flat into a SHORT, and Alpaca rejects
the conflicting orders (40310000 "potential wash trade") or fails the flatten
("insufficient qty available, held_for_orders=N").

This module is the ONE place that decides how many shares may be sold for a
symbol RIGHT NOW, given the live broker state:

    available = long_position_qty - shares_reserved_by_open_sell_orders

Every sell / stop / flatten path routes its quantity through here so no path
can oversell past flat or fight another path's resting exit. It also
reconciles (cancels) incompatible resting exit orders before a new exit so the
broker never sees two opposite-or-stacked exits at once.

Long-only invariant: a SELL is never allowed to cross through zero into a short.
`cap_sell_qty` clamps to the available long quantity; if the position is already
flat or short, the capped quantity is 0 (nothing to sell).

Design notes:
  * Pure helpers (cap_sell_qty) are I/O-free and trivially testable.
  * Broker reads (broker_position, open_sell_orders) normalise alpaca-py models
    AND plain dicts/Mocks so tests inject fakes without the SDK.
  * available_to_sell prefers the broker's own `qty_available` when present
    (it already nets held_for_orders) and otherwise derives it from the open
    SELL orders, so both real-broker and partial-fake clients work.
  * Nothing here weakens a risk limit, the paper gate, or the kill switch — it
    only ever REDUCES a quantity or cancels a redundant order.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from config.utils import log

# Order/position rows whose presence means a strategy is still holding (or
# in-flight on) a long for a symbol. Mirrors _open_buy_for_pair's working set
# in auto_trader so ownership derivation agrees with the live position view.
_OPEN_BUY_STATUSES = ("filled", "partially_filled", "accepted", "new")
_CLOSED_SELL_EXCLUDED = ("canceled", "rejected")

# ---------------------------------------------------------------------------
# In-run sell-reservation ledger (Sprint 3 / M1)
# ---------------------------------------------------------------------------
# The broker is the single source of truth for how many shares a symbol holds.
# But within ONE trading pass, strategy A can submit a market SELL that the
# broker has not yet reflected in qty/qty_available (an 'accepted', not-yet-
# 'filled' order) when strategy B's exit reads the position microseconds later.
# Reading the broker alone then lets B oversell the same shares A is already
# selling — the multi-strategy shared-symbol oversell that grew the account to
# −$101k of unintended shorts despite Sprint 2's broker reads being on the path.
#
# This ledger records every share quantity THIS process has committed to sell
# per symbol (net of any cancel/release) so available_to_sell can subtract it
# from the broker's long qty. It NEVER inflates a quantity — only reduces it —
# so it cannot weaken a risk limit, the paper gate, or the kill switch. It is
# in-memory and per-process; a fresh process (or an explicit reset at the top
# of a trading pass) starts from zero, after which the broker's own settled
# qty already reflects prior sells.
_RESERVE_LOCK = threading.Lock()
_RUN_SELL_RESERVED: Dict[str, float] = {}


def reset_run_reservations() -> None:
    """Clear the in-run sell-reservation ledger.

    Call once at the very top of a trading pass (process_signals /
    close_intraday_positions) so each run starts from the broker's settled
    truth. Safe to call repeatedly; never raises.
    """
    with _RESERVE_LOCK:
        _RUN_SELL_RESERVED.clear()


def run_reserved_for(symbol: str) -> float:
    with _RESERVE_LOCK:
        return float(_RUN_SELL_RESERVED.get(symbol, 0.0))


def _reserve_run_sell(symbol: str, qty) -> None:
    q = _as_float(qty, 0.0)
    if q <= 0:
        return
    with _RESERVE_LOCK:
        _RUN_SELL_RESERVED[symbol] = _RUN_SELL_RESERVED.get(symbol, 0.0) + q


def _release_run_sell(symbol: str, qty) -> None:
    q = _as_float(qty, 0.0)
    if q <= 0:
        return
    with _RESERVE_LOCK:
        cur = _RUN_SELL_RESERVED.get(symbol, 0.0)
        _RUN_SELL_RESERVED[symbol] = max(0.0, cur - q)

# Order statuses that still reserve shares against the position (held_for_orders).
# A working SELL in any of these states is holding inventory the broker will not
# let us sell again.
WORKING_STATUSES = frozenset({
    "new", "accepted", "pending_new", "accepted_for_bidding",
    "partially_filled", "held", "pending_replace", "replaced",
    "calculated", "done_for_day",
})


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _strict_float(value):
    """float(value) for real numbers/strings, else None.

    Unlike _as_float this does NOT coerce non-numeric objects (e.g. a bare
    MagicMock auto-attribute) to a default — it returns None so callers can
    treat "broker returned junk" as UNKNOWN rather than as flat (0.0)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _attr(obj, name):
    """Read an attribute from an alpaca model OR a dict OR a Mock."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _status_str(order) -> str:
    raw = _attr(order, "status")
    if raw is None:
        return ""
    # alpaca enums stringify as 'OrderStatus.NEW'; take the tail and lower it.
    s = str(raw)
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    return s.strip().lower()


def _side_str(order) -> str:
    raw = _attr(order, "side")
    if raw is None:
        return ""
    s = str(raw)
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    return s.strip().lower()


# ---------------------------------------------------------------------------
# Pure quantity logic
# ---------------------------------------------------------------------------

def cap_sell_qty(requested_qty, available_qty) -> int:
    """Clamp a requested sell quantity to what may actually be sold.

    Returns an int in [0, floor(available_qty)]. Never returns more than the
    available long quantity — this is the long-only no-cross-into-short guard.
    A negative or zero `available_qty` (already flat/short) yields 0.
    """
    try:
        req = int(requested_qty)
    except (TypeError, ValueError):
        return 0
    if req <= 0:
        return 0
    avail = int(_as_float(available_qty, 0.0))
    if avail <= 0:
        return 0
    return min(req, avail)


# ---------------------------------------------------------------------------
# Broker reads
# ---------------------------------------------------------------------------

def can_read_positions(client) -> bool:
    """True iff the client exposes any position-reading method.

    A bare/stub client (no get_open_position / get_all_positions / list_positions)
    cannot tell us the live broker truth. In that case the reservation layer must
    NOT silently block a flatten (that would re-introduce the "positions never
    close" failure) — callers fall back to the requested qty.
    """
    return any(getattr(client, m, None) is not None
               for m in ("get_open_position", "get_all_positions",
                         "list_positions"))


def broker_position(client, symbol: str) -> Optional[Dict]:
    """Normalised live broker position for `symbol`, or None if flat.

    Returns {symbol, qty (signed float), qty_available (float, may be None)}.
    qty<0 means the account is SHORT. Uses get_open_position(symbol) when the
    client exposes it (cheapest), else falls back to scanning all positions.
    A "no position" broker error (404) is treated as flat → None.
    """
    getter = getattr(client, "get_open_position", None)
    pos = None
    if getter is not None:
        try:
            pos = getter(symbol)
        except Exception:
            pos = None
    if pos is None:
        all_getter = (getattr(client, "get_all_positions", None)
                      or getattr(client, "list_positions", None))
        if all_getter is not None:
            try:
                for p in (all_getter() or []):
                    if (_attr(p, "symbol") or "") == symbol:
                        pos = p
                        break
            except Exception:
                pos = None
    if pos is None:
        return None
    qty = _strict_float(_attr(pos, "qty"))
    if qty is None:
        # Position object present but qty isn't a real number (e.g. a bare
        # MagicMock auto-attribute). We can't trust it → signal "unknown".
        return {"symbol": symbol, "qty": None, "qty_available": None}
    qa_raw = _attr(pos, "qty_available")
    qty_available = _strict_float(qa_raw) if qa_raw is not None else None
    return {"symbol": symbol, "qty": qty, "qty_available": qty_available}


def open_sell_orders(client, symbol: str) -> List:
    """Working SELL orders for `symbol` that currently reserve shares.

    Returns the raw order objects (filtered to side=sell and a working status).
    Best-effort: a broker read failure yields [] so callers degrade to the
    broker's own qty_available rather than crashing.
    """
    orders = _get_open_orders(client, symbol)
    out = []
    for o in orders:
        if _side_str(o) != "sell":
            continue
        if _status_str(o) and _status_str(o) not in WORKING_STATUSES:
            continue
        out.append(o)
    return out


def _get_open_orders(client, symbol: str) -> List:
    getter = getattr(client, "get_orders", None)
    if getter is None:
        return []
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        orders = getter(filter=req)
    except Exception:
        # Fake clients in tests may accept no args / a plain call.
        try:
            orders = getter()
        except Exception as e:
            log(f"position_manager: get_orders failed for {symbol}: {e}",
                "WARNING")
            return []
    if not orders:
        return []
    # When we fell back to an arg-less getter, narrow to this symbol ourselves.
    return [o for o in orders if (_attr(o, "symbol") or symbol) == symbol]


def _reserved_by_open_sells(client, symbol: str) -> float:
    """Shares reserved by working SELL orders (qty minus already-filled qty)."""
    reserved = 0.0
    for o in open_sell_orders(client, symbol):
        qty = _as_float(_attr(o, "qty"), 0.0)
        filled = _as_float(_attr(o, "filled_qty"), 0.0)
        reserved += max(0.0, qty - filled)
    return reserved


def available_to_sell(
    client, symbol: str, *, include_run_reservations: bool = False,
) -> Optional[int]:
    """Net shares that may be sold for `symbol` right now without overselling.

        available = long_qty - shares_reserved_by_open_sell_orders
                            - shares_this_run_already_committed_to_sell

    Prefers the broker's own `qty_available` (it already nets held_for_orders)
    when present; otherwise derives the reservation from the open SELL orders.
    Returns 0 when flat, short, or fully reserved — never negative.

    When `include_run_reservations` is True (the guarded submit path), also
    subtracts the in-run sell-reservation ledger — shares THIS process has
    already submitted to sell this pass but the broker may not yet reflect.
    This is the M1 fix for the multi-strategy shared-symbol oversell: strategy
    B's exit cannot re-sell the shares strategy A's exit already committed,
    even before A's order settles at the broker. Pure broker reads default to
    False so isolated reads are unaffected.

    Returns None ("unknown") when the client can't report positions at all, so
    callers fall back to the requested qty rather than blocking a flatten on a
    stub broker. When positions ARE readable but the symbol isn't held, that's
    a genuine flat → 0.
    """
    if not can_read_positions(client):
        return None
    pos = broker_position(client, symbol)
    if pos is None:
        return 0
    long_qty = pos["qty"]
    if long_qty is None:
        # Broker returned an unparseable position → unknown, not flat.
        return None
    if long_qty <= 0:
        # Flat or already short: nothing a long-only strategy may sell.
        return 0
    qa = pos.get("qty_available")
    if qa is not None:
        avail = qa
    else:
        avail = long_qty - _reserved_by_open_sells(client, symbol)
    # Never report more than the long position, never negative.
    avail = min(avail, long_qty)
    if include_run_reservations:
        # Subtract what this process already committed to sell this run but the
        # broker may not yet have reflected. This is bounded by the long_qty
        # cap above, so it can only ever REDUCE available, never go negative.
        avail = avail - run_reserved_for(symbol)
    return int(max(0.0, avail))


# ---------------------------------------------------------------------------
# Exit-order reconciliation
# ---------------------------------------------------------------------------

def reconcile_exit_orders(client, symbol: str) -> int:
    """Cancel resting SELL orders for `symbol` so a fresh exit isn't rejected
    for a wash trade or blocked by held shares. Returns the count cancelled.

    Best-effort per order — one bad cancel never blocks the rest. This is the
    single chokepoint that previously lived (partially) in
    close_intraday_positions._cancel_open_orders_for_symbols.
    """
    cancelled = 0
    canceller = getattr(client, "cancel_order_by_id", None)
    if canceller is None:
        return 0
    for o in open_sell_orders(client, symbol):
        oid = _attr(o, "id")
        if oid is None:
            continue
        try:
            canceller(oid)
            cancelled += 1
        except Exception as e:
            log(f"position_manager: cancel failed for order {oid} "
                f"({symbol}): {e}", "WARNING")
    return cancelled


# ---------------------------------------------------------------------------
# Guarded submit entry points (the ONE place all sell paths go through)
# ---------------------------------------------------------------------------

def safe_sell_qty(client, symbol: str, requested_qty) -> int:
    """The capped quantity a path may sell right now: min(requested, available).

    Pure read + clamp; submits nothing. Sell/stop/flatten paths call this to
    size their order so no path oversells past flat. Returns 0 when there's
    nothing safe to sell (flat, short, or fully reserved). When the broker can't
    report positions (unknown), falls back to the requested qty."""
    avail = available_to_sell(client, symbol)
    if avail is None:
        try:
            return max(0, int(requested_qty))
        except (TypeError, ValueError):
            return 0
    return cap_sell_qty(requested_qty, avail)


def safe_submit_sell(
    client, *, symbol: str, requested_qty, submit_fn,
    reconcile: bool = True, side: str = "sell",
) -> Optional[Dict]:
    """Submit a guarded SELL via `submit_fn(client, symbol=, qty=, side=)`.

    1. (optional) reconcile/cancel conflicting resting SELLs first.
    2. Read available from the BROKER, netting both the broker's own held
       shares AND the in-run sell-reservation ledger (shares this process has
       already committed to sell this pass but the broker may not yet reflect).
    3. Cap the requested qty to available — never oversell past flat, and never
       let a second strategy re-sell shares a first strategy already committed.
    4. Submit only if the capped qty >= 1, then reserve those shares in the run
       ledger so the next caller this pass sees them as already gone. Else
       return a SKIP record.

    Returns:
      {"action": "SUBMITTED", "qty": n, "order": <order>} on submit,
      {"action": "SKIP_NO_AVAILABLE_QTY", "qty": 0, "requested": r} otherwise.
    `submit_fn` is the existing _submit_market_order / cover submitter so this
    layer stays broker-agnostic and test-injectable.
    """
    if reconcile:
        reconcile_exit_orders(client, symbol)
    avail = available_to_sell(client, symbol, include_run_reservations=True)
    if avail is None:
        # Broker can't report positions (stub client / read unavailable): don't
        # block the flatten — fall back to the requested qty. The broker itself
        # still rejects a true oversell; this layer only ADDS safety when it has
        # truth, never removes the prior ability to close.
        qty = max(0, int(_as_float(requested_qty, 0)))
    else:
        qty = cap_sell_qty(requested_qty, avail)
    if qty < 1:
        log(f"position_manager: SKIP sell {symbol} requested={requested_qty} "
            f"available={avail} (nothing safe to sell)", "INFO")
        return {"action": "SKIP_NO_AVAILABLE_QTY", "symbol": symbol,
                "qty": 0, "requested": int(_as_float(requested_qty, 0)),
                "available": avail}
    order = submit_fn(client, symbol=symbol, qty=qty, side=side)
    # Record the committed sell so a later exit/flatten THIS run nets it out and
    # can't re-sell the same shares before the broker settles the fill.
    _reserve_run_sell(symbol, qty)
    return {"action": "SUBMITTED", "symbol": symbol, "qty": qty,
            "requested": int(_as_float(requested_qty, 0)),
            "available": avail, "order": order}


def safe_submit_buy_to_cover(
    client, *, symbol: str, submit_fn, reconcile: bool = True,
) -> Optional[Dict]:
    """Flatten an unintended SHORT by buying exactly abs(short_qty) shares.

    Used by the M2 cover tool. Submits nothing unless the live broker position
    is genuinely SHORT (qty<0). Buys exactly enough to reach flat — never more
    (no crossing through zero into a long).

    Returns:
      {"action": "COVERED", "qty": n, "order": <order>} on submit,
      {"action": "SKIP_NOT_SHORT", "qty": 0, "position_qty": q} otherwise.
    """
    pos = broker_position(client, symbol)
    if pos is None or pos["qty"] is None or pos["qty"] >= 0:
        return {"action": "SKIP_NOT_SHORT", "symbol": symbol, "qty": 0,
                "position_qty": (pos["qty"] if pos else 0.0)}
    cover_qty = int(abs(pos["qty"]))
    if cover_qty < 1:
        return {"action": "SKIP_NOT_SHORT", "symbol": symbol, "qty": 0,
                "position_qty": pos["qty"]}
    if reconcile:
        # Clear any resting orders for the symbol so the cover buy is clean.
        reconcile_exit_orders(client, symbol)
    order = submit_fn(client, symbol=symbol, qty=cover_qty, side="buy")
    return {"action": "COVERED", "symbol": symbol, "qty": cover_qty,
            "position_qty": pos["qty"], "order": order}


# ---------------------------------------------------------------------------
# M2 — single symbol-owner authority (OPTION A: one owner per symbol)
# ---------------------------------------------------------------------------
# Alpaca sees ONE broker position per symbol; before M2, any number of
# strategies could each open a long on the same symbol and then each fire its
# own exit/stop/flatten against that ONE shared position -> stacked SELLs,
# 40310000 wash rejects, overselling past flat into a short, and correlated
# dogpiling.
#
# OPTION A removes the conflict by construction: the FIRST strategy to hold a
# symbol OWNS it. While that symbol is held, any OTHER strategy's ENTRY on it is
# REJECTED (recorded as a skip). One broker position -> one owner -> one
# exit/stop/flatten stack. No two strategies ever touch the same symbol, so the
# shared-symbol exit/stop conflict cannot arise.
#
# PERSISTENCE: ownership is DERIVED from the live DB, not held in process memory
# (the live system is stateless 15-min scheduled subprocess runs — in-process
# memory dies each run). The owner of a symbol is the strategy with the OLDEST
# still-open buy in `paper_trades` (same working-status set auto_trader's
# `_open_buy_for_pair` uses to decide a position is live). Because `paper_trades`
# already persists across runs, ownership reconstructs deterministically every
# pass with NO new schema and NO migration. A symbol with no open buy is
# unowned (free to claim).


def _has_later_sell(conn, strategy_id: str, symbol: str, after_submitted_at: str) -> bool:
    """True iff a CLOSING sell exists after the open buy.

    A resting protective SELL STOP (order_type LIKE '%stop%') is NOT a realized
    close — it holds the position, it doesn't release it. Excluding stop rows is
    what lets a still-protected long stay OWNED; counting them would falsely free
    the symbol the moment its stop is armed (and let a second strategy claim it).
    Only a market/limit sell (or a filled stop) closes the position.
    """
    row = conn.execute(
        "SELECT 1 FROM paper_trades WHERE strategy_id=? AND symbol=? "
        "  AND side='sell' AND submitted_at > ? "
        "  AND status NOT IN ('canceled', 'rejected') "
        "  AND (order_type IS NULL OR order_type NOT LIKE '%stop%' "
        "       OR status='filled') LIMIT 1",
        (strategy_id, symbol, after_submitted_at),
    ).fetchone()
    return row is not None


def open_buy_owners(conn, symbol: str) -> List[str]:
    """Strategy ids that currently hold an UN-closed long for `symbol`.

    A strategy holds the symbol when it has a buy in a working status with no
    later non-cancelled sell. Ordered oldest-first by the open buy's
    submitted_at, so the head of the list is the priority/first owner. Normally
    length 0 (flat) or 1 (owned). Length >1 only on legacy rows pre-dating M2
    (multiple strategies already sharing a symbol) — the owner authority then
    deterministically picks the head and treats the rest as non-owners.
    """
    placeholders = ",".join("?" for _ in _OPEN_BUY_STATUSES)
    rows = conn.execute(
        f"SELECT strategy_id, MIN(submitted_at) AS first_open "
        f"FROM paper_trades WHERE symbol=? AND side='buy' "
        f"  AND status IN ({placeholders}) "
        f"GROUP BY strategy_id ORDER BY first_open ASC",
        (symbol, *_OPEN_BUY_STATUSES),
    ).fetchall()
    owners: List[str] = []
    for r in rows:
        sid = r["strategy_id"]
        if not sid:
            continue
        if _has_later_sell(conn, sid, symbol, r["first_open"]):
            continue
        owners.append(sid)
    return owners


def symbol_owner(conn, symbol: str) -> Optional[str]:
    """The single strategy that owns `symbol` right now, or None if unowned.

    The owner is the strategy holding the OLDEST open buy (first to claim).
    """
    owners = open_buy_owners(conn, symbol)
    return owners[0] if owners else None


def owns_symbol(conn, strategy_id: str, symbol: str) -> bool:
    """True iff `strategy_id` is THE owner of `symbol` (the first/priority
    holder). A non-owner that happens to hold a legacy shared position returns
    False — it may not submit exits/stops/flattens for a symbol it doesn't own.
    """
    return symbol_owner(conn, symbol) == strategy_id


def entry_owner_conflict(conn, strategy_id: str, symbol: str) -> Optional[str]:
    """For an ENTRY: the id of the strategy that already owns `symbol`, when
    that owner is someone OTHER than `strategy_id`. Returns None when the symbol
    is unowned (free to claim) or already owned by `strategy_id` itself (a
    pyramid add-on, handled upstream). The entry path rejects when this is
    non-None — OPTION A: one owner per symbol.
    """
    owner = symbol_owner(conn, symbol)
    if owner is None or owner == strategy_id:
        return None
    return owner


# ---------------------------------------------------------------------------
# M3 — idempotent stop / flatten / sell
# ---------------------------------------------------------------------------
# A protective SELL STOP reserves shares at the broker (held_for_orders). The
# pre-M3 path (`stops.submit_atr_stop`) blindly submitted a NEW stop every time
# it was called — so re-arming a symbol that already had a resting stop stacked
# a SECOND SELL STOP. Alpaca then either rejected it 40310000 (potential wash
# trade — two SELLs on a long-only position) or both stops reserved shares so
# the later market flatten saw held_for_orders == qty and failed "insufficient
# qty available". This is the remaining wash-trade source flagged in M1's
# handoff. M3 makes stop submission idempotent: cancel any incompatible resting
# SELL first (cancel/replace, not stack), then submit only the net-available
# quantity, and never cross zero into a short.


def safe_submit_stop(
    client, *, symbol: str, requested_qty, stop_price, submit_fn,
    reconcile: bool = True,
) -> Optional[Dict]:
    """Submit a protective SELL STOP idempotently.

    1. (optional) cancel any resting SELL (incl. an existing stop) for `symbol`
       so a re-arm REPLACES rather than STACKS — no two SELL stops on one long
       (the 40310000 wash-trade / double-reservation source).
    2. Read available net of held_for_orders AND the in-run sell-reservation
       ledger; cap the stop qty so it never reserves more than the position
       holds and never crosses zero into a short.
    3. Submit only when capped qty >= 1, via
       `submit_fn(client, symbol=, qty=, stop_price=)` — the existing
       stops.submit_atr_stop, kept broker-agnostic and test-injectable.

    Returns:
      {"action": "SUBMITTED", "qty": n, "cancelled": c, "order": <order>}
      {"action": "SKIP_NO_AVAILABLE_QTY", "qty": 0, "requested": r, ...}
    """
    cancelled = 0
    if reconcile:
        cancelled = reconcile_exit_orders(client, symbol)
    avail = available_to_sell(client, symbol, include_run_reservations=True)
    if avail is None:
        # Broker can't report positions (stub/unavailable): don't block arming a
        # stop on a freshly-filled entry whose position isn't visible yet — fall
        # back to the requested qty. The broker still rejects a true oversell.
        qty = max(0, int(_as_float(requested_qty, 0)))
    else:
        qty = cap_sell_qty(requested_qty, avail)
    if qty < 1:
        log(f"position_manager: SKIP stop {symbol} requested={requested_qty} "
            f"available={avail} (nothing to protect)", "INFO")
        return {"action": "SKIP_NO_AVAILABLE_QTY", "symbol": symbol, "qty": 0,
                "requested": int(_as_float(requested_qty, 0)),
                "available": avail, "cancelled": cancelled}
    order = submit_fn(client, symbol=symbol, qty=qty, stop_price=stop_price)
    return {"action": "SUBMITTED", "symbol": symbol, "qty": qty,
            "requested": int(_as_float(requested_qty, 0)),
            "available": avail, "cancelled": cancelled, "order": order}
