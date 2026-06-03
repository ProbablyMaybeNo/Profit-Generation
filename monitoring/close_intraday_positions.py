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
from datetime import datetime, timezone
from typing import Callable, List, Optional

from config.utils import get_alpaca_client, is_paper_mode, log
from data import db


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
           AND pt.id NOT IN (
                SELECT pt2.signal_id FROM paper_trades pt2
                 WHERE pt2.side = 'sell'
                   AND pt2.status NOT IN ('rejected', 'canceled')
                   AND pt2.signal_id = pt.signal_id
            )
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def close_intraday_positions(
    *,
    conn=None,
    dry_run: Optional[bool] = None,
    client=None,
    client_factory: Callable = get_alpaca_client,
    submit_market_order_fn: Optional[Callable] = None,
    cancel_open_orders_fn: Optional[Callable] = None,
    settle_seconds: float = 2.0,
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

            try:
                order = submitter(client, symbol=sym, qty=qty, side="sell")
            except Exception as e:
                log(f"close_intraday_positions: SELL failed for {sid}/{sym}: {e}",
                    "ERROR")
                skipped.append({
                    "reason": f"order_error: {str(e)[:120]}",
                    "strategy_id": sid, "symbol": sym,
                    "buy_order_id": pos.get("alpaca_order_id"),
                })
                continue

            db.record_paper_trade(conn, {
                "alpaca_order_id": str(getattr(order, "id", "")),
                "signal_id": pos["signal_id"],
                "strategy_id": sid, "symbol": sym, "side": "sell", "qty": qty,
                "order_type": "market",
                "submitted_at": str(getattr(order, "submitted_at",
                                             _utc_now_iso())),
                "status": str(getattr(order, "status", "submitted")),
                "notes": (f"auto-close intraday EOD; closing buy "
                          f"{pos.get('alpaca_order_id')}"),
            })
            log(f"EOD_CLOSE_INTRADAY SELL {qty} {sym} order: {order.id}",
                "SUCCESS")
            closed.append({
                "action": "CLOSE_INTRADAY",
                "strategy_id": sid, "symbol": sym, "qty": qty,
                "order_id": str(order.id),
                "signal_id": pos["signal_id"],
            })

        return {"status": "OK", "closed": closed, "skipped": skipped,
                "dry_run": dry_run, "scanned": len(positions)}
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
