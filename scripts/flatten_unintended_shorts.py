"""
flatten_unintended_shorts.py — Sprint 2 / M2 unintended-short cover tool.

Every strategy in this system is long-only. A broker position with qty<0 is an
ACCIDENT — produced by the multi-strategy oversell bug M1 fixes (several
intraday strategies each flattening the same symbol, selling past flat into a
short). Sprint 2 found ~-$62k of such shorts across 10 mega-caps (AAPL, AMZN,
AVGO, COIN, GOOGL, META, MSFT, NFLX, XLE, XLK).

This tool:
  1. Reads live broker positions.
  2. Lists every qty<0 position with the exact buy-to-cover quantity
     (abs(qty)) needed to reach flat.
  3. DRY-RUN by default — prints the plan, places NO orders.
  4. With --execute, submits buy-to-cover orders sized to flatten EXACTLY
     (never crossing zero into a long), routed through the M1 reservation
     layer (monitoring.position_manager.safe_submit_buy_to_cover) so the cover
     orders don't themselves conflict with resting orders.

Safe to re-run: a symbol already flat/long is skipped (SKIP_NOT_SHORT). Long
and flat symbols are never touched.

Usage (operator, at the next market open):
  py -3.13 -m scripts.flatten_unintended_shorts            # dry-run (default)
  py -3.13 -m scripts.flatten_unintended_shorts --execute  # place cover buys
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import get_alpaca_client, log  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402


def _all_positions(client) -> List:
    getter = (getattr(client, "get_all_positions", None)
              or getattr(client, "list_positions", None))
    if getter is None:
        raise RuntimeError("alpaca client exposes neither get_all_positions "
                           "nor list_positions")
    return list(getter() or [])


def detect_shorts(client) -> List[Dict]:
    """Return [{symbol, qty (negative float), cover_qty (int)}] for every
    broker position with qty<0. Pure read — places nothing."""
    shorts: List[Dict] = []
    for p in _all_positions(client):
        sym = pm._attr(p, "symbol")
        qty = pm._strict_float(pm._attr(p, "qty"))
        if sym is None or qty is None or qty >= 0:
            continue
        cover = int(abs(qty))
        if cover < 1:
            continue
        shorts.append({"symbol": sym, "qty": qty, "cover_qty": cover})
    shorts.sort(key=lambda s: s["symbol"])
    return shorts


def flatten_unintended_shorts(
    *,
    client=None,
    client_factory: Callable = get_alpaca_client,
    execute: bool = False,
    submit_fn: Optional[Callable] = None,
) -> Dict:
    """Detect unintended shorts and (when execute=True) buy-to-cover to flat.

    Args:
      client: pre-built broker client (tests inject a fake).
      client_factory: builds a client when `client` is None.
      execute: False (default) = dry-run, lists shorts + cover qty, no orders.
               True = submit buy-to-cover via the M1 reservation layer.
      submit_fn: order submitter (client, symbol=, qty=, side=) for tests;
                 defaults to auto_trader._submit_market_order.

    Returns {status, dry_run, shorts: [...], covered: [...], skipped: [...]}.
    """
    if client is None:
        client = client_factory()

    if submit_fn is None:
        from monitoring.auto_trader import _submit_market_order
        submit_fn = _submit_market_order

    shorts = detect_shorts(client)
    covered: List[Dict] = []
    skipped: List[Dict] = []

    if not shorts:
        log("flatten_unintended_shorts: no unintended shorts found — account "
            "is flat/long on every symbol.", "INFO")
        return {"status": "OK", "dry_run": not execute, "shorts": [],
                "covered": [], "skipped": []}

    total_cover = sum(s["cover_qty"] for s in shorts)
    log(f"flatten_unintended_shorts: detected {len(shorts)} unintended short "
        f"position(s), {total_cover} total shares to cover:", "WARNING")
    for s in shorts:
        log(f"  SHORT {s['symbol']}: qty={s['qty']} → BUY {s['cover_qty']} "
            f"to flatten", "WARNING")

    if not execute:
        log("flatten_unintended_shorts: DRY-RUN — no orders placed. Re-run "
            "with --execute to cover.", "INFO")
        return {"status": "OK", "dry_run": True, "shorts": shorts,
                "covered": [], "skipped": []}

    for s in shorts:
        sym = s["symbol"]
        try:
            res = pm.safe_submit_buy_to_cover(
                client, symbol=sym, submit_fn=submit_fn,
            )
        except Exception as e:
            log(f"flatten_unintended_shorts: cover failed for {sym}: {e}",
                "ERROR")
            skipped.append({"symbol": sym, "reason": str(e)[:160]})
            continue
        if res.get("action") == "COVERED":
            order = res.get("order")
            log(f"COVERED {sym}: bought {res['qty']} (order "
                f"{getattr(order, 'id', '?')})", "SUCCESS")
            covered.append({"symbol": sym, "qty": res["qty"],
                            "order_id": str(getattr(order, "id", ""))})
        else:
            skipped.append({"symbol": sym, "reason": res.get("action"),
                            "position_qty": res.get("position_qty")})

    return {"status": "OK", "dry_run": False, "shorts": shorts,
            "covered": covered, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(
        description="Detect and (with --execute) cover unintended short "
                    "positions to flat.")
    parser.add_argument(
        "--execute", action="store_true",
        help="Place buy-to-cover orders. WITHOUT this flag the tool is a "
             "dry-run and places NO orders.")
    args = parser.parse_args()

    result = flatten_unintended_shorts(execute=args.execute)
    print(json.dumps({
        "status": result["status"],
        "dry_run": result["dry_run"],
        "shorts": result["shorts"],
        "covered": result["covered"],
        "skipped": result["skipped"],
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
