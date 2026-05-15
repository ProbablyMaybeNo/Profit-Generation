"""
auto_trader.py — Submit Alpaca paper market orders on EOD '1d' fires from
strategies that have demonstrated edge in the closed-outcomes record.

Triple-gated for safety:
  1. settings.auto_trade.enabled must be true (default false — opt-in)
  2. settings.auto_trade.dry_run controls whether orders actually submit
     (default true — switch to false only when you've watched dry-runs
     for several days and you're happy with what they would have done)
  3. is_paper_mode() must return True before any submission

Dedupe per signal_id, side. A given signal opens / closes at most one
paper_trades row regardless of how many times the pipeline runs.

CLI:
  py -3.13 -m monitoring.auto_trader                 # honour settings as-is
  py -3.13 -m monitoring.auto_trader --dry-run       # force dry-run
  py -3.13 -m monitoring.auto_trader --enable        # override enabled=false
  py -3.13 -m monitoring.auto_trader --asof 2026-05-14
"""

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import get_alpaca_client, is_paper_mode, load_settings, log  # noqa: E402
from data import db  # noqa: E402

DEFAULT_SETTINGS = {
    "enabled": False,
    "dry_run": True,
    "min_outcomes": 30,
    "min_mean_ret_pct": 0.0,
    "min_sharpe_ish": 0.10,
    "max_position_usd": 1000,
    "skip_intraday_signals": True,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _config() -> dict:
    s = load_settings().get("auto_trade", {})
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in s.items() if not k.startswith("_")})
    return out


def _is_eligible(conn, strategy_id: str, settings: dict) -> tuple:
    """Return (ok: bool, stats: dict). Stats always populated for logging."""
    rows = conn.execute(
        "SELECT o.return_pct FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval='1d' AND s.strategy_id=?",
        (strategy_id,),
    ).fetchall()
    rets = [r["return_pct"] for r in rows]
    n = len(rets)
    stats = {"n": n, "mean": 0.0, "sharpe": 0.0}
    if n == 0:
        return False, stats
    mean = sum(rets) / n
    sd = statistics.stdev(rets) if n > 1 else 0.0
    sharpe = (mean / sd) if sd > 0 else 0.0
    stats["mean"] = round(mean, 4)
    stats["sharpe"] = round(sharpe, 4)
    if n < settings.get("min_outcomes", 30):
        return False, stats
    if mean < settings.get("min_mean_ret_pct", 0.0):
        return False, stats
    if sharpe < settings.get("min_sharpe_ish", 0.10):
        return False, stats
    return True, stats


def _already_traded(conn, signal_id: int, side: str) -> bool:
    """Did we already submit this side for this signal? (Excluding rejected/canceled.)"""
    row = conn.execute(
        "SELECT 1 FROM paper_trades WHERE signal_id=? AND side=? "
        "  AND status NOT IN ('canceled', 'rejected')",
        (signal_id, side),
    ).fetchone()
    return row is not None


def _open_buy_for_pair(conn, strategy_id: str, symbol: str):
    """Most recent paper_trades buy for (strategy, symbol) that hasn't been closed."""
    row = conn.execute(
        "SELECT * FROM paper_trades WHERE strategy_id=? AND symbol=? AND side='buy' "
        "  AND status IN ('filled', 'partially_filled', 'accepted', 'new') "
        "ORDER BY submitted_at DESC LIMIT 1",
        (strategy_id, symbol),
    ).fetchone()
    if row is None:
        return None
    later_sell = conn.execute(
        "SELECT 1 FROM paper_trades WHERE strategy_id=? AND symbol=? AND side='sell' "
        "  AND submitted_at > ? "
        "  AND status NOT IN ('canceled', 'rejected') LIMIT 1",
        (strategy_id, symbol, row["submitted_at"]),
    ).fetchone()
    if later_sell is not None:
        return None
    return row


def _calc_qty(price: Optional[float], max_position_usd: float) -> int:
    if price is None or price <= 0:
        return 0
    return int(max_position_usd // price)


def _submit_market_order(client, *, symbol: str, qty: int, side: str):
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    req = MarketOrderRequest(
        symbol=symbol, qty=qty,
        side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    return client.submit_order(req)


def _process_entry(conn, client, settings: dict, sig, dry_run: bool) -> dict:
    sid, sym = sig["strategy_id"], sig["symbol"]
    eligible, stats = _is_eligible(conn, sid, settings)
    if not eligible:
        return {"action": "SKIP_INELIGIBLE", "strategy_id": sid, "symbol": sym,
                "reason": "fails edge thresholds", "stats": stats}
    if _already_traded(conn, sig["id"], "buy"):
        return {"action": "SKIP_DUPLICATE", "strategy_id": sid, "symbol": sym,
                "signal_id": sig["id"]}
    qty = _calc_qty(sig["close"], float(settings.get("max_position_usd", 1000)))
    if qty < 1:
        return {"action": "SKIP_PRICE", "strategy_id": sid, "symbol": sym,
                "price": sig["close"], "max_usd": settings.get("max_position_usd")}

    if dry_run:
        log(f"[DRY-RUN] BUY {qty} {sym} @ ~${sig['close']:.2f} "
            f"(~${qty * sig['close']:.2f}) for {sid}", "INFO")
        return {"action": "DRY_BUY", "strategy_id": sid, "symbol": sym,
                "qty": qty, "price": sig["close"], "signal_id": sig["id"]}

    try:
        order = _submit_market_order(client, symbol=sym, qty=qty, side="buy")
    except Exception as e:
        log(f"order submit failed for {sid}/{sym}: {e}", "ERROR")
        return {"action": "ERROR", "strategy_id": sid, "symbol": sym,
                "error": str(e)[:200]}

    db.record_paper_trade(conn, {
        "alpaca_order_id": str(getattr(order, "id", "")),
        "signal_id": sig["id"],
        "strategy_id": sid, "symbol": sym, "side": "buy", "qty": qty,
        "order_type": "market",
        "submitted_at": str(getattr(order, "submitted_at", _utc_now())),
        "status": str(getattr(order, "status", "submitted")),
        "notes": f"auto-entry on bar_ts={sig['bar_ts']}",
    })
    log(f"BUY {qty} {sym} order submitted: {order.id}", "SUCCESS")
    return {"action": "BUY", "strategy_id": sid, "symbol": sym, "qty": qty,
            "order_id": str(order.id), "signal_id": sig["id"]}


def _process_exit(conn, client, settings: dict, sig, dry_run: bool) -> dict:
    sid, sym = sig["strategy_id"], sig["symbol"]
    if _already_traded(conn, sig["id"], "sell"):
        return {"action": "SKIP_DUPLICATE", "strategy_id": sid, "symbol": sym,
                "signal_id": sig["id"]}
    open_buy = _open_buy_for_pair(conn, sid, sym)
    if open_buy is None:
        return {"action": "SKIP_NO_POSITION", "strategy_id": sid, "symbol": sym}
    qty = int(open_buy["qty"])

    if dry_run:
        log(f"[DRY-RUN] SELL {qty} {sym} (close position from "
            f"{open_buy['submitted_at'][:10]}) for {sid}", "INFO")
        return {"action": "DRY_SELL", "strategy_id": sid, "symbol": sym,
                "qty": qty, "signal_id": sig["id"],
                "from_order_id": open_buy["alpaca_order_id"]}

    try:
        order = _submit_market_order(client, symbol=sym, qty=qty, side="sell")
    except Exception as e:
        log(f"order submit failed for {sid}/{sym}: {e}", "ERROR")
        return {"action": "ERROR", "strategy_id": sid, "symbol": sym,
                "error": str(e)[:200]}

    db.record_paper_trade(conn, {
        "alpaca_order_id": str(getattr(order, "id", "")),
        "signal_id": sig["id"],
        "strategy_id": sid, "symbol": sym, "side": "sell", "qty": qty,
        "order_type": "market",
        "submitted_at": str(getattr(order, "submitted_at", _utc_now())),
        "status": str(getattr(order, "status", "submitted")),
        "notes": f"auto-exit on bar_ts={sig['bar_ts']}; "
                 f"closing buy {open_buy['alpaca_order_id']}",
    })
    log(f"SELL {qty} {sym} order submitted: {order.id}", "SUCCESS")
    return {"action": "SELL", "strategy_id": sid, "symbol": sym, "qty": qty,
            "order_id": str(order.id), "signal_id": sig["id"]}


def process_signals(
    conn,
    *,
    asof: Optional[date] = None,
    settings: Optional[dict] = None,
    client=None,
    client_factory: Callable = get_alpaca_client,
) -> dict:
    """Walk today's '1d' signals; submit Alpaca paper market orders per eligibility + dedupe.

    Returns {status, dry_run, asof, actions}. Status 'DISABLED' / 'BLOCKED_LIVE_MODE'
    when guard rails trigger; 'OK' otherwise.
    """
    settings = settings if settings is not None else _config()
    if not settings.get("enabled", False):
        return {"status": "DISABLED", "dry_run": settings.get("dry_run", True),
                "asof": (asof or date.today()).isoformat(), "actions": []}
    if not is_paper_mode():
        log("auto_trader: BLOCKED — not in paper mode", "ERROR")
        return {"status": "BLOCKED_LIVE_MODE", "dry_run": True,
                "asof": (asof or date.today()).isoformat(), "actions": []}

    asof = asof or date.today()
    dry_run = bool(settings.get("dry_run", True))
    if client is None and not dry_run:
        client = client_factory()

    sigs = conn.execute(
        "SELECT id, ts, bar_ts, bar_interval, strategy_id, symbol, signal_type, close "
        "  FROM signals "
        " WHERE bar_ts = ? AND bar_interval = '1d' "
        " ORDER BY id ASC",
        (asof.isoformat(),),
    ).fetchall()

    actions: List[dict] = []
    for sig in sigs:
        if sig["signal_type"] == "long_entry":
            actions.append(_process_entry(conn, client, settings, sig, dry_run))
        elif sig["signal_type"] == "long_exit":
            actions.append(_process_exit(conn, client, settings, sig, dry_run))

    return {"status": "OK", "dry_run": dry_run, "asof": asof.isoformat(),
            "actions": actions}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", help="ISO date (default: today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Force dry-run regardless of settings.dry_run")
    parser.add_argument("--enable", action="store_true",
                        help="Override settings.enabled=false (use with care)")
    args = parser.parse_args()

    asof = date.fromisoformat(args.asof) if args.asof else date.today()
    settings = _config()
    if args.dry_run:
        settings["dry_run"] = True
    if args.enable:
        settings["enabled"] = True

    conn = db.init_db()
    try:
        result = process_signals(conn, asof=asof, settings=settings)
        print(json.dumps(result, indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
