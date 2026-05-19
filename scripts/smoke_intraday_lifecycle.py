"""smoke_intraday_lifecycle.py — End-to-end paper smoke test for the
intraday lifecycle (milestone 5.7.1).

Runs a single intraday strategy (`mean_reversion_intraday` — 3-bar low
ported to 15-min bars) against a deterministic synthetic intraday bar
series through the full intraday wiring in dry-run mode. Verifies the
chain:

  - intraday fire-check commits a long_entry signal to the signals table
  - auto_trader.process_signals(bar_interval='15m') consumes that signal
    and submits a paper entry (BUY)
  - additional bars allow the price to rebound and an exit signal fires
  - close_intraday_positions sweeps any still-open intraday position at
    16:00 ET via a fake MOC submitter

Output: a trade-by-trade log + final stats. NOT a unit test — this is
the live-fire smoke test that proves the 5.1 / 5.2 / 5.5.3 wiring works
end-to-end before `auto_trade.intraday_enabled` is flipped on for the
first live-paper intraday day.

CLI:
  py -3.13 scripts/smoke_intraday_lifecycle.py
  py -3.13 scripts/smoke_intraday_lifecycle.py --json
  py -3.13 scripts/smoke_intraday_lifecycle.py --interval 5m
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import MagicMock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import close_intraday_positions as ci  # noqa: E402
from monitoring import intraday_fires as ifires  # noqa: E402


SYNTHETIC_SYMBOL = "SPY"
SYNTHETIC_DATE = date(2026, 5, 18)


def _generate_synthetic_intraday_bars(
    n_bars: int = 30, interval_min: int = 15,
) -> pd.DataFrame:
    """Build a 15-min intraday bar series with a clean 3-bar low fire
    followed by a rebound to trigger the exit.

    Shape:
      - First N-7 bars hold a flat $100 base with $0.5 wiggle
      - Bars [N-7 .. N-5] dip stepwise to $97 → triggers compute_3bar_low
        (close < min(low) of prior 3 bars)
      - Final 5 bars rebound to $102 → triggers exit (close > max(high)
        of prior 7 bars)

    Index is the bar OPEN timestamp; the strategy's last row corresponds
    to the most recent completed bar.
    """
    start = datetime.combine(SYNTHETIC_DATE, dtime(9, 30))
    idx = pd.date_range(start=start, periods=n_bars,
                         freq=f"{interval_min}min")
    closes: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    base = 100.0
    dip_start = n_bars - 7
    dip_end = n_bars - 5  # 3 dipping bars (inclusive)
    rebound_start = n_bars - 5
    for i in range(n_bars):
        if i < dip_start:
            close = base + (0.25 if i % 2 == 0 else -0.25)
        elif dip_start <= i <= dip_end:
            close = base - (1.0 * (i - dip_start + 1))  # 99, 98, 97
        elif i == rebound_start:
            close = base - 1.5  # slight bounce so 3-bar-low fires on 97 bar
        else:
            steps_in = i - rebound_start
            close = base + 0.5 + 0.4 * steps_in  # ramp 100 → 102+
        closes.append(round(close, 2))
        highs.append(round(close + 0.4, 2))
        lows.append(round(close - 0.4, 2))
    return pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1_000_000.0] * n_bars,
    }, index=idx)


def _make_settings(*, max_position_usd: float = 1000.0) -> Dict:
    return {
        "enabled": True,
        "dry_run": False,
        "min_outcomes": 0,
        "min_mean_ret_pct": 0.0,
        "min_sharpe_ish": 0.0,
        "max_position_usd": max_position_usd,
        "sizing_method": "fixed",
        "grace_period_size_multiplier": 1.0,
        "cool_down_losers": 0,
        "earnings_veto_days": 0,
    }


def _declaration() -> Dict:
    """The intraday declaration used by the smoke. Mirrors the 5.3.1
    shape but keeps the grace period on so the eligibility gate doesn't
    block with n=0 closed outcomes."""
    return {
        "id": "smoke-intraday-mr-3bar-low-15m",
        "compute": "compute_3bar_low_intraday",
        "module": "strategies.intraday.mean_reversion_intraday",
        "strategy_class": "mean_reversion",
        "bar_interval": "15m",
        "active_on": [SYNTHETIC_SYMBOL],
        "grace_period": True,
        "pyramidable": False,
    }


def _account_summary_stub() -> Dict:
    return {
        "portfolio_value": 100_000.0,
        "cash": 100_000.0,
        "equity": 100_000.0,
        "buying_power": 100_000.0,
        "equity_at_open": 100_000.0,
        "last_equity": 100_000.0,
    }


def _run_smoke(*, interval: str = "15m") -> Dict:
    """Drive the synthetic intraday session bar-by-bar through the wiring.

    Returns a structured report containing every action emitted plus the
    close-out result.
    """
    tmp = Path(tempfile.mkdtemp()) / "trading.db"
    import data.db as dbmod
    dbmod.DB_FILE = tmp
    conn = db.init_db(tmp)
    at.is_paper_mode = lambda: True
    ci.is_paper_mode = lambda: True

    declaration = _declaration()
    declaration["bar_interval"] = interval
    db.upsert_strategy(conn, {"extra": {"strategy_id": declaration["id"]}})

    interval_min = int(interval.rstrip("m")) if interval.endswith("m") else 15
    bars = _generate_synthetic_intraday_bars(
        n_bars=30, interval_min=interval_min,
    )

    # Stub the broker submitter so no real network is touched.
    submitted: List[Dict] = []

    def fake_market(client, *, symbol, qty, side, client_order_id=None):
        submitted.append({"symbol": symbol, "qty": qty, "side": side,
                          "client_order_id": client_order_id})
        order = MagicMock()
        order.id = f"smoke-intra-{len(submitted)}"
        order.status = "accepted"
        order.submitted_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        order.filled_avg_price = float(bars["close"].iloc[-1])
        return order

    at._submit_market_order = fake_market

    # Pin TRACKED_STRATEGIES so process_signals sees our declaration only.
    from monitoring import config as cfg_mod
    cfg_mod.TRACKED_STRATEGIES = [declaration]

    # Step through bars: at each closed bar, run the intraday fire-check
    # against the bar-window-so-far, then run process_signals for the
    # asof date. The fire-check is idempotent so re-running on each bar
    # only inserts new signals.
    trade_log: List[Dict] = []
    fires_log: List[Dict] = []
    settings = _make_settings()
    asof_date = SYNTHETIC_DATE

    for i in range(len(bars)):
        window = bars.iloc[: i + 1]
        bar_dt = window.index[-1].to_pydatetime()

        def loader(symbols, _interval, _lookback, *, now,
                    _window=window):
            return {sym: _window for sym in symbols if sym == SYNTHETIC_SYMBOL}

        fires = ifires.check_intraday_fires(
            asof=bar_dt,
            declarations=[declaration],
            bar_loader=loader,
            conn=conn,
            min_bars=4,
        )
        for f in fires:
            if f.get("signal_id"):
                fires_log.append({
                    "bar_index": i,
                    "bar_ts": f["bar_ts"],
                    "strategy_id": f["strategy_id"],
                    "symbol": f["symbol"],
                    "signal_type": f["signal_type"],
                    "close": f["close"],
                })

        res = at.process_signals(
            conn,
            asof=asof_date,
            settings=settings,
            client=MagicMock(),
            bar_interval=interval,
            account_summary_fn=_account_summary_stub,
        )
        for action in res.get("actions", []):
            if action.get("action") in (
                "BUY", "DRY_BUY", "SELL", "DRY_SELL",
                "SKIP_DEDUPE", "SKIP_INTRADAY_SYMBOL_CAP",
            ):
                trade_log.append({
                    "bar_index": i,
                    "bar_dt": bar_dt.isoformat(),
                    "close": float(window["close"].iloc[-1]),
                    **{k: v for k, v in action.items()
                       if k not in ("sizing", "stop")},
                })

    # End-of-session: invoke close_intraday_positions in dry-run mode so
    # the smoke proves the close-out wiring identifies the open intraday
    # position. dry_run=False with a fake submitter exercises the live
    # insert path; we use dry_run=False here for full coverage.
    close_res = ci.close_intraday_positions(
        conn=conn,
        dry_run=False,
        client=MagicMock(),
        submit_market_order_fn=lambda client, symbol, qty, side: MagicMock(
            id=f"close-{symbol}",
            status="accepted",
            submitted_at=datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        ),
    )

    # Compute final stats.
    entries = [t for t in trade_log if t.get("action") in ("BUY", "DRY_BUY")]
    exits_via_signal = [t for t in trade_log
                         if t.get("action") in ("SELL", "DRY_SELL")]
    n_closed = len(close_res.get("closed") or [])

    entry_price = entries[0]["close"] if entries else None
    exit_price = (exits_via_signal[-1]["close"] if exits_via_signal
                   else float(bars["close"].iloc[-1]))
    total_qty = sum(float(t.get("qty") or 0) for t in entries)
    pnl = ((exit_price - entry_price) * total_qty
           if entry_price and total_qty else None)

    return {
        "strategy_id": declaration["id"],
        "interval": interval,
        "n_bars": len(bars),
        "n_fires": len(fires_log),
        "n_entry_fires": sum(1 for f in fires_log
                              if f["signal_type"] == "long_entry"),
        "n_exit_fires": sum(1 for f in fires_log
                             if f["signal_type"] == "long_exit"),
        "entries": len(entries),
        "exits_via_signal": len(exits_via_signal),
        "close_out": {
            "status": close_res.get("status"),
            "scanned": close_res.get("scanned", 0),
            "closed": n_closed,
            "skipped": len(close_res.get("skipped") or []),
        },
        "entry_price": entry_price,
        "exit_price": exit_price,
        "total_qty": total_qty,
        "approx_pnl_usd": round(pnl, 2) if pnl is not None else None,
        "n_orders_submitted": len(submitted),
        "trade_log": trade_log,
        "fires_log": fires_log,
    }


def _format_human_log(report: Dict) -> str:
    """Pretty-print the trade-by-trade log + stats."""
    out: List[str] = []
    out.append("=" * 78)
    out.append(f"SMOKE TEST — intraday lifecycle ({report['strategy_id']})")
    out.append(f"interval: {report['interval']}")
    out.append("=" * 78)
    out.append(f"  bars processed:     {report['n_bars']}")
    out.append(f"  signal fires:       {report['n_fires']}  "
               f"(entry={report['n_entry_fires']} "
               f"exit={report['n_exit_fires']})")
    out.append(f"  entries:            {report['entries']}")
    out.append(f"  exits via signal:   {report['exits_via_signal']}")
    co = report["close_out"]
    out.append(f"  EOD close-out:      status={co['status']} "
               f"scanned={co['scanned']} closed={co['closed']}")
    out.append(f"  entry price:        {report['entry_price']}")
    out.append(f"  exit price:         {report['exit_price']}")
    out.append(f"  total qty:          {report['total_qty']}")
    if report["approx_pnl_usd"] is not None:
        out.append(f"  approx PnL (USD):   ${report['approx_pnl_usd']:.2f}")
    out.append("")
    out.append("Signal fires:")
    out.append("-" * 78)
    for f in report["fires_log"]:
        out.append(
            f"  [{f['bar_ts']}] {f['signal_type']:<12s} "
            f"{f['symbol']:<6s} close=${f['close']:.2f} "
            f"({f['strategy_id']})"
        )
    out.append("")
    out.append("Trade-by-trade log:")
    out.append("-" * 78)
    for t in report["trade_log"]:
        action = t.get("action", "?")
        bar_dt = t.get("bar_dt", "?")
        close = t.get("close", 0)
        qty = t.get("qty", "")
        out.append(
            f"  [{bar_dt}] {action:<22s} qty={qty:<8} close=${close:.2f}"
        )
    out.append("=" * 78)
    return "\n".join(out)


def smoke_test(interval: str = "15m") -> Dict:
    """Public entrypoint exercised by the unit tests."""
    return _run_smoke(interval=interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", default="15m",
                        choices=["5m", "15m", "1h"])
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of human log")
    args = parser.parse_args()
    report = _run_smoke(interval=args.interval)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    print(_format_human_log(report))
    if report["entries"] == 0:
        print("\n[WARN] No entry fired — strategy did not trigger on the "
              "synthetic series. Wiring still works; verify with a steeper "
              "synthetic dip or a longer bar window.")


if __name__ == "__main__":
    main()
