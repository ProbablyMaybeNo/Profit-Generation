"""
close_intraday_positions.py — EOD close-out of intraday-strategy
positions (5.5.3).

Intraday strategies should not hold overnight. Gap risk on a 15-min
mean-reversion position is a categorically different risk profile from
the strategy's design — the strategy proved its edge on the assumption
that positions exit within the same session. Forcing flat at the close
keeps the realized P/L driven by the strategy's intra-session behavior.

What it does:
  1. Identify all currently-open paper_trades rows whose corresponding
     signal has bar_interval != '1d'.
  2. For each open intraday position, submit a market sell (or, when
     supported by the broker, an MOC order) to close it.
  3. Record the close in paper_trades with notes="auto-close intraday EOD".

Invoked from schedulers/run_daily.bat at 16:00 ET (15:00 PT for Ross)
after the regular daily close pipeline. Idempotent — re-running emits
zero closes if all intraday positions are already flat.

Per the plan: "Alpaca MOC order or fallback to market." Alpaca-py's
SDK accepts `time_in_force=DAY` + `type=market` to fire a market order
that fills at the next available bar; for true MOC the API expects
`time_in_force=OPG`/`CLS` — feature support varies by symbol. To stay
simple and broker-agnostic we use market orders submitted shortly
before the close, which is functionally equivalent in paper.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Callable, List, Optional

from config.utils import get_alpaca_client, is_paper_mode, log
from data import db
from monitoring import excursion

# F2-SAFETY (audit 2026-06-03): exit_reason stamped on intraday outcomes that
# were left OPEN by a PRIOR session — i.e. the EOD flatten that normally owns
# their close never ran (crash / restart / schedule gap). Distinct from
# 'eod_close' so these orphan-sweeps are honestly attributable in stats.
STALE_INTRADAY_EXIT_REASON = "stale_intraday_flatten_missed"


def _cancel_open_orders_for_symbols(client, symbols: List[str]) -> int:
    """Cancel resting open orders (initial ATR stops, trailing stops,
    unfilled entries) for the given symbols so the EOD flatten isn't
    rejected for "potential wash trade detected" (an opposite-side order
    exists) or "insufficient qty available" (shares held_for_orders by a
    resting stop). Returns the number of cancel requests issued.

    Best-effort per order — one bad cancel never blocks the rest.
    """
    if not symbols:
        return 0
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=list(symbols))
    orders = client.get_orders(filter=req)
    n = 0
    for o in orders:
        try:
            client.cancel_order_by_id(o.id)
            n += 1
        except Exception as e:
            log(f"close_intraday_positions: cancel failed for order "
                f"{getattr(o, 'id', '?')} ({getattr(o, 'symbol', '?')}): {e}",
                "WARNING")
    return n


def _open_intraday_buys(conn) -> List[dict]:
    """Return one row per open paper_trades buy whose signal is intraday.

    A "buy" is open when no offsetting sell exists. Joins through signals
    to filter on bar_interval != '1d'.
    """
    rows = conn.execute(
        """
        SELECT pt.id, pt.alpaca_order_id, pt.signal_id, pt.strategy_id,
               pt.symbol, pt.qty, pt.submitted_at, pt.fill_price,
               s.bar_interval, s.bar_ts
          FROM paper_trades pt
          JOIN signals s ON s.id = pt.signal_id
         WHERE pt.side = 'buy'
           AND pt.status IN ('filled', 'partially_filled', 'accepted', 'new')
           AND COALESCE(s.bar_interval, '1d') != '1d'
           AND pt.signal_id NOT IN (
                SELECT pt2.signal_id FROM paper_trades pt2
                 WHERE pt2.side = 'sell'
                   AND pt2.status NOT IN ('rejected', 'canceled')
                   AND pt2.signal_id IS NOT NULL
            )
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# M6 (Sprint 3) — end-of-session flat assertion.
# Root cause of `stale_intraday_flatten_missed`: F2 opens an intraday outcome at
# entry and lets ONLY the EOD flatten close it; if that flatten is missed (crash,
# restart, schedule gap) OR the broker rejected/partially-filled the closing SELL,
# the position survives overnight and the outcome strands OPEN until a LATER
# session's bounded sweep closes it with the stale tag. The sweep is a band-aid;
# it never tells anyone the flatten silently failed THIS session. M6 adds the
# missing assertion: right after the flatten pass, verify every intraday-owned
# symbol is actually FLAT at the broker (the source of truth), and ALERT LOUDLY
# when one isn't — turning a silent overnight carry into a same-session alarm.

def assert_intraday_flat(
    client, symbols, *, alert_fn=None, dry_run: bool = False,
) -> dict:
    """Assert every intraday symbol just processed is FLAT at the broker.

    Reads the live broker position for each symbol (the source of truth, post
    in-run reservations) and flags any that is still non-flat (qty != 0) — an
    unflattened intraday carry. Fires `alert_fn(text)` once with the offenders
    (default: telegram). A clean session is silent (no alert).

    Returns {asserted, still_open: [{symbol, qty}], alerted: bool}. Skipped in
    dry-run (nothing was actually flattened) and when the broker can't report
    positions (no truth to assert against).
    """
    from monitoring import position_manager as pm_mod
    result = {"asserted": 0, "still_open": [], "alerted": False}
    if dry_run or not symbols:
        return result
    if not pm_mod.can_read_positions(client):
        return result
    still_open = []
    for sym in sorted(set(symbols)):
        result["asserted"] += 1
        pos = pm_mod.broker_position(client, sym)
        if pos is None:
            continue  # genuinely flat
        qty = pos.get("qty")
        if qty is None:
            continue  # broker returned junk → can't assert; don't false-alarm
        if abs(float(qty)) >= 1e-9:
            still_open.append({"symbol": sym, "qty": float(qty)})
    result["still_open"] = still_open
    if still_open:
        detail = ", ".join(f"{o['symbol']}={o['qty']:g}" for o in still_open)
        msg = (f"🚨 EOD intraday flat assertion FAILED: "
               f"{len(still_open)} intraday position(s) NOT flat after close-out "
               f"[{detail}]. Overnight gap risk + stale_intraday_flatten_missed "
               f"will follow. Investigate the flatten path.")
        log(msg, "ERROR")
        sender = alert_fn
        if sender is None:
            try:
                from monitoring.telegram_alerter import send_message as sender
            except Exception:
                sender = None
        if sender is not None:
            try:
                sender(msg)
                result["alerted"] = True
            except Exception as e:
                log(f"assert_intraday_flat: alert send failed: {e}", "WARNING")
    return result


def _open_outcome_for_signal(conn, signal_id) -> Optional[dict]:
    """The open outcome row for an entry signal_id, or None."""
    if signal_id is None:
        return None
    row = conn.execute(
        "SELECT signal_id, entry_ts, entry_price FROM outcomes "
        " WHERE signal_id=? AND status='open'",
        (signal_id,),
    ).fetchone()
    return dict(row) if row else None


def _intraday_bars_window(conn, symbol, after_ts, before_ts) -> List[dict]:
    """intraday_bars for symbol in (after_ts, before_ts], chronological."""
    try:
        rows = conn.execute(
            "SELECT ts_utc AS ts, high, low, close FROM intraday_bars "
            " WHERE symbol=? AND ts_utc>=? AND ts_utc<=? "
            " ORDER BY ts_utc ASC",
            (symbol, after_ts or "", before_ts or "9999"),
        ).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def _close_outcome_for_eod(conn, pos, exit_ts, exit_price) -> bool:
    """Close the open outcome behind an intraday EOD-flattened position.

    Computes MFE/MAE over the intraday_bars between entry and exit when
    available; persists exit_reason='eod_close'. Best-effort — a missing
    outcome or missing bars never aborts the flatten. Returns True if an
    outcome was closed.
    """
    outcome = _open_outcome_for_signal(conn, pos.get("signal_id"))
    if outcome is None:
        return False
    entry_ts = outcome["entry_ts"]
    entry_price = outcome["entry_price"]
    bars = _intraday_bars_window(conn, pos["symbol"], entry_ts, exit_ts)
    mfe, mae = excursion.compute_mfe_mae(
        bars, entry_price=entry_price,
        entry_ts=entry_ts, exit_ts=exit_ts, side="long",
    )
    db.close_outcome(
        conn, signal_id=int(pos["signal_id"]),
        exit_ts=str(exit_ts), exit_price=float(exit_price),
        exit_reason="eod_close", mfe_pct=mfe, mae_pct=mae,
    )
    return True


def _session_date(value) -> Optional[date]:
    """Coerce an ISO date/datetime string (or date) to a calendar date.

    Intraday entry_ts is the entry bar's ISO datetime (e.g.
    '2026-06-02T15:57:00'); the leading 10 chars are the session date. Returns
    None on anything unparseable so the caller can skip it safely.
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _open_intraday_outcomes(conn) -> List[dict]:
    """Every OPEN outcome whose signal is intraday (bar_interval != '1d'),
    carrying entry ts/price + symbol. Used by the stale-orphan safety net."""
    rows = conn.execute(
        """
        SELECT o.signal_id AS signal_id, o.entry_ts AS entry_ts,
               o.entry_price AS entry_price, s.symbol AS symbol,
               s.strategy_id AS strategy_id, s.bar_interval AS bar_interval
          FROM outcomes o
          JOIN signals  s ON s.id = o.signal_id
         WHERE o.status = 'open'
           AND COALESCE(s.bar_interval, '1d') != '1d'
        """
    ).fetchall()
    return [dict(r) for r in rows]


def sweep_stale_intraday_outcomes(
    conn,
    *,
    session_date: Optional[date] = None,
    now_iso: Optional[str] = None,
) -> dict:
    """Bounded safety net for intraday outcomes orphaned OPEN by a prior session.

    F2 opens an intraday outcome row at entry and deliberately lets ONLY the
    EOD flatten (close_intraday_positions) close it. If that flatten is missed
    (crash, restart, schedule gap), the outcome is stranded OPEN forever,
    polluting open-position state and eligibility stats.

    This sweep closes ONLY intraday outcomes whose entry session date is
    STRICTLY BEFORE `session_date` (the session the EOD flatten owns; callers
    pass the report's session date, default today's UTC date). Same-session
    intraday outcomes are left untouched so the EOD flatten retains sole
    ownership of the normal close — this never races the flatten for the
    current session.

    Each swept outcome is closed at the LAST available intraday bar's close
    (the best honest mark we have post-hoc), with MFE/MAE computed over
    entry..last-bar, and exit_reason=STALE_INTRADAY_EXIT_REASON. Outcomes with
    no usable bars after entry are skipped (no fabricated exit price).

    Idempotent: closed outcomes drop out of the OPEN query, so re-running is a
    no-op. Best-effort per row — one failure never aborts the rest.

    Returns {scanned, swept, skipped}.
    """
    boundary = session_date or datetime.now(timezone.utc).date()
    fallback_ts = now_iso or _utc_now_iso()
    candidates = _open_intraday_outcomes(conn)
    swept = 0
    skipped = 0
    for o in candidates:
        entry_date = _session_date(o.get("entry_ts"))
        # Staleness boundary: strictly prior to the current session. Same- or
        # future-session (clock skew) rows are NOT swept — the flatten owns them.
        if entry_date is None or entry_date >= boundary:
            skipped += 1
            continue
        symbol = o["symbol"]
        entry_ts = o["entry_ts"]
        # Last available bar from entry onward becomes the exit mark.
        bars = _intraday_bars_window(conn, symbol, entry_ts, None)
        if not bars:
            skipped += 1
            continue
        last_bar = bars[-1]
        exit_ts = str(last_bar.get("ts") or fallback_ts)
        try:
            exit_price = float(last_bar.get("close"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        mfe, mae = excursion.compute_mfe_mae(
            bars, entry_price=o["entry_price"],
            entry_ts=entry_ts, exit_ts=exit_ts, side="long",
        )
        try:
            db.close_outcome(
                conn, signal_id=int(o["signal_id"]),
                exit_ts=exit_ts, exit_price=exit_price,
                exit_reason=STALE_INTRADAY_EXIT_REASON,
                mfe_pct=mfe, mae_pct=mae,
            )
        except Exception as e:
            log(f"sweep_stale_intraday_outcomes: close failed for "
                f"{o.get('strategy_id')}/{symbol} sig {o.get('signal_id')}: {e}",
                "WARNING")
            skipped += 1
            continue
        log(f"STALE_INTRADAY_SWEEP closed orphan outcome sig "
            f"{o.get('signal_id')} ({o.get('strategy_id')}/{symbol}) entered "
            f"{entry_date} @ {o['entry_price']} -> {exit_price} "
            f"(reason={STALE_INTRADAY_EXIT_REASON})", "INFO")
        swept += 1
    return {"scanned": len(candidates), "swept": swept, "skipped": skipped}


def close_intraday_positions(
    *,
    conn=None,
    dry_run: Optional[bool] = None,
    client=None,
    client_factory: Callable = get_alpaca_client,
    submit_market_order_fn: Optional[Callable] = None,
    cancel_open_orders_fn: Optional[Callable] = None,
    settle_seconds: float = 2.0,
    flat_assert_alert_fn: Optional[Callable] = None,
) -> dict:
    """Walk open intraday-strategy positions and submit closing sells.

    Args:
      conn: optional open sqlite connection.
      dry_run: when True, logs would-be closes without submitting.
        Defaults to True when not in paper mode AND no broker is wired,
        else False (live submit).
      client: optional pre-built broker client (testing).
      client_factory: factory that returns a broker client; used when
        `client` is None and dry_run is False.
      submit_market_order_fn: optional override of the order submitter;
        receives (client, symbol, qty, side) and returns an order object
        with `.id`. Used by tests.

    Returns: {status, closed: [list of dicts], skipped: [], dry_run}.
    """
    if dry_run is None:
        dry_run = not is_paper_mode()

    # M1 (Sprint 3): start the flatten pass from the broker's settled truth so
    # the per-symbol in-run sell-reservation ledger nets THIS pass's flattens
    # (two strategies flattening the same shared symbol can't oversell it).
    from monitoring import position_manager as _pm_reset
    _pm_reset.reset_run_reservations()

    own_conn = conn is None
    if own_conn:
        conn = db.init_db()

    closed: List[dict] = []
    skipped: List[dict] = []
    try:
        positions = _open_intraday_buys(conn)
        if not positions:
            return {"status": "OK", "closed": [], "skipped": [],
                    "dry_run": dry_run, "scanned": 0}

        if not dry_run and client is None:
            try:
                client = client_factory()
            except Exception as e:
                log(f"close_intraday_positions: broker init failed: {e}",
                    "ERROR")
                return {"status": "ERROR", "closed": [], "skipped": [],
                        "dry_run": dry_run, "scanned": len(positions),
                        "error": str(e)[:200]}

        submitter = submit_market_order_fn
        if submitter is None:
            from monitoring.auto_trader import _submit_market_order
            submitter = _submit_market_order

        # Clear resting stops/entries for every symbol we're about to
        # flatten. Without this, Alpaca rejects the market sell because a
        # protective stop is still on the book (wash-trade) or is holding
        # the shares (insufficient qty). Best-effort: a failed sweep must
        # never abort the flatten.
        if not dry_run:
            canceller = cancel_open_orders_fn or _cancel_open_orders_for_symbols
            symbols = sorted({p["symbol"] for p in positions})
            canceled = 0
            try:
                canceled = canceller(client, symbols)
            except Exception as e:
                log(f"close_intraday_positions: order-cancel sweep failed "
                    f"(continuing to flatten): {e}", "WARNING")
            if canceled:
                log(f"close_intraday_positions: canceled {canceled} resting "
                    f"order(s) across {len(symbols)} symbol(s) before flatten",
                    "INFO")
                if settle_seconds > 0:
                    time.sleep(settle_seconds)

        for pos in positions:
            sid = pos["strategy_id"]
            sym = pos["symbol"]
            qty = int(pos["qty"] or 0)
            if qty < 1:
                skipped.append({
                    "reason": "qty<1",
                    "strategy_id": sid, "symbol": sym,
                    "buy_order_id": pos.get("alpaca_order_id"),
                })
                continue

            if dry_run:
                log(f"[DRY-RUN] EOD_CLOSE_INTRADAY SELL {qty} {sym} "
                    f"(closing buy {pos.get('alpaca_order_id')}) for {sid}",
                    "INFO")
                closed.append({
                    "action": "DRY_CLOSE_INTRADAY",
                    "strategy_id": sid, "symbol": sym, "qty": qty,
                    "buy_order_id": pos.get("alpaca_order_id"),
                    "signal_id": pos.get("signal_id"),
                })
                continue

            # M1 (Sprint 2): flatten through the single per-symbol reservation
            # layer so a symbol owned by multiple intraday strategies is sold
            # only down to broker-available (never past flat into a short) and
            # the second strategy's flatten for the same symbol is a clean SKIP
            # rather than an oversell / wash-trade reject. The symbol-wide
            # cancel sweep above already cleared resting stops; reconcile=False
            # here avoids a redundant per-symbol cancel pass.
            from monitoring import position_manager as pm_mod
            try:
                res = pm_mod.safe_submit_sell(
                    client, symbol=sym, requested_qty=qty,
                    submit_fn=submitter, reconcile=False,
                )
            except Exception as e:
                log(f"close_intraday_positions: SELL failed for {sid}/{sym}: {e}",
                    "ERROR")
                skipped.append({
                    "reason": f"order_error: {str(e)[:120]}",
                    "strategy_id": sid, "symbol": sym,
                    "buy_order_id": pos.get("alpaca_order_id"),
                })
                continue
            if res is None or res.get("action") != "SUBMITTED":
                skipped.append({
                    "reason": "no_available_qty",
                    "strategy_id": sid, "symbol": sym,
                    "buy_order_id": pos.get("alpaca_order_id"),
                    "available": (res or {}).get("available", 0),
                })
                continue
            order = res["order"]
            qty = res["qty"]

            exit_ts = str(getattr(order, "filled_at", None)
                          or getattr(order, "submitted_at", None)
                          or _utc_now_iso())
            exit_price = getattr(order, "filled_avg_price", None)
            db.record_paper_trade(conn, {
                "alpaca_order_id": str(getattr(order, "id", "")),
                "signal_id": pos["signal_id"],
                "strategy_id": sid, "symbol": sym, "side": "sell", "qty": qty,
                "order_type": "market",
                "submitted_at": str(getattr(order, "submitted_at",
                                             _utc_now_iso())),
                "fill_price": (float(exit_price)
                               if exit_price not in (None, "") else None),
                "status": str(getattr(order, "status", "submitted")),
                "notes": (f"auto-close intraday EOD; closing buy "
                          f"{pos.get('alpaca_order_id')}"),
            })
            log(f"EOD_CLOSE_INTRADAY SELL {qty} {sym} order: {order.id}",
                "SUCCESS")
            # Close the tracking outcome so intraday trades land in the
            # outcomes table with MFE/MAE + exit_reason='eod_close'. This is
            # the specific gap that left zero closed intraday outcomes.
            outcome_exit_price = (
                float(exit_price) if exit_price not in (None, "")
                else (float(pos["fill_price"])
                      if pos.get("fill_price") is not None else None)
            )
            outcome_closed = False
            if outcome_exit_price is not None:
                try:
                    outcome_closed = _close_outcome_for_eod(
                        conn, pos, exit_ts, outcome_exit_price,
                    )
                except Exception as e:
                    log(f"close_intraday_positions: outcome-close failed for "
                        f"{sid}/{sym} (flatten still recorded): {e}", "WARNING")
            closed.append({
                "action": "CLOSE_INTRADAY",
                "strategy_id": sid, "symbol": sym, "qty": qty,
                "order_id": str(order.id),
                "signal_id": pos["signal_id"],
                "outcome_closed": outcome_closed,
            })

        # M6 (Sprint 3) — end-of-session flat assertion. After the flatten pass,
        # verify every intraday symbol we just processed is actually FLAT at the
        # broker; alert loudly on any silent overnight carry (the
        # stale_intraday_flatten_missed precursor). Best-effort: a broker read
        # hiccup must never abort the close-out result.
        flat_assert = {"asserted": 0, "still_open": [], "alerted": False}
        try:
            if not dry_run:
                flat_assert = assert_intraday_flat(
                    client,
                    [p["symbol"] for p in positions],
                    alert_fn=flat_assert_alert_fn,
                    dry_run=dry_run,
                )
        except Exception as e:
            log(f"close_intraday_positions: flat assertion skipped "
                f"({type(e).__name__}: {e})", "WARNING")
        return {"status": "OK", "closed": closed, "skipped": skipped,
                "dry_run": dry_run, "scanned": len(positions),
                "flat_assert": flat_assert}
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Force dry-run regardless of paper-mode")
    args = parser.parse_args()

    result = close_intraday_positions(
        dry_run=True if args.dry_run else None,
    )
    print(json.dumps({
        "status": result["status"],
        "scanned": result.get("scanned", 0),
        "closed": len(result.get("closed", [])),
        "skipped": len(result.get("skipped", [])),
        "dry_run": result.get("dry_run"),
    }, indent=2))
