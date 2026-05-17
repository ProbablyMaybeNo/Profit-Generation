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
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import (  # noqa: E402
    get_account_summary, get_alpaca_client, is_paper_mode,
    load_settings, log,
)
from data import db  # noqa: E402

DEFAULT_SETTINGS = {
    "enabled": False,
    "dry_run": True,
    "min_outcomes": 30,
    "min_mean_ret_pct": 0.0,
    "min_sharpe_ish": 0.10,
    "max_position_usd": 1000,
    "skip_intraday_signals": True,
    "entry_time_offset_min": 0,
    "order_type": "market",
    "sizing_method": "fixed",
    "stop_loss_atr_multiple": 0,
    "cool_down_losers": 3,
    "cool_down_days": 5,
    "earnings_veto_days": 2,
    "veto_negative_sentiment": False,
    "negative_sentiment_threshold": 2,
    "negative_sentiment_window_hours": 24,
}

ORDER_TYPE_MARKET = "market"
ORDER_TYPE_LIMIT_INSIDE_SPREAD = "limit_inside_spread"
SUPPORTED_ORDER_TYPES = {ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT_INSIDE_SPREAD}

# US Eastern offset relative to UTC during market hours. The auto-trader
# only operates on EOD '1d' fires so we don't need precise DST handling
# — the offset is used to schedule a sleep relative to the user's clock,
# and the worst case is a 1h drift twice a year that the user can
# observe in the order log.
MARKET_OPEN_UTC = dtime(13, 30, 0)  # 09:30 ET ≈ 13:30 UTC (standard time)
MAX_OFFSET_MIN = 360  # 6 hours — anything more is a config typo.
MAX_CLIENT_ORDER_ID_LEN = 128


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


def _coerce_offset_min(raw) -> int:
    """Read settings.entry_time_offset_min defensively. Negative → 0;
    above MAX_OFFSET_MIN → clamped + warning."""
    try:
        v = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    if v > MAX_OFFSET_MIN:
        log(
            f"entry_time_offset_min={v} exceeds cap {MAX_OFFSET_MIN}; "
            f"clamping",
            "WARNING",
        )
        return MAX_OFFSET_MIN
    return v


def _target_execution_utc(asof: date, offset_min: int) -> datetime:
    """Market open + offset_min for the asof date, in UTC."""
    base = datetime.combine(asof, MARKET_OPEN_UTC).replace(tzinfo=timezone.utc)
    return base + timedelta(minutes=offset_min)


def _build_client_order_id(
    *, strategy_id: str, symbol: str, side: str,
    bar_ts: str, target_utc: Optional[datetime],
) -> str:
    """Build a deterministic, traceable client_order_id.

    Format: "ato-<sid>-<sym>-<side>-<bar>-t<HHMM>". Trimmed to 128 chars
    (Alpaca's limit) by truncating the strategy_id middle if needed.
    The target HHMM block is in UTC.
    """
    side_short = "b" if side.lower() == "buy" else "s"
    bar_short = (bar_ts or "")[:10]
    t_block = ""
    if target_utc is not None:
        t_block = f"-t{target_utc.strftime('%H%M')}"
    sid = strategy_id or "x"
    sym = symbol or "x"
    prefix = "ato"
    fixed = f"{prefix}--{sym}-{side_short}-{bar_short}{t_block}"
    budget = MAX_CLIENT_ORDER_ID_LEN - len(fixed)
    if budget <= 0:
        # Pathological: keep the suffix, drop the strategy.
        out = f"{prefix}-{sym}-{side_short}-{bar_short}{t_block}"
    else:
        sid_trimmed = sid if len(sid) <= budget else sid[: max(budget - 1, 1)] + "~"
        out = f"{prefix}-{sid_trimmed}-{sym}-{side_short}-{bar_short}{t_block}"
    return out[:MAX_CLIENT_ORDER_ID_LEN]


def _sleep_until(target_utc: datetime,
                 *, now_fn=None, sleep_fn=None) -> float:
    """Block until `target_utc`. Returns seconds waited (>= 0). Mocks pluggable
    for tests. If target is already past, returns 0 without sleeping."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    sleep_fn = sleep_fn or time.sleep
    now = now_fn()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = (target_utc - now).total_seconds()
    if delta <= 0:
        return 0.0
    sleep_fn(delta)
    return delta


def _submit_market_order(
    client, *, symbol: str, qty: int, side: str,
    client_order_id: Optional[str] = None,
):
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    kwargs = dict(
        symbol=symbol, qty=qty,
        side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    if client_order_id:
        kwargs["client_order_id"] = client_order_id
    req = MarketOrderRequest(**kwargs)
    return client.submit_order(req)


def _submit_limit_order(
    client, *, symbol: str, qty: int, side: str, limit_price: float,
    client_order_id: Optional[str] = None,
):
    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    kwargs = dict(
        symbol=symbol, qty=qty,
        side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )
    if client_order_id:
        kwargs["client_order_id"] = client_order_id
    req = LimitOrderRequest(**kwargs)
    return client.submit_order(req)


def _get_data_client():
    """Build a Stock data client lazily so callers without market-data
    permissions never pay the import cost."""
    from alpaca.data.historical import StockHistoricalDataClient
    from config.utils import load_credentials
    creds = load_credentials("alpaca")
    return StockHistoricalDataClient(creds["api_key"], creds["secret_key"])


def _build_quote_request(symbol: str):
    """Constructed lazily so the alpaca-py import only happens when the
    real fetch path is exercised; tests that inject a MagicMock data
    client never need the SDK installed."""
    from alpaca.data.requests import StockLatestQuoteRequest
    return StockLatestQuoteRequest(symbol_or_symbols=symbol)


def _fetch_latest_quote(symbol: str, data_client=None) -> tuple:
    """Return (bid, ask) for `symbol`. Returns (None, None) on any
    failure — caller decides how to fall back."""
    try:
        if data_client is None:
            data_client = _get_data_client()
        try:
            req = _build_quote_request(symbol)
        except ImportError:
            # alpaca-py isn't installed; let the mock test path through
            # by passing the bare symbol — production hits the import.
            req = symbol
        resp = data_client.get_stock_latest_quote(req)
        quote = resp.get(symbol) if hasattr(resp, "get") else resp[symbol]
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return (None, None)
        return (bid, ask)
    except Exception as e:
        log(f"latest quote fetch failed for {symbol}: {e}", "WARNING")
        return (None, None)


def _mid_price(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return round((bid + ask) / 2.0, 4)


def _normalize_order_type(raw) -> str:
    """Coerce settings.order_type to a known string. Unknown → market."""
    if not raw:
        return ORDER_TYPE_MARKET
    v = str(raw).lower().strip()
    if v in SUPPORTED_ORDER_TYPES:
        return v
    log(f"unknown order_type {raw!r}; falling back to market", "WARNING")
    return ORDER_TYPE_MARKET


def _process_entry(conn, client, settings: dict, sig, dry_run: bool,
                    *, asof: Optional[date] = None,
                    sleep_fn=None, now_fn=None,
                    data_client=None,
                    portfolio_value: Optional[float] = None,
                    bars_fetcher: Optional[Callable] = None,
                    throttle_multiplier: float = 1.0) -> dict:
    from monitoring import sizing as sizing_mod
    from monitoring import stops as stops_mod
    sid, sym = sig["strategy_id"], sig["symbol"]
    eligible, stats = _is_eligible(conn, sid, settings)
    if not eligible:
        return {"action": "SKIP_INELIGIBLE", "strategy_id": sid, "symbol": sym,
                "reason": "fails edge thresholds", "stats": stats}
    if _already_traded(conn, sig["id"], "buy"):
        return {"action": "SKIP_DUPLICATE", "strategy_id": sid, "symbol": sym,
                "signal_id": sig["id"]}
    sizing = sizing_mod.compute_notional(
        conn, sid,
        sizing_method=settings.get("sizing_method"),
        portfolio_value=portfolio_value,
        max_position_usd=float(settings.get("max_position_usd", 1000)),
        settings_tiered=settings.get("tiered"),
    )
    notional = sizing["notional"] * float(throttle_multiplier)
    sizing["throttle_multiplier"] = float(throttle_multiplier)
    sizing["notional_after_throttle"] = round(notional, 2)
    if notional <= 0:
        return {"action": "SKIP_SIZING_ZERO", "strategy_id": sid, "symbol": sym,
                "sizing": sizing}
    qty = _calc_qty(sig["close"], notional)
    if qty < 1:
        return {"action": "SKIP_PRICE", "strategy_id": sid, "symbol": sym,
                "price": sig["close"], "max_usd": settings.get("max_position_usd"),
                "sizing": sizing}

    offset_min = _coerce_offset_min(settings.get("entry_time_offset_min"))
    target_utc = (
        _target_execution_utc(asof or date.today(), offset_min)
        if offset_min > 0 else None
    )
    client_order_id = _build_client_order_id(
        strategy_id=sid, symbol=sym, side="buy",
        bar_ts=sig["bar_ts"], target_utc=target_utc,
    )

    requested_order_type = _normalize_order_type(settings.get("order_type"))
    limit_price: Optional[float] = None
    effective_order_type = ORDER_TYPE_MARKET
    if requested_order_type == ORDER_TYPE_LIMIT_INSIDE_SPREAD:
        bid, ask = _fetch_latest_quote(sym, data_client=data_client)
        mid = _mid_price(bid, ask)
        if mid is None:
            log(
                f"limit_inside_spread for {sid}/{sym}: no usable quote "
                f"(bid={bid}, ask={ask}); falling back to market",
                "WARNING",
            )
            effective_order_type = ORDER_TYPE_MARKET
        else:
            limit_price = mid
            effective_order_type = ORDER_TYPE_LIMIT_INSIDE_SPREAD

    if dry_run:
        offset_note = (
            f" (would sleep until {target_utc.isoformat()})"
            if target_utc is not None else ""
        )
        ot_note = (
            f" [limit @ ${limit_price:.4f}]"
            if effective_order_type == ORDER_TYPE_LIMIT_INSIDE_SPREAD
            else ""
        )
        stop_info = _maybe_attach_stop(
            conn, client, settings, sig,
            entry_fill=float(sig["close"] or 0),
            qty=qty, client_order_id=client_order_id,
            bars_fetcher=bars_fetcher, dry_run=True,
        )
        stop_note = (
            f" stop @ ${stop_info['stop_price']:.4f}"
            if stop_info and stop_info.get("stop_price") is not None else ""
        )
        log(f"[DRY-RUN] BUY {qty} {sym} @ ~${sig['close']:.2f} "
            f"(~${qty * sig['close']:.2f}) for {sid}{ot_note}{offset_note}"
            f"{stop_note}",
            "INFO")
        return {"action": "DRY_BUY", "strategy_id": sid, "symbol": sym,
                "qty": qty, "price": sig["close"], "signal_id": sig["id"],
                "client_order_id": client_order_id,
                "target_execution_utc": target_utc.isoformat() if target_utc else None,
                "entry_time_offset_min": offset_min,
                "order_type": effective_order_type,
                "limit_price": limit_price,
                "requested_order_type": requested_order_type,
                "sizing": sizing,
                "stop": stop_info}

    if target_utc is not None:
        waited = _sleep_until(target_utc, now_fn=now_fn, sleep_fn=sleep_fn)
        if waited > 0:
            log(
                f"entry_time_offset_min={offset_min}: slept {waited:.0f}s "
                f"for {sid}/{sym} until {target_utc.isoformat()}",
                "INFO",
            )

    try:
        if effective_order_type == ORDER_TYPE_LIMIT_INSIDE_SPREAD:
            order = _submit_limit_order(
                client, symbol=sym, qty=qty, side="buy",
                limit_price=limit_price,
                client_order_id=client_order_id,
            )
        else:
            order = _submit_market_order(
                client, symbol=sym, qty=qty, side="buy",
                client_order_id=client_order_id,
            )
    except Exception as e:
        log(f"order submit failed for {sid}/{sym}: {e}", "ERROR")
        return {"action": "ERROR", "strategy_id": sid, "symbol": sym,
                "error": str(e)[:200]}

    entry_fill = float(getattr(order, "filled_avg_price", 0) or 0) or None
    db.record_paper_trade(conn, {
        "alpaca_order_id": str(getattr(order, "id", "")),
        "signal_id": sig["id"],
        "strategy_id": sid, "symbol": sym, "side": "buy", "qty": qty,
        "order_type": effective_order_type,
        "limit_price": limit_price,
        "fill_price": entry_fill,
        "submitted_at": str(getattr(order, "submitted_at", _utc_now())),
        "status": str(getattr(order, "status", "submitted")),
        "notes": f"auto-entry on bar_ts={sig['bar_ts']}"
                 + (f"; client_order_id={client_order_id}" if offset_min > 0 else "")
                 + (f"; limit_inside_spread @ ${limit_price}" if limit_price else ""),
    })
    log(
        f"BUY {qty} {sym} order submitted: {order.id} "
        f"({effective_order_type})",
        "SUCCESS",
    )

    # Optional ATR-based stop attached alongside the entry.
    stop_info = _maybe_attach_stop(
        conn, client, settings, sig,
        entry_fill=entry_fill or float(sig["close"] or 0),
        qty=qty,
        client_order_id=client_order_id,
        bars_fetcher=bars_fetcher,
    )

    return {"action": "BUY", "strategy_id": sid, "symbol": sym, "qty": qty,
            "order_id": str(order.id), "signal_id": sig["id"],
            "client_order_id": client_order_id,
            "target_execution_utc": target_utc.isoformat() if target_utc else None,
            "entry_time_offset_min": offset_min,
            "order_type": effective_order_type,
            "limit_price": limit_price,
            "requested_order_type": requested_order_type,
            "sizing": sizing,
            "stop": stop_info}


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


DEFAULT_MAX_PCT_PER_SYMBOL = 0.30
DEFAULT_MAX_OPEN_PER_STRATEGY = 3


def _coerce_max_open_per_strategy(raw) -> int:
    """Cap value coerced from settings.risk.max_open_per_strategy.
    Zero or negative disables the cap (returns 0 → unbounded)."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_OPEN_PER_STRATEGY
    if v < 0:
        return 0
    return v


def _open_position_count_per_strategy(conn) -> Dict[str, int]:
    """Return {strategy_id: open_qty_position_count}. An "open" position
    is a BUY whose later SELL hasn't filled (excluding canceled/rejected).
    Counts per-(strategy, symbol) so two BUYs on the same symbol from
    different strategies are tracked separately, but two BUYs on the same
    (strategy, symbol) only count once."""
    rows = conn.execute(
        "SELECT DISTINCT strategy_id, symbol FROM paper_trades "
        " WHERE side='buy' "
        "   AND status IN ('filled', 'partially_filled', 'accepted', 'new') ",
    ).fetchall()
    out: Dict[str, int] = {}
    for r in rows:
        sid, sym = r["strategy_id"], r["symbol"]
        if sid is None or sym is None:
            continue
        # Filter out (sid, sym) pairs that have a later SELL.
        later_sell = conn.execute(
            "SELECT 1 FROM paper_trades pt1 "
            " WHERE pt1.strategy_id=? AND pt1.symbol=? AND pt1.side='sell' "
            "   AND pt1.status NOT IN ('canceled', 'rejected') "
            "   AND pt1.submitted_at > ("
            "     SELECT MAX(submitted_at) FROM paper_trades "
            "      WHERE strategy_id=? AND symbol=? AND side='buy' "
            "        AND status NOT IN ('canceled', 'rejected')) LIMIT 1",
            (sid, sym, sid, sym),
        ).fetchone()
        if later_sell is not None:
            continue
        out[sid] = out.get(sid, 0) + 1
    return out


def _coerce_max_daily_loss_pct(raw) -> Optional[float]:
    """Settings value may be a positive percent (e.g. 2.0 = 2%) or None.
    Negative / zero / non-numeric → None (= disabled)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v


def _drawdown_circuit_breaker(settings: dict, account: dict) -> Optional[dict]:
    """Return a dict describing the trip when daily loss has reached the
    configured threshold; None otherwise.

    Reads:
      - settings.risk.max_daily_loss_pct (positive percent, e.g. 2.0).
      - account.portfolio_value (current equity).
      - account.equity_at_open or account.last_equity (yesterday's close
        as proxy when the broker doesn't expose intraday opening equity).

    When account data is missing, returns None so the auto-trader doesn't
    block on a transient API hiccup.
    """
    threshold = _coerce_max_daily_loss_pct(
        (settings.get("risk") or {}).get("max_daily_loss_pct"),
    )
    if threshold is None:
        return None
    if not account:
        return None
    pv = account.get("portfolio_value")
    open_equity = account.get("equity_at_open") or account.get("last_equity")
    try:
        pv = float(pv) if pv is not None else None
        open_equity = float(open_equity) if open_equity is not None else None
    except (TypeError, ValueError):
        return None
    if pv is None or open_equity is None or open_equity <= 0:
        return None
    daily_pl_pct = (pv - open_equity) / open_equity * 100.0
    if daily_pl_pct > -threshold:
        return None
    return {
        "daily_pl_pct": round(daily_pl_pct, 4),
        "threshold_pct": threshold,
        "portfolio_value": pv,
        "equity_at_open": open_equity,
        "reason": (
            f"daily P/L {daily_pl_pct:.2f}% ≤ -{threshold:.2f}% threshold "
            f"({pv:.2f} from {open_equity:.2f})"
        ),
    }


DEFAULT_COOL_DOWN_LOSERS = 3
DEFAULT_COOL_DOWN_DAYS = 5


def _coerce_cool_down_losers(raw) -> int:
    """Number of consecutive losers that triggers cool-down. 0/negative = disabled."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_COOL_DOWN_LOSERS
    if v < 0:
        return 0
    return v


def _coerce_cool_down_days(raw) -> int:
    """Number of trading days to pause. 0/negative = disabled."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_COOL_DOWN_DAYS
    if v < 0:
        return 0
    return v


def _trading_days_between(conn, start_iso: str, end_iso: str) -> int:
    """Count distinct trading-day dates strictly between start and end,
    inclusive of end and exclusive of start. Uses snapshots.snapshot_date
    as the trading-day calendar — it's populated by the daily report and
    skips weekends/holidays naturally.

    If snapshots is empty (test env / fresh install), falls back to a
    weekday-only Mon-Fri count. Weekends still count as zero gap; holidays
    are not honoured but the user notices once and overrides if needed.
    """
    try:
        start = date.fromisoformat(start_iso[:10])
        end = date.fromisoformat(end_iso[:10])
    except (ValueError, TypeError):
        return 0
    if end <= start:
        return 0
    rows = conn.execute(
        "SELECT DISTINCT snapshot_date FROM snapshots "
        " WHERE snapshot_date > ? AND snapshot_date <= ? "
        " ORDER BY snapshot_date ASC",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if rows:
        return len(rows)
    # Snapshot calendar unavailable — fall back to weekday count.
    count = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def _last_n_closed_outcomes(conn, strategy_id: str, n: int) -> List[dict]:
    """Return the strategy's most recent N closed 1d outcomes, newest first."""
    if n <= 0:
        return []
    rows = conn.execute(
        "SELECT o.exit_ts, o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval='1d' AND s.strategy_id=? "
        " ORDER BY o.exit_ts DESC, o.signal_id DESC "
        " LIMIT ?",
        (strategy_id, int(n)),
    ).fetchall()
    return [{"exit_ts": r["exit_ts"], "return_pct": float(r["return_pct"])}
            for r in rows]


def _cool_down_state(
    conn, strategy_id: str, settings: dict, *,
    asof: Optional[date] = None,
) -> Optional[dict]:
    """Return a dict describing the cool-down trip for `strategy_id`, or None.

    Triggers when the strategy's last `cool_down_losers` closed 1d outcomes
    were ALL losers (return_pct <= 0). The pause lasts `cool_down_days`
    trading days after the most-recent loser's exit_ts. Mixed wins/losses
    in the trailing window do NOT trip. Re-arm happens automatically the
    first day the trading-day gap exceeds the threshold.

    Returns:
      {"strategy_id": str, "losers_required": int, "pause_days": int,
       "last_loser_exit_ts": str, "trading_days_since": int,
       "trading_days_remaining": int, "reason": str}
    """
    losers_required = _coerce_cool_down_losers(settings.get("cool_down_losers"))
    pause_days = _coerce_cool_down_days(settings.get("cool_down_days"))
    if losers_required <= 0 or pause_days <= 0:
        return None
    outcomes = _last_n_closed_outcomes(conn, strategy_id, losers_required)
    if len(outcomes) < losers_required:
        return None
    if not all(o["return_pct"] <= 0 for o in outcomes):
        return None
    last_loser = outcomes[0]
    asof_d = asof or date.today()
    days_since = _trading_days_between(
        conn, last_loser["exit_ts"], asof_d.isoformat(),
    )
    if days_since >= pause_days:
        return None
    remaining = pause_days - days_since
    return {
        "strategy_id": strategy_id,
        "losers_required": losers_required,
        "pause_days": pause_days,
        "last_loser_exit_ts": last_loser["exit_ts"],
        "trading_days_since": days_since,
        "trading_days_remaining": remaining,
        "reason": (
            f"last {losers_required} closed outcomes all losers; "
            f"{remaining} trading day(s) remaining of "
            f"{pause_days}-day pause"
        ),
    }


DEFAULT_EARNINGS_VETO_DAYS = 2
DEFAULT_NEGATIVE_SENTIMENT_THRESHOLD = 2
DEFAULT_NEGATIVE_SENTIMENT_WINDOW_HOURS = 24


def _coerce_negative_sentiment_threshold(raw) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_NEGATIVE_SENTIMENT_THRESHOLD
    if v < 1:
        return 1
    return v


def _coerce_negative_sentiment_window_hours(raw) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_NEGATIVE_SENTIMENT_WINDOW_HOURS
    if v < 1:
        return 1
    return v


def _count_negative_news_for_symbol(
    conn, symbol: str, *, asof_dt: datetime, window_hours: int,
) -> int:
    """Count distinct news rows for `symbol` published within `window_hours`
    of `asof_dt` whose sentiment payload contains at least one negative
    label for this symbol.

    asof_dt is interpreted as UTC. The news table stores published_utc
    as ISO-8601 strings — a naïve lexical >= compare works for that format.
    """
    from monitoring.news_sentiment_overlay import extract_sentiment_labels
    if asof_dt.tzinfo is None:
        asof_dt = asof_dt.replace(tzinfo=timezone.utc)
    cutoff = (asof_dt - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    upper = asof_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT id, sentiment FROM news "
        " WHERE symbol=? AND sentiment IS NOT NULL "
        "   AND published_utc >= ? AND published_utc <= ?",
        (symbol, cutoff, upper),
    ).fetchall()
    negative = 0
    for r in rows:
        labels = extract_sentiment_labels(r["sentiment"], symbol)
        if any(l == "negative" for l in labels):
            negative += 1
    return negative


def _negative_sentiment_veto(
    conn, symbol: str, settings: dict, *, asof: Optional[date] = None,
) -> Optional[dict]:
    """Return a veto descriptor when `symbol` has ≥ threshold negative-
    sentiment news items in the last N hours, or None when disabled / clear."""
    if not bool(settings.get("veto_negative_sentiment", False)):
        return None
    threshold = _coerce_negative_sentiment_threshold(
        settings.get("negative_sentiment_threshold"),
    )
    window_hours = _coerce_negative_sentiment_window_hours(
        settings.get("negative_sentiment_window_hours"),
    )
    if asof is None:
        asof_dt = datetime.now(timezone.utc)
    else:
        asof_dt = datetime.combine(asof, dtime(23, 59, 59),
                                    tzinfo=timezone.utc)
    n_negative = _count_negative_news_for_symbol(
        conn, symbol, asof_dt=asof_dt, window_hours=window_hours,
    )
    if n_negative < threshold:
        return None
    return {
        "symbol": symbol,
        "negative_count": n_negative,
        "threshold": threshold,
        "window_hours": window_hours,
        "reason": (
            f"{n_negative} negative-sentiment news item(s) in the last "
            f"{window_hours}h (>= threshold {threshold})"
        ),
    }



def _coerce_earnings_veto_days(raw) -> int:
    """Trading-day window for earnings veto. 0/negative = disabled."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EARNINGS_VETO_DAYS
    if v < 0:
        return 0
    return v


def _earnings_veto(
    conn, symbol: str, settings: dict, *, asof: Optional[date] = None,
) -> Optional[dict]:
    """Return a veto descriptor when `symbol` is inside the earnings window,
    or None when no upcoming earnings are recorded / veto is disabled."""
    window = _coerce_earnings_veto_days(settings.get("earnings_veto_days"))
    if window <= 0:
        return None
    from monitoring import earnings_calendar
    return earnings_calendar.is_within_earnings_window(
        conn, symbol, asof=asof, window_trading_days=window,
    )


def _max_pct_per_symbol(settings: dict) -> float:
    """settings.risk.max_pct_per_symbol → float in (0, 1]. Falls back to
    the default when missing / out of range."""
    raw = (settings.get("risk") or {}).get("max_pct_per_symbol")
    if raw is None:
        raw = settings.get("max_pct_per_symbol")  # back-compat: top-level
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PCT_PER_SYMBOL
    if v <= 0 or v > 1.0:
        return DEFAULT_MAX_PCT_PER_SYMBOL
    return v


def _open_notional_by_symbol(conn) -> Dict[str, float]:
    """Notional value of currently-open paper positions per symbol."""
    rows = conn.execute(
        "SELECT pt.symbol, pt.qty, COALESCE(pt.fill_price, pt.limit_price) AS px "
        "  FROM paper_trades pt "
        " WHERE pt.side='buy' "
        "   AND pt.status IN ('filled', 'partially_filled', 'accepted', 'new') "
    ).fetchall()
    out: Dict[str, float] = {}
    for r in rows:
        if r["qty"] is None or r["px"] is None:
            continue
        # Subtract any matching SELL with later submitted_at? Simpler:
        # we treat all live BUYs as occupying capital; the sell-paired
        # filter would require timestamp comparison. For the cap this
        # over-counts in the worst case (= more conservative), which
        # is the safe direction.
        out[r["symbol"]] = out.get(r["symbol"], 0.0) + float(r["qty"]) * float(r["px"])
    return out


def _strategy_sharpe(conn, strategy_id: str) -> float:
    rows = conn.execute(
        "SELECT o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval='1d' AND s.strategy_id=?",
        (strategy_id,),
    ).fetchall()
    rets = [float(r["return_pct"]) for r in rows]
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    sd = statistics.stdev(rets)
    return (mean / sd) if sd > 0 else 0.0


def _concentration_block_map(
    conn, settings: dict, sigs, *,
    portfolio_value: Optional[float],
) -> Dict[int, dict]:
    """Decide which competing long_entry signals get blocked by the
    per-symbol concentration cap. Returns {signal_id: skip_action_dict}.

    Rules:
      - Group today's long_entry signals by symbol.
      - For each symbol, count the existing open-position notional plus
        new entries in sharpe-desc order until the cap is hit.
      - Each rejected entry yields a SKIP_CONCENTRATION_CAP action.
      - When portfolio_value is unknown OR the user hasn't opted into
        max_pct_per_symbol, no blocks are issued.
    """
    if not portfolio_value or portfolio_value <= 0:
        return {}
    has_cap_setting = (settings.get("risk") or {}).get("max_pct_per_symbol") \
        is not None or "max_pct_per_symbol" in settings
    if not has_cap_setting:
        return {}
    pct = _max_pct_per_symbol(settings)
    cap = pct * float(portfolio_value)
    max_position = float(settings.get("max_position_usd", 1000))
    # When Kelly sizing is in play, the actual notional per entry will be
    # bounded by both max_position and KELLY_CAP * portfolio_value — using
    # the smaller of the two avoids over-counting the per-position
    # footprint when the user has set an aspirational max_position_usd
    # that Kelly will never actually reach.
    from monitoring.sizing import KELLY_CAP, normalize_sizing_method
    if normalize_sizing_method(settings.get("sizing_method")) == "kelly":
        max_position = min(max_position,
                            KELLY_CAP * float(portfolio_value))

    by_symbol: Dict[str, List] = {}
    for s in sigs:
        if s["signal_type"] != "long_entry":
            continue
        by_symbol.setdefault(s["symbol"], []).append(s)

    open_notional = _open_notional_by_symbol(conn)
    blocks: Dict[int, dict] = {}
    for sym, entries in by_symbol.items():
        used = open_notional.get(sym, 0.0)
        ranked = sorted(
            entries,
            key=lambda s: (-_strategy_sharpe(conn, s["strategy_id"]),
                            s["strategy_id"]),
        )
        for ent in ranked:
            if used + max_position > cap + 1e-6:
                blocks[ent["id"]] = {
                    "action": "SKIP_CONCENTRATION_CAP",
                    "strategy_id": ent["strategy_id"],
                    "symbol": sym,
                    "signal_id": ent["id"],
                    "cap_usd": round(cap, 2),
                    "used_usd": round(used, 2),
                    "next_position_usd": round(max_position, 2),
                    "max_pct_per_symbol": pct,
                }
                continue
            used += max_position
    return blocks


def _maybe_attach_stop(
    conn, client, settings: dict, sig,
    *, entry_fill: float, qty: int,
    client_order_id: Optional[str],
    bars_fetcher: Optional[Callable],
    dry_run: bool = False,
) -> Optional[dict]:
    """Compute + (if not dry-run) submit an ATR-based stop. Returns
    a dict describing the stop, or None when stops are disabled / not
    actionable.

    The returned dict shape:
      {"requested_multiple": N,
       "atr": float | None,
       "stop_price": float | None,
       "status": "disabled" | "no_bars" | "no_stop" | "submitted"
                  | "submit_failed" | "dry_run",
       "order_id": str | None,
       "stop_order_client_id": str | None,
       "error": str | None}
    """
    from monitoring import stops as stops_mod
    multiple = stops_mod._coerce_multiple(settings.get("stop_loss_atr_multiple"))
    if multiple <= 0:
        return None
    info: dict = {
        "requested_multiple": multiple,
        "atr": None,
        "stop_price": None,
        "status": "disabled",
        "order_id": None,
        "stop_order_client_id": None,
        "error": None,
    }
    if bars_fetcher is None:
        info["status"] = "no_bars"
        return info
    try:
        bars = bars_fetcher(sig["symbol"])
    except Exception as e:
        info["status"] = "no_bars"
        info["error"] = str(e)[:200]
        return info
    atr = stops_mod.compute_atr(bars)
    info["atr"] = atr
    stop_price = stops_mod.stop_price_for(entry_fill, atr, multiple)
    info["stop_price"] = stop_price
    if stop_price is None:
        info["status"] = "no_stop"
        return info
    stop_cid = (client_order_id + "-stop")[:MAX_CLIENT_ORDER_ID_LEN] \
        if client_order_id else None
    info["stop_order_client_id"] = stop_cid
    if dry_run:
        info["status"] = "dry_run"
        return info
    try:
        stop_order = stops_mod.submit_atr_stop(
            client, symbol=sig["symbol"], qty=qty,
            stop_price=stop_price, client_order_id=stop_cid,
        )
    except Exception as e:
        log(f"stop submit failed for {sig['strategy_id']}/{sig['symbol']}: {e}",
            "ERROR")
        info["status"] = "submit_failed"
        info["error"] = str(e)[:200]
        return info
    info["order_id"] = str(getattr(stop_order, "id", ""))
    info["status"] = "submitted"
    db.record_paper_trade(conn, {
        "alpaca_order_id": info["order_id"],
        "signal_id": sig["id"],
        "strategy_id": sig["strategy_id"], "symbol": sig["symbol"],
        "side": "sell", "qty": qty,
        "order_type": "stop",
        "stop_price": stop_price,
        "submitted_at": str(getattr(stop_order, "submitted_at", _utc_now())),
        "status": str(getattr(stop_order, "status", "submitted")),
        "notes": f"ATR({stops_mod.DEFAULT_ATR_PERIOD})={atr} × {multiple} "
                 f"= stop @ ${stop_price}; "
                 f"linked to entry signal_id={sig['id']}",
    })
    log(
        f"STOP SELL {qty} {sig['symbol']} @ ${stop_price} "
        f"(ATR={atr} × {multiple})",
        "SUCCESS",
    )
    return info


def _live_strategies(settings: dict) -> set:
    """Coerce settings.auto_trade.live_strategies into a set of strategy_ids.

    Defaults to an empty set (= every strategy routes to paper). Accepts
    list-of-strings or null; anything else logs a warning and defaults
    to empty so a malformed setting never silently sends live orders.
    """
    raw = settings.get("live_strategies")
    if raw is None:
        return set()
    if not isinstance(raw, list):
        log(
            f"auto_trade.live_strategies is not a list "
            f"({type(raw).__name__}); ignoring — all strategies stay paper",
            "WARNING",
        )
        return set()
    return {str(s) for s in raw if s}


def _resolve_strategy_client(
    strategy_id: str, *,
    live_set: set,
    paper_client,
    live_client_factory: Callable,
    live_cache: Dict[str, object],
) -> object:
    """Return the Alpaca client to use for `strategy_id`. Builds the live
    client lazily and caches it. Raises ValueError when live creds are
    missing — caller decides whether to skip the signal or abort the run.
    """
    if strategy_id not in live_set:
        return paper_client
    if "live" in live_cache:
        return live_cache["live"]
    client = live_client_factory()
    live_cache["live"] = client
    return client


def process_signals(
    conn,
    *,
    asof: Optional[date] = None,
    settings: Optional[dict] = None,
    client=None,
    client_factory: Callable = get_alpaca_client,
    live_client_factory: Optional[Callable] = None,
    sleep_fn=None,
    now_fn=None,
    data_client=None,
    account_summary_fn: Optional[Callable] = None,
    bars_fetcher: Optional[Callable] = None,
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

    live_set = _live_strategies(settings)
    if live_client_factory is None:
        live_client_factory = lambda: get_alpaca_client(live=True)
    live_cache: Dict[str, object] = {}

    sigs = conn.execute(
        "SELECT id, ts, bar_ts, bar_interval, strategy_id, symbol, signal_type, close "
        "  FROM signals "
        " WHERE bar_ts = ? AND bar_interval = '1d' "
        " ORDER BY id ASC",
        (asof.isoformat(),),
    ).fetchall()

    # portfolio_value is needed by Kelly sizing, by the per-symbol
    # concentration cap, AND by the daily drawdown circuit breaker. We
    # fetch the account summary once per pipeline invocation so all three
    # consumers see the same number.
    needs_kelly = str(settings.get("sizing_method") or "").lower() == "kelly"
    has_cap_setting = (settings.get("risk") or {}).get("max_pct_per_symbol") \
        is not None or "max_pct_per_symbol" in settings
    has_drawdown_setting = (settings.get("risk") or {}).get(
        "max_daily_loss_pct") is not None
    # Drawdown auto-throttle (3.2.2) is always-on once an account_summary_fn
    # is wired: we want it monitoring even when the user hasn't opted into
    # the other risk knobs.
    throttle_always_on = account_summary_fn is not None or not dry_run
    portfolio_value: Optional[float] = None
    account_summary: Dict = {}
    if (needs_kelly or has_cap_setting or has_drawdown_setting
            or throttle_always_on):
        fn = account_summary_fn or (
            (lambda: get_account_summary()) if not dry_run else None
        )
        if fn is not None:
            try:
                account_summary = fn() or {}
                pv = account_summary.get("portfolio_value")
                portfolio_value = float(pv) if pv is not None else None
            except Exception as e:
                log(f"account_summary lookup failed: {e}", "WARNING")
                portfolio_value = None
                account_summary = {}

    drawdown_block = _drawdown_circuit_breaker(settings, account_summary)

    # Portfolio drawdown auto-throttle (3.2.2).
    throttle_info = None
    throttle_multiplier = 1.0
    if portfolio_value is not None and portfolio_value > 0:
        try:
            db.record_equity_snapshot(
                conn,
                portfolio_value=portfolio_value,
                cash=account_summary.get("cash") if account_summary else None,
                equity=account_summary.get("equity") if account_summary else None,
                buying_power=(account_summary.get("buying_power")
                              if account_summary else None),
                source="auto_trader",
            )
        except Exception as e:
            log(f"equity snapshot insert failed: {e}", "WARNING")
        from monitoring import drawdown_throttle as dt_mod
        throttle_cfg = (settings.get("drawdown_throttle") or {})
        peak = db.trailing_peak_portfolio_value(
            conn,
            window_days=int(dt_mod._coerce_settings(throttle_cfg)["window_days"]),
        )
        throttle_info = dt_mod.evaluate(
            current_pv=portfolio_value, peak_pv=peak,
            settings_throttle=throttle_cfg,
        )
        throttle_multiplier = float(throttle_info["multiplier"])
        if throttle_info.get("trip_kill_switch"):
            try:
                dt_mod.maybe_engage_kill_switch(throttle_info)
                log(f"DRAWDOWN_THROTTLE_HALT: {throttle_info['reason']}", "ERROR")
            except Exception as e:
                log(f"drawdown throttle: kill switch engage failed: {e}", "WARNING")

    concentration_blocks = _concentration_block_map(
        conn, settings, sigs, portfolio_value=portfolio_value,
    )

    # Concurrent open-position cap by strategy (3.2.3).
    max_open_per_strategy = _coerce_max_open_per_strategy(
        (settings.get("risk") or {}).get("max_open_per_strategy",
                                          DEFAULT_MAX_OPEN_PER_STRATEGY),
    )
    open_per_strategy = (_open_position_count_per_strategy(conn)
                         if max_open_per_strategy > 0 else {})

    # Read the kill switch AFTER the throttle has had a chance to engage
    # it for this run — that way a brutal in-session drawdown trips the
    # switch and immediately halts further entries the same run.
    from monitoring import kill_switch as ks_mod
    kill_switch_state = ks_mod.load_state()
    kill_switch_engaged = bool(kill_switch_state.get("live_trading_halted"))
    kill_switch_logged = False

    actions: List[dict] = []
    cool_down_cache: Dict[str, Optional[dict]] = {}
    earnings_cache: Dict[str, Optional[dict]] = {}
    sentiment_cache: Dict[str, Optional[dict]] = {}
    for sig in sigs:
        if sig["signal_type"] == "long_entry":
            if kill_switch_engaged:
                if not kill_switch_logged:
                    log(
                        "KILL_SWITCH_HALT: refusing all entries — "
                        f"reason={kill_switch_state.get('reason') or '(none)'} "
                        f"set_at={kill_switch_state.get('set_at') or '?'}",
                        "WARNING",
                    )
                    kill_switch_logged = True
                actions.append({
                    "action": "KILL_SWITCH_HALT",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    "reason": kill_switch_state.get("reason") or "",
                    "set_at": kill_switch_state.get("set_at") or "",
                })
                continue
            if drawdown_block is not None:
                actions.append({
                    "action": "SKIP_DAILY_DRAWDOWN",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    **drawdown_block,
                })
                continue
            sym = sig["symbol"]
            if sym not in earnings_cache:
                earnings_cache[sym] = _earnings_veto(
                    conn, sym, settings, asof=asof,
                )
            ev = earnings_cache[sym]
            if ev is not None:
                actions.append({
                    "action": "SKIP_EARNINGS_WEEK",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sym,
                    "signal_id": sig["id"],
                    **ev,
                })
                continue
            if sym not in sentiment_cache:
                sentiment_cache[sym] = _negative_sentiment_veto(
                    conn, sym, settings, asof=asof,
                )
            ns = sentiment_cache[sym]
            if ns is not None:
                actions.append({
                    "action": "SKIP_NEGATIVE_SENTIMENT",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sym,
                    "signal_id": sig["id"],
                    **ns,
                })
                continue
            sid = sig["strategy_id"]
            if sid not in cool_down_cache:
                cool_down_cache[sid] = _cool_down_state(
                    conn, sid, settings, asof=asof,
                )
            cd = cool_down_cache[sid]
            if cd is not None:
                actions.append({
                    "action": "SKIP_COOL_DOWN",
                    "strategy_id": sid,
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    **cd,
                })
                continue
            block = concentration_blocks.get(sig["id"])
            if block is not None:
                actions.append(block)
                continue
            if max_open_per_strategy > 0:
                cur_open = open_per_strategy.get(sig["strategy_id"], 0)
                if cur_open >= max_open_per_strategy:
                    actions.append({
                        "action": "SKIP_MAX_OPEN_PER_STRATEGY",
                        "strategy_id": sig["strategy_id"],
                        "symbol": sig["symbol"],
                        "signal_id": sig["id"],
                        "open_count": cur_open,
                        "cap": max_open_per_strategy,
                        "reason": (
                            f"strategy already has {cur_open} open "
                            f"position(s) (cap={max_open_per_strategy})"
                        ),
                    })
                    continue
            try:
                strategy_client = _resolve_strategy_client(
                    sig["strategy_id"],
                    live_set=live_set,
                    paper_client=client,
                    live_client_factory=live_client_factory,
                    live_cache=live_cache,
                )
            except ValueError as e:
                actions.append({
                    "action": "SKIP_LIVE_CREDS_MISSING",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    "reason": str(e),
                })
                continue
            entry_action = _process_entry(
                conn, strategy_client, settings, sig, dry_run,
                asof=asof, sleep_fn=sleep_fn, now_fn=now_fn,
                data_client=data_client,
                portfolio_value=portfolio_value,
                bars_fetcher=bars_fetcher,
                throttle_multiplier=throttle_multiplier,
            )
            actions.append(entry_action)
            if (max_open_per_strategy > 0
                    and entry_action.get("action") in ("BUY", "DRY_BUY")):
                open_per_strategy[sig["strategy_id"]] = (
                    open_per_strategy.get(sig["strategy_id"], 0) + 1
                )
        elif sig["signal_type"] == "long_exit":
            try:
                strategy_client = _resolve_strategy_client(
                    sig["strategy_id"],
                    live_set=live_set,
                    paper_client=client,
                    live_client_factory=live_client_factory,
                    live_cache=live_cache,
                )
            except ValueError as e:
                actions.append({
                    "action": "SKIP_LIVE_CREDS_MISSING",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    "reason": str(e),
                })
                continue
            actions.append(_process_exit(conn, strategy_client, settings, sig, dry_run))

    out = {"status": "OK", "dry_run": dry_run, "asof": asof.isoformat(),
           "actions": actions}
    if throttle_info is not None:
        out["throttle"] = throttle_info
    return out


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
