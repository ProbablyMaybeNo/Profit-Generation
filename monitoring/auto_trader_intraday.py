"""
auto_trader_intraday.py — Trigger the auto-trader pipeline for intraday
signals committed by `monitoring.intraday_fires`.

Reads `auto_trade.intraday_enabled` and `auto_trade.intraday_intervals`
from settings.json. When intraday_enabled=false (the safe default until
5.2.3 flips it), no orders are submitted — intraday signals still record
to the signals table via 5.1.2, but the auto_trader path is observe-only.

Invoked from `schedulers/run_intraday.bat` immediately after
`monitoring.intraday_fires` completes.
"""

from __future__ import annotations

from datetime import date
from typing import Callable, List, Optional

from config.utils import load_settings, log
from data import db
from monitoring import auto_trader as at


DEFAULT_INTRADAY_INTERVALS: List[str] = ["15m"]


def _intraday_config(settings: dict) -> dict:
    auto_trade = settings.get("auto_trade") or {}
    # Build the SAME flattened/merged config the EOD path uses so intraday
    # honors the top-level stops/kelly/trailing_stop/risk blocks (without
    # them, intraday silently ran more conservative: max_open_per_strategy
    # defaulted to 3, Kelly fell back, stops were weaker).
    return {
        "enabled": bool(auto_trade.get("intraday_enabled", False)),
        "intervals": list(auto_trade.get("intraday_intervals",
                                          DEFAULT_INTRADAY_INTERVALS)),
        "auto_trade": at.merge_config(settings),
    }


def process_intraday(
    *,
    asof: Optional[date] = None,
    settings: Optional[dict] = None,
    conn=None,
    process_signals_fn: Callable = at.process_signals,
) -> dict:
    """Run the auto_trader pipeline once per configured intraday interval.

    Returns {status, intraday_enabled, intervals, results} where `results`
    is a list of per-interval `process_signals` return values.
    """
    settings = settings if settings is not None else load_settings()
    cfg = _intraday_config(settings)
    asof = asof or date.today()

    if not cfg["enabled"]:
        return {
            "status": "DISABLED_INTRADAY",
            "intraday_enabled": False,
            "intervals": cfg["intervals"],
            "asof": asof.isoformat(),
            "results": [],
        }

    own_conn = conn is None
    if own_conn:
        conn = db.init_db()

    try:
        results = []
        for interval in cfg["intervals"]:
            log(f"auto_trader_intraday: processing interval={interval}", "INFO")
            res = process_signals_fn(
                conn,
                asof=asof,
                settings=cfg["auto_trade"],
                bar_interval=interval,
            )
            res_with_interval = dict(res)
            res_with_interval["bar_interval"] = interval
            results.append(res_with_interval)
        return {
            "status": "OK",
            "intraday_enabled": True,
            "intervals": cfg["intervals"],
            "asof": asof.isoformat(),
            "results": results,
        }
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", type=str, default=None,
                        help="ISO date override (default: today)")
    parser.add_argument("--no-market-check", action="store_true",
                        help="Skip market_is_open check (testing / off-hours)")
    args = parser.parse_args()

    if not args.no_market_check:
        from config.utils import market_is_open
        if not market_is_open():
            print(json.dumps({"market_open": False, "scanned": False}))
            sys.exit(0)

    asof = date.fromisoformat(args.asof) if args.asof else None
    result = process_intraday(asof=asof)
    print(json.dumps({
        "status": result["status"],
        "intraday_enabled": result["intraday_enabled"],
        "intervals": result["intervals"],
        "actions_total": sum(len(r.get("actions") or []) for r in result["results"]),
    }, indent=2))
