"""
intraday_monitor.py — Synthesize today's in-progress daily bar from minute
data, append to historical daily bars, run each tracked strategy's
compute_fn, and persist projected long_entry / long_exit fires with
bar_interval='1d-intraday' so they stay distinct from EOD '1d' signals.

Logs new fires to console + logs/intraday_alerts.log so the user can
mirror them into TradingView paper trades while the session is still open.
"""

import argparse
import importlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import load_bars  # noqa: E402
from config.utils import market_is_open, log  # noqa: E402
from data import db  # noqa: E402
from monitoring.config import TRACKED_CRYPTO  # noqa: E402

INTRADAY_INTERVAL = "1d-intraday"
ALERT_LOG = ROOT / "logs" / "intraday_alerts.log"
DEFAULT_LOOKBACK_DAYS = 90
MIN_BARS_REQUIRED = 25

# Modules searched for compute_fn names from the strategies registry.
COMPUTE_FN_MODULES = [
    "strategies.mean_reversion.botnet101",
]


def _resolve_compute_fn(fn_name: str) -> Optional[Callable]:
    for mod_path in COMPUTE_FN_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except Exception:
            continue
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            return fn
    return None


def synthesize_today_bar(
    symbol: str,
    asof: Optional[datetime] = None,
    minute_loader: Callable = load_bars,
) -> Optional[pd.Series]:
    """Aggregate today's minute bars into a single daily bar."""
    asof = asof or datetime.now()
    today_str = asof.date().isoformat()
    end_str = (asof + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        bars_dict = minute_loader(
            [symbol], start=today_str, end=end_str,
            interval="1m", source="alpaca",
        )
    except Exception as e:
        log(f"intraday minute fetch failed for {symbol}: {e}", "WARNING")
        return None
    if not bars_dict or symbol not in bars_dict or bars_dict[symbol].empty:
        return None
    df = bars_dict[symbol]
    return pd.Series({
        "open":   float(df["open"].iloc[0]),
        "high":   float(df["high"].max()),
        "low":    float(df["low"].min()),
        "close":  float(df["close"].iloc[-1]),
        "volume": float(df["volume"].sum()),
    }, name=pd.Timestamp(asof.date()))


def blended_daily_history(
    symbol: str,
    asof: datetime,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    daily_loader: Callable = load_bars,
    minute_loader: Callable = load_bars,
) -> Optional[pd.DataFrame]:
    """Historical daily bars with today's synthesized bar appended."""
    end = asof.date().isoformat()
    start = (asof.date() - timedelta(days=lookback_days)).isoformat()
    try:
        daily = daily_loader([symbol], start=start, end=end, interval="1d", source="yf")
    except Exception as e:
        log(f"daily history fetch failed for {symbol}: {e}", "WARNING")
        return None
    if not daily or symbol not in daily or daily[symbol].empty:
        return None
    base = daily[symbol].copy()
    today = synthesize_today_bar(symbol, asof=asof, minute_loader=minute_loader)
    if today is None:
        return base
    today_ts = pd.Timestamp(asof.date())
    if today_ts in base.index:
        base = base.drop(today_ts)
    base.loc[today_ts] = today
    return base.sort_index()


def _alert(message: str, *, kind: Optional[str] = None,
           strategy_id: Optional[str] = None, symbol: Optional[str] = None,
           close: Optional[float] = None) -> None:
    """Emit an alert to console + log file + Telegram (if configured).
    Structured kwargs are used by the Telegram path; the message string still
    gets sent to console + log for back-compat with tests + tail tooling.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    log(message, "SUCCESS")
    try:
        ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ALERT_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        log(f"alert log write failed: {e}", "WARNING")
    if kind and strategy_id and symbol and close is not None:
        try:
            from monitoring import telegram_alerter
            telegram_alerter.send_intraday_alert(
                kind=kind, strategy_id=strategy_id, symbol=symbol, close=close,
            )
        except Exception as e:
            log(f"telegram intraday push failed (non-fatal): {e}", "WARNING")


def _format_alert(kind: str, strategy_id: str, symbol: str, close: float) -> str:
    qualifier = ("would fire on today's close at this level" if kind == "FIRE"
                 else "exit condition true at this level")
    return (f"INTRADAY {kind} — {strategy_id} on {symbol} @ ${close:.2f} "
            f"({qualifier}); TradingView paper note: "
            f"{'BUY' if kind == 'FIRE' else 'SELL'} {symbol} ~{close:.2f} {strategy_id}")


def _emit(alerter: Callable, kind: str, strategy_id: str, symbol: str, close: float) -> None:
    """Call the alerter with the structured kwargs if it accepts them, else just the message."""
    msg = _format_alert(kind, strategy_id, symbol, close)
    try:
        alerter(msg, kind=kind, strategy_id=strategy_id, symbol=symbol, close=close)
    except TypeError:
        alerter(msg)


def scan_once(
    asof: Optional[datetime] = None,
    *,
    daily_loader: Callable = load_bars,
    minute_loader: Callable = load_bars,
    alerter: Callable[[str], None] = _alert,
) -> Dict[str, int]:
    """One pass over (active strategy, symbol) pairs. Returns counts."""
    asof = asof or datetime.now()
    bar_ts_today = asof.date().isoformat()
    counts = {"evaluated": 0, "fires": 0, "exits": 0, "skipped_strategies": 0,
              "skipped_no_bars": 0, "errors": 0}
    conn = db.init_db()
    try:
        rows = conn.execute(
            "SELECT strategy_id, compute_fn, active_on_json FROM strategies "
            "WHERE compute_fn IS NOT NULL AND active_on_json IS NOT NULL"
        ).fetchall()

        bar_cache: Dict[str, Optional[pd.DataFrame]] = {}

        for r in rows:
            sid = r["strategy_id"]
            fn = _resolve_compute_fn(r["compute_fn"])
            if fn is None:
                counts["skipped_strategies"] += 1
                continue
            try:
                active = json.loads(r["active_on_json"]) or []
            except Exception:
                active = []
            for sym in active:
                if sym in TRACKED_CRYPTO:
                    continue
                if sym not in bar_cache:
                    bar_cache[sym] = blended_daily_history(
                        sym, asof,
                        daily_loader=daily_loader, minute_loader=minute_loader,
                    )
                bars = bar_cache[sym]
                if bars is None or len(bars) < MIN_BARS_REQUIRED:
                    counts["skipped_no_bars"] += 1
                    continue
                try:
                    signals_df = fn(bars)
                except Exception as e:
                    log(f"{sid} compute failed on {sym}: {e}", "WARNING")
                    counts["errors"] += 1
                    continue
                if signals_df.empty:
                    continue
                last = signals_df.iloc[-1]
                close = float(last.get("close", bars["close"].iloc[-1]))
                counts["evaluated"] += 1
                extra = {"asof": asof.isoformat(timespec="seconds"),
                         "source": "intraday_monitor"}

                if bool(last.get("long_entry", False)):
                    sig_id = db.record_signal(
                        conn,
                        strategy_id=sid, symbol=sym,
                        bar_ts=bar_ts_today, signal_type="long_entry",
                        close=close, bar_interval=INTRADAY_INTERVAL, extra=extra,
                    )
                    if sig_id is not None:
                        counts["fires"] += 1
                        _emit(alerter, "FIRE", sid, sym, close)

                if bool(last.get("long_exit", False)):
                    sig_id = db.record_signal(
                        conn,
                        strategy_id=sid, symbol=sym,
                        bar_ts=bar_ts_today, signal_type="long_exit",
                        close=close, bar_interval=INTRADAY_INTERVAL, extra=extra,
                    )
                    if sig_id is not None:
                        counts["exits"] += 1
                        _emit(alerter, "EXIT", sid, sym, close)
        return counts
    finally:
        conn.close()


def monitor_loop(interval_sec: int = 300, market_check: bool = True) -> None:
    log(f"Intraday monitor starting (interval={interval_sec}s, "
        f"market_check={market_check}); Ctrl+C to stop.")
    while True:
        try:
            if market_check and not market_is_open():
                log("Market closed; sleeping 5 min", "INFO")
                time.sleep(300)
                continue
            counts = scan_once()
            log(f"scan: evaluated={counts['evaluated']} fires={counts['fires']} "
                f"exits={counts['exits']} skipped_no_bars={counts['skipped_no_bars']} "
                f"errors={counts['errors']}", "INFO")
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            log("Stopping monitor", "INFO")
            return
        except Exception as e:
            log(f"loop error (continuing): {e}", "ERROR")
            time.sleep(min(interval_sec, 60))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Single scan and exit")
    parser.add_argument("--interval-sec", type=int, default=300)
    parser.add_argument("--no-market-check", action="store_true",
                        help="Skip market_is_open check (testing / off-hours)")
    args = parser.parse_args()

    if args.once:
        if not args.no_market_check and not market_is_open():
            log("Market closed; --once exiting cleanly", "INFO")
            print(json.dumps({"market_open": False, "scanned": False}))
            sys.exit(0)
        counts = scan_once()
        print(json.dumps(counts, indent=2))
    else:
        monitor_loop(interval_sec=args.interval_sec,
                     market_check=not args.no_market_check)
