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
from typing import Any, Callable, Dict, List, Optional

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
    "realized_stats_gate": {
        "enabled": True,
        "recent_n": 10,
        "min_sample": 3,
        "min_win_rate": 0.40,
        "min_avg_return_pct": 0.0,
    },
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


def _skip_source_for_bar_interval(bar_interval: Optional[str]) -> str:
    """Map a bar_interval to the `source` value persisted on intraday_skips
    rows. EOD (1d) → 'daily'; anything else → 'intraday_15m'. The 1m-native
    live_stream source is reserved for the 7.5.4 path."""
    bi = (bar_interval or "1d").lower()
    if bi == "1d":
        return "daily"
    return "intraday_15m"


def _record_skip(
    conn, *, sig=None, strategy_id=None, symbol=None,
    bar_ts=None, signal_type=None,
    gate: str, reason_detail: Optional[str] = None,
    source: str = "daily",
) -> None:
    """Thin wrapper around db.record_intraday_skip that never raises into
    the caller. The block decision is the load-bearing behavior; the skip
    write is observability and must not break trading if the DB hiccups."""
    try:
        if sig is not None:
            strategy_id = strategy_id or (sig["strategy_id"]
                if "strategy_id" in sig.keys() else None)
            symbol = symbol or (sig["symbol"]
                if "symbol" in sig.keys() else None)
            bar_ts = bar_ts or (sig["bar_ts"]
                if "bar_ts" in sig.keys() else None)
            signal_type = signal_type or (sig["signal_type"]
                if "signal_type" in sig.keys() else None)
        db.record_intraday_skip(
            conn,
            strategy_id=strategy_id, symbol=symbol,
            bar_ts=bar_ts, signal_type=signal_type,
            gate=gate, reason_detail=reason_detail,
            source=source,
        )
    except Exception as e:
        log(f"intraday_skip write failed (non-fatal): "
            f"{type(e).__name__}: {e}", "WARNING")


def merge_config(raw: dict) -> dict:
    """Flatten a raw settings.json dict into the single dict the engine reads:
    DEFAULT_SETTINGS, overlaid with the `auto_trade` block (minus `_` keys),
    plus the sibling `stops`/`kelly`/`trailing_stop`/`risk` blocks merged in.

    The engine reads those sibling blocks off the same settings dict
    (settings.get("stops"/"kelly"/"trailing_stop"/"risk")), but they live at
    the top level of settings.json — merge them so the live path sees them,
    without clobbering an explicit auto_trade key. Shared by the EOD path
    (_config) and the intraday path so both honor identical risk/sizing.
    """
    s = raw.get("auto_trade", {})
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in s.items() if not k.startswith("_")})
    for block in ("stops", "kelly", "trailing_stop", "risk", "intraday",
                  "max_loss_cap"):
        if block in raw and block not in out:
            out[block] = raw[block]
    return out


def _config() -> dict:
    return merge_config(load_settings())


def _is_eligible(conn, strategy_id: str, settings: dict,
                 *, grace_period: bool = False,
                 bar_interval: str = "1d") -> tuple:
    """Return (ok: bool, stats: dict). Stats always populated for logging.

    When `grace_period=True`, the strategy is allowed to fire orders even
    before it accumulates `min_outcomes` closed trades — the size is
    expected to be reduced by the caller (via the grace_period_size_multiplier
    setting). Stats includes `in_grace=True` so callers can apply that
    multiplier. Once n >= min_outcomes, grace mode is ignored and the
    normal mean / sharpe thresholds apply.

    F6 (audit 2026-06-03): scope the outcome set to the signal's own class so
    an intraday strategy is judged on its INTRADAY record, not the (empty) 1d
    set. A 1d signal counts 1d outcomes; any non-1d (intraday) signal counts
    the strategy's non-1d closed outcomes. The two classes are kept separate
    so incomparable interval stats aren't pooled. Thresholds are unchanged —
    only WHICH outcomes are counted.
    """
    if (bar_interval or "1d") == "1d":
        interval_clause = "s.bar_interval='1d'"
    else:
        interval_clause = "s.bar_interval!='1d'"
    # M8 (Sprint 3) — judge eligibility on FRESH trading closes only. An outcome
    # closed by a reconcile/orphan/stale sweep is cleanup bookkeeping (often a 0%
    # flat at the last mark), not the strategy's edge; counting it poisons the
    # mean/sharpe gate that decides whether the strategy keeps trading.
    from monitoring.strategy_health import CLEANUP_EXIT_REASONS as _CLEANUP
    _cleanup_ph = ", ".join("?" for _ in _CLEANUP)
    rows = conn.execute(
        "SELECT o.return_pct FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='closed' AND o.return_pct IS NOT NULL "
        f"   AND (o.exit_reason IS NULL OR o.exit_reason NOT IN ({_cleanup_ph})) "
        f"   AND {interval_clause} AND s.strategy_id=? "
        " ORDER BY o.exit_ts ASC, o.signal_id ASC",
        (*_CLEANUP, strategy_id),
    ).fetchall()
    rets = [r["return_pct"] for r in rows]
    n = len(rets)
    min_n = settings.get("min_outcomes", 30)
    realized_cfg = settings.get("realized_stats_gate") or {}
    realized_stats = {"enabled": bool(realized_cfg.get("enabled", False)),
                      "blocked": False, "n": 0, "win_rate": 0.0,
                      "avg_return_pct": 0.0, "reason": ""}
    stats = {"n": n, "mean": 0.0, "sharpe": 0.0, "in_grace": False,
             "realized_gate": realized_stats}
    if n == 0:
        if grace_period:
            stats["in_grace"] = True
            return True, stats
        return False, stats
    mean = sum(rets) / n
    sd = statistics.stdev(rets) if n > 1 else 0.0
    sharpe = (mean / sd) if sd > 0 else 0.0
    stats["mean"] = round(mean, 4)
    stats["sharpe"] = round(sharpe, 4)

    if realized_stats["enabled"]:
        recent_n = max(1, int(realized_cfg.get("recent_n", 10) or 10))
        min_sample = max(1, int(realized_cfg.get("min_sample", 3) or 3))
        min_win_rate = float(realized_cfg.get("min_win_rate", 0.40))
        min_avg = float(realized_cfg.get("min_avg_return_pct", 0.0))
        recent = rets[-recent_n:]
        rn = len(recent)
        ravg = (sum(recent) / rn) if rn else 0.0
        win_rate = (sum(1 for r in recent if r > 0) / rn) if rn else 0.0
        realized_stats.update({
            "n": rn,
            "recent_n": recent_n,
            "min_sample": min_sample,
            "win_rate": win_rate,
            "avg_return_pct": ravg,
            "min_win_rate": min_win_rate,
            "min_avg_return_pct": min_avg,
        })
        if rn < min_sample:
            realized_stats["reason"] = (
                f"n={rn} < {min_sample} realized-stats min-sample"
            )
        elif win_rate < min_win_rate or ravg < min_avg:
            realized_stats["blocked"] = True
            realized_stats["reason"] = (
                f"recent realized stats failed: win_rate={win_rate:.2%} "
                f"(min {min_win_rate:.2%}), avg={ravg:+.4f}% "
                f"(min {min_avg:+.4f}%)"
            )
            return False, stats
        else:
            realized_stats["reason"] = (
                f"recent realized stats passed: win_rate={win_rate:.2%}, "
                f"avg={ravg:+.4f}%"
            )

    if n < min_n:
        if grace_period:
            # Still accumulating — let it fire but flag for size reduction.
            # Skip mean/sharpe gates since the sample is too thin to trust.
            stats["in_grace"] = True
            return True, stats
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


def _exit_already_working_for_pair(conn, strategy_id: str, symbol: str) -> bool:
    """M5 (Sprint 2): True if a SELL exit for (strategy, symbol) is already
    accepted/working — a resting stop, a submitted market exit, or a partial
    fill that hasn't fully closed. Used to suppress redundant exit attempts
    (the 5,868 long_exit signals firing every bar) WITHOUT changing the trading
    decision: the first genuine exit still fires; subsequent ones are no-ops
    while the first is in flight."""
    row = conn.execute(
        "SELECT 1 FROM paper_trades WHERE strategy_id=? AND symbol=? "
        "  AND side='sell' "
        "  AND status IN ('new', 'accepted', 'partially_filled', 'pending_new', "
        "                 'held', 'accepted_for_bidding') LIMIT 1",
        (strategy_id, symbol),
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


def portfolio_heat_usd(conn, *, default_stop_pct: float = 0.05) -> float:
    """Stage 1.2 — total open dollar risk across the book = Σ over open
    positions of qty × (entry − initial stop). This is the 'heat' a correlated
    selloff would realize at once; capping it (vs capping per-trade risk alone)
    is what survives a tech/semi cluster gapping down together. A position with
    no resting stop is charged `default_stop_pct` of its notional so an
    unprotected leg can't read as zero risk.
    """
    rows = conn.execute(
        """
        SELECT o.entry_price AS entry,
               (SELECT COALESCE(SUM(b.qty), 0) FROM paper_trades b
                 WHERE b.signal_id = o.signal_id AND b.side = 'buy') AS qty,
               (SELECT s.stop_price FROM paper_trades s
                 WHERE s.signal_id = o.signal_id AND s.order_type = 'stop'
                   AND s.stop_price IS NOT NULL
                 ORDER BY s.id ASC LIMIT 1) AS stop
          FROM outcomes o
         WHERE o.status = 'open'
        """
    ).fetchall()
    total = 0.0
    for r in rows:
        entry, qty, stop = r["entry"], r["qty"], r["stop"]
        if not entry or not qty:
            continue
        if stop is not None and 0 < stop < entry:
            total += qty * (entry - stop)
        else:
            total += qty * entry * default_stop_pct
    return total


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


_ENTRY_DEAD_STATUSES = {"rejected", "canceled", "cancelled", "expired"}


def _order_status(order) -> str:
    """Lowercased order status, tolerant of Alpaca enums and bare strings."""
    raw = getattr(order, "status", "")
    val = getattr(raw, "value", None)
    s = val if val is not None else str(raw)
    return s.split(".")[-1].lower()


def _entry_is_live(order) -> bool:
    """True when a just-submitted entry order is live/filling (not rejected).

    Stage 0.2: a market BUY may return status='accepted' before the broker
    surfaces the new position, so the stop-attach path reads
    available_to_sell()==0 and would skip the protective stop — the naked-long
    bug (0 of 409 trades had a resting stop). A live entry lets the stop arm at
    the requested qty anyway; the broker still rejects a genuine oversell.
    """
    return _order_status(order) not in _ENTRY_DEAD_STATUSES


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


def _build_default_bars_fetcher(*, lookback_bars: int = 60) -> Callable:
    """Lazy per-symbol daily-bars fetcher backed by monitoring.wide_bars.

    Returns a callable ``symbol -> list[dict(open/high/low/close/volume)]``
    (oldest bar first), or ``[]`` on any failure. Without this, the live
    entry point passes ``bars_fetcher=None`` and every ATR-stop / trailing-
    stop path silently no-ops. Fetches are cached per symbol within the run
    so a symbol that's both signalled and held is fetched once.
    """
    from monitoring import wide_bars
    cache: Dict[str, List[dict]] = {}

    def fetcher(symbol: str) -> List[dict]:
        sym = (symbol or "").upper()
        if not sym:
            return []
        if sym in cache:
            return cache[sym]
        rows: List[dict] = []
        try:
            frames = wide_bars.fetch_wide_daily_bars(
                [sym], lookback_bars=lookback_bars)
            df = frames.get(sym)
            if df is not None and not getattr(df, "empty", True):
                for _, r in df.iterrows():
                    rows.append({
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["volume"]) if "volume" in r else 0.0,
                    })
        except Exception as e:
            log(f"bars_fetcher: daily bars fetch failed for {sym}: {e}",
                "WARNING")
            rows = []
        cache[sym] = rows
        return rows

    return fetcher


def _normalize_order_type(raw) -> str:
    """Coerce settings.order_type to a known string. Unknown → market."""
    if not raw:
        return ORDER_TYPE_MARKET
    v = str(raw).lower().strip()
    if v in SUPPORTED_ORDER_TYPES:
        return v
    log(f"unknown order_type {raw!r}; falling back to market", "WARNING")
    return ORDER_TYPE_MARKET


def _resolve_strategy_class(
    strategy_id: str,
    tracked_strategies: Optional[List[dict]] = None,
) -> Optional[str]:
    """Read the `strategy_class` declaration from TRACKED_STRATEGIES (or
    TREND_DECLARATIONS) for `strategy_id`. Returns None when undeclared."""
    for meta in (tracked_strategies or []):
        if not isinstance(meta, dict):
            continue
        if meta.get("id") != strategy_id:
            continue
        sc = meta.get("strategy_class")
        if sc:
            return str(sc).lower()
    return None


def _resolve_strategy_declaration(
    strategy_id: str,
    tracked_strategies: Optional[List[dict]] = None,
) -> Optional[dict]:
    """Return the TRACKED_STRATEGIES entry for `strategy_id` (or None)."""
    for meta in (tracked_strategies or []):
        if not isinstance(meta, dict):
            continue
        if meta.get("id") == strategy_id:
            return meta
    return None


def _market_regime_to_allocator_regime(market_regime: Optional[str]) -> str:
    """Bridge the daily_reports regime vocabulary to the allocator's.

    daily_reports stores one of {trending_up, trending_down, low_vol,
    choppy, mixed}; the allocator's DEFAULT_ALLOCATIONS uses the same
    keys. Unknown / missing → 'mixed'.
    """
    if not market_regime:
        return "mixed"
    return str(market_regime)


def _process_entry(conn, client, settings: dict, sig, dry_run: bool,
                    *, asof: Optional[date] = None,
                    sleep_fn=None, now_fn=None,
                    data_client=None,
                    portfolio_value: Optional[float] = None,
                    bars_fetcher: Optional[Callable] = None,
                    throttle_multiplier: float = 1.0,
                    market_regime: Optional[str] = None,
                    tracked_strategies: Optional[List[dict]] = None,
                    remaining_bp_budget: Optional[float] = None,
                    remaining_heat_usd: Optional[float] = None) -> dict:
    from monitoring import sizing as sizing_mod
    from monitoring import stops as stops_mod
    from monitoring import crypto_adapter as crypto_mod
    sid, sym = sig["strategy_id"], sig["symbol"]
    sig_bar_interval_top = (sig["bar_interval"]
        if "bar_interval" in sig.keys() else "1d")
    _src = _skip_source_for_bar_interval(sig_bar_interval_top)
    decl = _resolve_strategy_declaration(sid, tracked_strategies)
    grace_period_flag = bool((decl or {}).get("grace_period", False))
    eligible, stats = _is_eligible(conn, sid, settings,
                                   grace_period=grace_period_flag,
                                   bar_interval=sig_bar_interval_top)
    if not eligible:
        _record_skip(
            conn, sig=sig, gate="ineligible",
            reason_detail=(
                f"n={stats.get('n')}, mean={stats.get('mean')}, "
                f"sharpe={stats.get('sharpe')}, "
                f"realized_gate={stats.get('realized_gate')}"
            ),
            source=_src,
        )
        return {"action": "SKIP_INELIGIBLE", "strategy_id": sid, "symbol": sym,
                "reason": "fails edge thresholds", "stats": stats}
    if _already_traded(conn, sig["id"], "buy"):
        _record_skip(
            conn, sig=sig, gate="already_submitted",
            reason_detail=f"signal_id={sig['id']} already has a buy",
            source=_src,
        )
        return {"action": "SKIP_DUPLICATE", "strategy_id": sid, "symbol": sym,
                "signal_id": sig["id"]}
    # M2 (Sprint 3) — single symbol-owner authority (OPTION A: one owner per
    # symbol). If a DIFFERENT strategy already owns this symbol (holds the
    # oldest open buy), reject this entry. Alpaca has ONE broker position per
    # symbol; letting a second strategy in is what created the shared-symbol
    # stacked-exit / wash-trade / oversell-into-short failures. The owner keeps
    # the position; this strategy waits until the symbol is flat. A same-symbol
    # entry by the OWNER never reaches here (process_signals routes it to the
    # pyramid add-on branch first), so this only ever blocks a non-owner.
    from monitoring import position_manager as pm_mod
    owner_conflict = pm_mod.entry_owner_conflict(conn, sid, sym)
    if owner_conflict is not None:
        _record_skip(
            conn, sig=sig, gate="symbol_owned_by_other",
            reason_detail=f"{sym} already owned by {owner_conflict}",
            source=_src,
        )
        return {"action": "SKIP_SYMBOL_OWNED", "strategy_id": sid, "symbol": sym,
                "signal_id": sig["id"], "owner": owner_conflict}
    # 7.5.4 — Intraday confirmation overlay (shadow mode). Record what
    # a 1m-close-above-trigger gate would have decided for this entry.
    # Recorded BEFORE sizing/qty checks so even SKIP_PRICE entries still
    # get a shadow observation. Never affects the live entry path.
    _maybe_record_intraday_confirm_shadow(
        conn, sig=sig, decl=decl, side="long",
    )
    is_crypto = crypto_mod.is_crypto_symbol(sym)
    if is_crypto:
        max_pos_usd = crypto_mod.crypto_max_position_usd(settings)
    else:
        max_pos_usd = float(settings.get("max_position_usd", 1000))
    # Grace period: strategy is firing before it has enough closed outcomes
    # to gauge edge. Reduce the cap so paper-data collection happens at
    # smaller size while the strategy proves itself. Multiplier is global
    # via settings.auto_trade.grace_period_size_multiplier (default 0.25).
    in_grace = bool(stats.get("in_grace", False))
    if in_grace:
        grace_mult = float(settings.get("grace_period_size_multiplier", 0.25))
        max_pos_usd = max_pos_usd * grace_mult
    # 4.7.2 — When the strategy is pyramidable, reserve capacity for the
    # full pyramid ladder by sizing the initial (tier-0) entry to
    # max_position_usd / sum(tier_schedule). That way pyramid add-ons
    # fit under the aggregate cap rather than being refused by the
    # SKIP_PYRAMID_OVER_CAP guard.
    is_pyramidable = bool((decl or {}).get("pyramidable", False))
    if is_pyramidable:
        from monitoring import pyramiding as py_mod
        pyr_settings = (settings.get("pyramiding") or {})
        tier_schedule = pyr_settings.get(
            "tier_schedule", py_mod.DEFAULT_TIER_SCHEDULE,
        )
        max_tiers = int(pyr_settings.get("max_tiers", py_mod.DEFAULT_MAX_TIERS))
        schedule_sum = sum(float(t) for t
                            in list(tier_schedule)[:max_tiers]) or 1.0
        max_pos_usd = max_pos_usd / schedule_sum
    # 4.7.3 — Regime allocator: trend strategies get sized at the trend
    # share of the current regime's allocation; mean-reversion strategies
    # at the mean-reversion share. Other classes (or undeclared) get 1.0.
    strategy_class = _resolve_strategy_class(sid, tracked_strategies)
    regime_multiplier = None
    if strategy_class in ("trend", "mean_reversion", "mean-reversion"):
        regime_multiplier = sizing_mod.resolve_regime_multiplier(
            strategy_class=strategy_class,
            regime=_market_regime_to_allocator_regime(market_regime),
        )
    min_position_usd = float(settings.get("min_position_usd", 0) or 0)
    # 5.5.1 — Intraday sizing tier. EOD entries pass intraday_multiplier=None
    # (no change). Intraday entries (bar_interval != "1d") get a multiplier
    # from the strategy declaration override or settings default (0.5).
    sig_bar_interval = sig["bar_interval"] if "bar_interval" in sig.keys() else "1d"
    # M4 — raise the intraday floor so the post-multiplier notional clears the
    # price of one liquid share (SPY/QQQ etc.). settings.intraday.min_position_usd
    # overrides the shared floor for intraday entries only; EOD is untouched.
    if sig_bar_interval != "1d":
        intraday_cfg = settings.get("intraday")
        if isinstance(intraday_cfg, dict):
            try:
                imp = float(intraday_cfg.get("min_position_usd") or 0)
                if imp > min_position_usd:
                    min_position_usd = imp
            except (TypeError, ValueError):
                pass
    intraday_multiplier = sizing_mod.resolve_intraday_multiplier(
        bar_interval=sig_bar_interval,
        declaration=decl,
        settings_auto_trade=settings,
    )
    # Stage 1.1 — volatility-target sizing needs the initial-stop distance up
    # front (risk-per-share = entry − stop). Compute the stop once here (the same
    # dry-run path the unprotected-entry guard uses) ONLY when atr_risk is the
    # active method, so every other sizing path is unchanged. A missing/invalid
    # stop leaves inputs None and atr_risk falls back to the tiered method.
    atr_risk_inputs = None
    if sizing_mod.normalize_sizing_method(settings.get("sizing_method")) \
            == sizing_mod.SIZING_METHOD_ATR_RISK:
        entry_px = float(sig["close"] or 0)
        size_stop = _maybe_attach_stop(
            conn, client, settings, sig,
            entry_fill=entry_px, qty=1,
            client_order_id=None, bars_fetcher=bars_fetcher, dry_run=True,
            strategy_class=strategy_class, market_regime=market_regime,
        )
        sp = (size_stop or {}).get("stop_price")
        rps = (entry_px - sp) if (sp is not None and entry_px > 0) else None
        atr_risk_inputs = {
            "entry_price": entry_px,
            "risk_per_share": rps if (rps is not None and rps > 0) else None,
            "atr": (size_stop or {}).get("atr"),
            "risk_pct": float(settings.get(
                "risk_per_trade_pct", sizing_mod.DEFAULT_RISK_PER_TRADE_PCT)),
        }
    sizing = sizing_mod.compute_notional(
        conn, sid,
        sizing_method=settings.get("sizing_method"),
        portfolio_value=portfolio_value,
        max_position_usd=max_pos_usd,
        settings_tiered=settings.get("tiered"),
        settings_kelly=settings.get("kelly"),
        regime_multiplier=regime_multiplier,
        strategy_class=strategy_class,
        min_position_usd=min_position_usd,
        intraday_multiplier=intraday_multiplier,
        atr_risk_inputs=atr_risk_inputs,
    )
    if is_crypto:
        sizing["asset_class"] = "crypto"
        sizing["crypto_max_position_usd"] = max_pos_usd
    notional = sizing["notional"] * float(throttle_multiplier)
    sizing["throttle_multiplier"] = float(throttle_multiplier)
    sizing["notional_after_throttle"] = round(notional, 2)
    if notional <= 0:
        _record_skip(
            conn, sig=sig, gate="sizing_zero",
            reason_detail=f"notional={notional} (after throttle)",
            source=_src,
        )
        return {"action": "SKIP_SIZING_ZERO", "strategy_id": sid, "symbol": sym,
                "sizing": sizing}
    qty = _calc_qty(sig["close"], notional)
    if qty < 1:
        # M3 — the shrunken (intraday) notional can't afford one share, but
        # the strategy's REAL cap (max_pos_usd) might. The old veto skipped
        # the best, most liquid high-priced names (SPY/QQQ/NVDA) purely
        # because the intraday position was sized too small — not because the
        # share was genuinely unaffordable. Only skip when even the full cap
        # can't afford a single share; otherwise buy the cap-affordable
        # minimum (1 share). The aggregate buying-power guard below still
        # refuses orders that don't fit the spendable budget.
        cap_qty = _calc_qty(sig["close"], max_pos_usd)
        if cap_qty < 1:
            _record_skip(
                conn, sig=sig, gate="price_too_high",
                reason_detail=(
                    f"cap=${max_pos_usd:.2f}, signal close="
                    f"${sig['close']}" if sig['close'] is not None
                    else f"cap=${max_pos_usd:.2f}, no signal close"
                ),
                source=_src,
            )
            return {"action": "SKIP_PRICE", "strategy_id": sid, "symbol": sym,
                    "price": sig["close"], "max_usd": max_pos_usd,
                    "sizing": sizing}
        qty = 1
        sizing["qty_floored_to_cap_min"] = True

    # Aggregate buying-power guard. The caller tracks notional committed
    # across this run and passes what's left of the account's spendable
    # budget; refuse the entry rather than fire an order the broker would
    # reject for insufficient buying power. None → unbounded (dry-run /
    # no account summary).
    order_notional = qty * float(sig["close"] or 0)
    if (remaining_bp_budget is not None
            and order_notional > remaining_bp_budget + 1e-6):
        _record_skip(
            conn, sig=sig, gate="buying_power",
            reason_detail=(f"order notional ${order_notional:.2f} exceeds "
                           f"remaining budget ${remaining_bp_budget:.2f}"),
            source=_src,
        )
        return {"action": "SKIP_BUYING_POWER", "strategy_id": sid, "symbol": sym,
                "order_notional": round(order_notional, 2),
                "remaining_bp_budget": round(remaining_bp_budget, 2),
                "sizing": sizing}

    # Stage 1.2 — portfolio heat cap. Risk-of-ruin is bounded by TOTAL open risk,
    # not per-trade risk: refuse the entry when it would push Σ(stop distance ×
    # size) past the run's remaining heat budget (default cap 6% of equity). Risk
    # for this entry is qty × stop-distance when known (atr_risk), else a
    # notional fraction. None budget → disabled (no equity / cap not configured).
    rps_for_heat = (atr_risk_inputs or {}).get("risk_per_share")
    if rps_for_heat:
        entry_risk_usd = qty * float(rps_for_heat)
    else:
        entry_risk_usd = order_notional * float(
            settings.get("default_stop_pct_for_heat", 0.05))
    if (remaining_heat_usd is not None
            and entry_risk_usd > remaining_heat_usd + 1e-6):
        _record_skip(
            conn, sig=sig, gate="portfolio_heat",
            reason_detail=(f"entry risk ${entry_risk_usd:.2f} exceeds remaining "
                           f"heat budget ${remaining_heat_usd:.2f}"),
            source=_src,
        )
        return {"action": "SKIP_HEAT_CAP", "strategy_id": sid, "symbol": sym,
                "entry_risk_usd": round(entry_risk_usd, 2),
                "remaining_heat_usd": round(remaining_heat_usd, 2),
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

    # Stop-required guard: if config expects an initial hard stop, prove the
    # stop is computable before submitting the entry. Without this, an ATR data
    # gap (or missing fallback) can open a naked paper position and only alert
    # after the damage is done. A config with no stops enabled is the explicit
    # exemption path and keeps the legacy no-stop behaviour.
    if not dry_run:
        stop_preflight = _maybe_attach_stop(
            conn, client, settings, sig,
            entry_fill=float(sig["close"] or 0),
            qty=qty, client_order_id=client_order_id,
            bars_fetcher=bars_fetcher, dry_run=True,
            strategy_class=strategy_class,
            market_regime=market_regime,
        )
        if stop_preflight is not None and stop_preflight.get("stop_price") is None:
            _record_skip(
                conn, sig=sig, gate="unprotected_entry",
                reason_detail=(
                    f"initial stop unavailable before entry "
                    f"(status={stop_preflight.get('status')})"
                ),
                source=_src,
            )
            return {"action": "SKIP_UNPROTECTED_ENTRY", "strategy_id": sid,
                    "symbol": sym, "signal_id": sig["id"],
                    "reason": "initial stop unavailable before entry",
                    "stop": stop_preflight,
                    "sizing": sizing}

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
            strategy_class=strategy_class,
            market_regime=market_regime,
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
                "notional": round(order_notional, 2),
                "client_order_id": client_order_id,
                "target_execution_utc": target_utc.isoformat() if target_utc else None,
                "entry_time_offset_min": offset_min,
                "order_type": effective_order_type,
                "limit_price": limit_price,
                "requested_order_type": requested_order_type,
                "sizing": sizing,
                "entry_risk_usd": round(entry_risk_usd, 2),
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

    # Stage 0.2 — mark the entry live (not rejected) so the protective stop arms
    # even when the broker hasn't surfaced the fresh position yet (the naked-long
    # fill-settlement race; see _entry_is_live). Buy-status settlement is handled
    # by order_sync.
    entry_filled = _entry_is_live(order)
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
        strategy_class=strategy_class,
        market_regime=market_regime,
        entry_filled=entry_filled,
    )

    # 6.1.1 — record the stop method on the entry row so audit queries
    # like "show me every entry protected by ATR" don't need to join.
    # Stage 0.3 — gate the stamp on an actually-submitted stop. Previously this
    # keyed off stop_method (set the moment a stop PRICE was computed, before
    # submit), so a buy whose stop was skipped/rejected was still stamped
    # 'atr_initial' — 119 buys advertised protection that did not exist. Only
    # stamp when the stop truly rests on the book (status=='submitted').
    if stop_info and stop_info.get("status") == "submitted" \
            and stop_info.get("stop_method"):
        db.record_paper_trade(conn, {
            "alpaca_order_id": str(getattr(order, "id", "")),
            "signal_id": sig["id"],
            "strategy_id": sid, "symbol": sym, "side": "buy", "qty": qty,
            "order_type": effective_order_type,
            "limit_price": limit_price,
            "fill_price": entry_fill,
            "status": str(getattr(order, "status", "submitted")),
            "entry_stops": stop_info["stop_method"],
        })

    # M7 (Sprint 3) — post-fill stop-protection verification. A long that fills
    # without a protective stop is a naked position: a gap-down has no floor
    # (the ENPH/AVGO −16% tail). Verify a stop is actually attached (this run's
    # submit OR a stop already resting at the broker); alert loudly otherwise.
    # Only when stops are EXPECTED for this run — a strategy/config that runs
    # without stops by design raises no false alarm. stop_info is None only when
    # stops are globally disabled, so verification keys off that.
    protection = None
    if stop_info is not None:
        from monitoring import position_manager as pm_protect
        try:
            protection = pm_protect.verify_fill_protected(
                client, symbol=sym, stop_info=stop_info, stops_expected=True,
            )
        except Exception as e:
            log(f"M7 stop-protection verify skipped for {sid}/{sym}: {e}",
                "WARNING")

    return {"action": "BUY", "strategy_id": sid, "symbol": sym, "qty": qty,
            "order_id": str(order.id), "signal_id": sig["id"],
            "notional": round(order_notional, 2),
            "client_order_id": client_order_id,
            "target_execution_utc": target_utc.isoformat() if target_utc else None,
            "entry_time_offset_min": offset_min,
            "order_type": effective_order_type,
            "limit_price": limit_price,
            "requested_order_type": requested_order_type,
            "sizing": sizing,
            "entry_risk_usd": round(entry_risk_usd, 2),
            "stop": stop_info,
            "stop_protection": protection}


def _map_regime_for_pyramiding(market_regime: Optional[str]) -> Optional[str]:
    """Bridge daily_reports' regime vocabulary to pyramiding's friendly-
    regime set ({"bull", "trend"}). trending_up → "bull"; trending_down →
    "bear"; choppy/low_vol/mixed → unchanged (so pyramiding will refuse).
    """
    if not market_regime:
        return None
    r = str(market_regime).lower()
    if r == "trending_up":
        return "bull"
    if r == "trending_down":
        return "bear"
    return r


def _aggregate_open_notional(conn, strategy_id: str, symbol: str) -> float:
    """Sum of (qty × fill_price-or-limit_price) across all open BUYs for
    the (strategy, symbol) pair. Used to enforce that pyramiding doesn't
    breach `auto_trade.max_position_usd` across the *aggregate* position.
    """
    rows = conn.execute(
        "SELECT qty, COALESCE(fill_price, limit_price) AS px "
        "  FROM paper_trades "
        " WHERE strategy_id=? AND symbol=? AND side='buy' "
        "   AND status IN ('filled', 'partially_filled', 'accepted', 'new') ",
        (strategy_id, symbol),
    ).fetchall()
    total = 0.0
    for r in rows:
        if r["qty"] is None or r["px"] is None:
            continue
        total += float(r["qty"]) * float(r["px"])
    return total


def _initial_qty_for_pyramid(
    conn, strategy_id: str, symbol: str,
) -> Optional[int]:
    """Return the qty of the EARLIEST (tier 0) buy in the open pyramid
    chain, used to compute add-on tier sizes. Falls back to the most-
    recent buy when pyramid_tier is uniformly NULL.
    """
    row = conn.execute(
        "SELECT qty FROM paper_trades "
        " WHERE strategy_id=? AND symbol=? AND side='buy' "
        "   AND status IN ('filled', 'partially_filled', 'accepted', 'new') "
        "   AND COALESCE(pyramid_tier, 0) = 0 "
        " ORDER BY submitted_at ASC LIMIT 1",
        (strategy_id, symbol),
    ).fetchone()
    if row is None or row["qty"] is None:
        return None
    try:
        return int(row["qty"])
    except (TypeError, ValueError):
        return None


def _process_pyramid_addon(
    conn, client, settings: dict, sig, dry_run: bool,
    *, tracked_strategies: Optional[List[dict]],
    market_regime: Optional[str],
    asof: Optional[date] = None,
) -> dict:
    """Evaluate a long_entry signal that arrived AFTER an existing open
    position from the same (strategy, symbol). Routes through
    monitoring.pyramiding's evaluate_addon and submits an add-on order
    when all checks pass.
    """
    from monitoring import pyramiding as py_mod
    sid, sym = sig["strategy_id"], sig["symbol"]
    sig_bar_interval_p = (sig["bar_interval"]
        if "bar_interval" in sig.keys() else "1d")
    _src_p = _skip_source_for_bar_interval(sig_bar_interval_p)
    if _already_traded(conn, sig["id"], "buy"):
        _record_skip(
            conn, sig=sig, gate="already_submitted",
            reason_detail=f"signal_id={sig['id']} already has a buy (pyramid)",
            source=_src_p,
        )
        return {"action": "SKIP_DUPLICATE", "strategy_id": sid, "symbol": sym,
                "signal_id": sig["id"]}
    decl = _resolve_strategy_declaration(sid, tracked_strategies)
    strategy_class = (decl or {}).get("strategy_class") or "trend"
    initial_qty = _initial_qty_for_pyramid(conn, sid, sym)
    if initial_qty is None or initial_qty <= 0:
        _record_skip(
            conn, sig=sig, gate="pyramid_no_base",
            reason_detail="no tier-0 BUY to size add-on against",
            source=_src_p,
        )
        return {"action": "SKIP_NO_PYRAMID_BASE",
                "strategy_id": sid, "symbol": sym, "signal_id": sig["id"],
                "reason": "no tier-0 BUY to size add-on against"}
    pyr_settings = (settings.get("pyramiding") or {})
    max_tiers = int(pyr_settings.get("max_tiers", py_mod.DEFAULT_MAX_TIERS))
    tier_schedule = tuple(
        pyr_settings.get("tier_schedule", py_mod.DEFAULT_TIER_SCHEDULE)
    )
    mapped_regime = _map_regime_for_pyramiding(market_regime)
    decision = py_mod.evaluate_addon(
        conn,
        strategy_id=sid, symbol=sym,
        initial_qty=initial_qty,
        regime=mapped_regime,
        declaration=decl,
        direction="long",
        tier_schedule=tier_schedule,
        max_tiers=max_tiers,
        strategy_class=str(strategy_class).lower(),
    )
    if decision["action"] == "VETO_NOT_PYRAMIDABLE":
        _record_skip(
            conn, sig=sig, gate="pyramid_not_pyramidable",
            reason_detail=decision["reason"], source=_src_p,
        )
        return {"action": "SKIP_NO_PYRAMID",
                "strategy_id": sid, "symbol": sym, "signal_id": sig["id"],
                "reason": decision["reason"]}
    if decision["action"] == "VETO_REGIME":
        _record_skip(
            conn, sig=sig, gate="pyramid_regime",
            reason_detail=(
                f"regime={market_regime}; {decision['reason']}"
            ), source=_src_p,
        )
        return {"action": "SKIP_PYRAMID_REGIME",
                "strategy_id": sid, "symbol": sym, "signal_id": sig["id"],
                "current_regime": market_regime,
                "reason": decision["reason"]}
    if decision["action"] == "VETO_MAX_TIERS":
        _record_skip(
            conn, sig=sig, gate="pyramid_max_tiers",
            reason_detail=(
                f"tier={decision['tier']}, max={max_tiers}; "
                f"{decision['reason']}"
            ), source=_src_p,
        )
        return {"action": "SKIP_MAX_TIERS",
                "strategy_id": sid, "symbol": sym, "signal_id": sig["id"],
                "tier": decision["tier"],
                "max_tiers": max_tiers,
                "reason": decision["reason"]}
    # decision["action"] == "ADDON"
    addon_qty = int(decision["qty"])
    addon_tier = int(decision["tier"])
    addon_price = float(sig["close"] or 0)
    if addon_price <= 0:
        _record_skip(
            conn, sig=sig, gate="pyramid_no_price",
            reason_detail="no usable price on the entry signal",
            source=_src_p,
        )
        return {"action": "SKIP_PYRAMID_PRICE",
                "strategy_id": sid, "symbol": sym, "signal_id": sig["id"],
                "reason": "no usable price on the entry signal"}
    addon_notional = addon_qty * addon_price
    open_notional = _aggregate_open_notional(conn, sid, sym)
    cap_usd = float(settings.get("max_position_usd", 1000))
    if open_notional + addon_notional > cap_usd + 1e-6:
        _reason_over_cap = (
            f"add-on tier {addon_tier} ({addon_notional:.2f}) would "
            f"push aggregate ({open_notional:.2f}) over the "
            f"max_position_usd cap of {cap_usd:.2f}"
        )
        _record_skip(
            conn, sig=sig, gate="pyramid_over_cap",
            reason_detail=_reason_over_cap, source=_src_p,
        )
        return {"action": "SKIP_PYRAMID_OVER_CAP",
                "strategy_id": sid, "symbol": sym, "signal_id": sig["id"],
                "tier": addon_tier,
                "open_notional": round(open_notional, 2),
                "addon_notional": round(addon_notional, 2),
                "cap_usd": round(cap_usd, 2),
                "reason": _reason_over_cap}
    client_order_id = _build_client_order_id(
        strategy_id=sid, symbol=sym, side="buy",
        bar_ts=sig["bar_ts"], target_utc=None,
    )
    client_order_id = (client_order_id + f"-p{addon_tier}")[:MAX_CLIENT_ORDER_ID_LEN]
    if dry_run:
        log(f"[DRY-RUN] PYRAMID_ADDON tier={addon_tier} BUY {addon_qty} "
            f"{sym} @ ~${addon_price:.2f} for {sid}", "INFO")
        return {"action": "PYRAMID_ADDON",
                "strategy_id": sid, "symbol": sym, "signal_id": sig["id"],
                "qty": addon_qty, "tier": addon_tier,
                "price": addon_price,
                "client_order_id": client_order_id,
                "open_notional_before": round(open_notional, 2),
                "addon_notional": round(addon_notional, 2),
                "dry_run": True}
    try:
        order = _submit_market_order(
            client, symbol=sym, qty=addon_qty, side="buy",
            client_order_id=client_order_id,
        )
    except Exception as e:
        log(f"pyramid add-on submit failed for {sid}/{sym}: {e}", "ERROR")
        return {"action": "ERROR", "strategy_id": sid, "symbol": sym,
                "error": str(e)[:200]}
    fill_price = float(getattr(order, "filled_avg_price", 0) or 0) or None
    paper_trade_id = db.record_paper_trade(conn, {
        "alpaca_order_id": str(getattr(order, "id", "")),
        "signal_id": sig["id"],
        "strategy_id": sid, "symbol": sym, "side": "buy", "qty": addon_qty,
        "order_type": "market",
        "fill_price": fill_price,
        "submitted_at": str(getattr(order, "submitted_at", _utc_now())),
        "status": str(getattr(order, "status", "submitted")),
        "notes": (f"auto-pyramid tier={addon_tier} on bar_ts={sig['bar_ts']}; "
                   f"add-on of {addon_qty} on top of open {open_notional:.2f}"),
    })
    if paper_trade_id is not None:
        from monitoring import pyramiding as py_mod
        py_mod.record_addon_tier(conn, paper_trade_id=paper_trade_id,
                                  tier=addon_tier)
    log(f"PYRAMID_ADDON tier={addon_tier} BUY {addon_qty} {sym}: "
        f"{getattr(order, 'id', '?')}", "SUCCESS")
    return {"action": "PYRAMID_ADDON",
            "strategy_id": sid, "symbol": sym, "signal_id": sig["id"],
            "qty": addon_qty, "tier": addon_tier,
            "order_id": str(getattr(order, "id", "")),
            "client_order_id": client_order_id,
            "open_notional_before": round(open_notional, 2),
            "addon_notional": round(addon_notional, 2)}


def _resolve_trailing_config(
    strategy_id: str,
    settings: dict,
    tracked_strategies: Optional[List[dict]] = None,
) -> Optional[dict]:
    """Return the per-strategy trailing-stop config, or None when the strategy
    doesn't opt into trailing.

    Resolution order: per-strategy declaration in TRACKED_STRATEGIES (or
    TREND_DECLARATIONS), falling back to the global
    settings.auto_trade.trailing_stop block. Missing `method` ⇒ None.
    """
    cfg: dict = {}
    for meta in (tracked_strategies or []):
        if not isinstance(meta, dict):
            continue
        if meta.get("id") != strategy_id:
            continue
        per_strat = meta.get("trailing_stop")
        if isinstance(per_strat, dict):
            cfg.update(per_strat)
        break
    global_cfg = settings.get("trailing_stop")
    if isinstance(global_cfg, dict):
        for k, v in global_cfg.items():
            cfg.setdefault(k, v)
    method = cfg.get("method")
    if not method:
        return None
    return cfg


def _entry_time_stop_floor(conn, strategy_id: str, symbol: str) -> Optional[float]:
    """The most-recent open STOP order's stop_price for (strategy, symbol).
    Used as the floor below which the trailing stop is never allowed to slip."""
    row = conn.execute(
        "SELECT stop_price FROM paper_trades "
        " WHERE strategy_id=? AND symbol=? "
        "   AND order_type LIKE '%stop%' "
        "   AND stop_price IS NOT NULL "
        "   AND status NOT IN ('canceled', 'rejected', 'filled', 'expired') "
        " ORDER BY submitted_at DESC LIMIT 1",
        (strategy_id, symbol),
    ).fetchone()
    if row is None or row["stop_price"] is None:
        return None
    try:
        return float(row["stop_price"])
    except (TypeError, ValueError):
        return None


def _advance_trailing_stop_for_position(
    conn, *, strategy_id: str, symbol: str,
    entry_price: float, trailing_cfg: dict,
    bars_fetcher: Callable, now_iso: Optional[str] = None,
) -> Optional[dict]:
    """Fetch fresh bars, advance the trailing stop with ratchet semantics,
    and floor it against the entry-time ATR stop. Returns the new state or
    None when bars are unavailable / insufficient.
    """
    from monitoring import trailing_stops as ts_mod
    try:
        bars = bars_fetcher(symbol)
    except Exception as e:
        log(f"trailing stop: bars fetch failed for {strategy_id}/{symbol}: {e}",
            "WARNING")
        return None
    if not bars:
        return None
    method = str(trailing_cfg.get("method") or ts_mod.DEFAULT_METHOD).lower()
    multiplier = float(trailing_cfg.get("multiplier",
                                         ts_mod.DEFAULT_ATR_MULTIPLIER))
    pct = float(trailing_cfg.get("pct", ts_mod.DEFAULT_PCT_TRAIL))
    chandelier_lookback = int(trailing_cfg.get(
        "chandelier_lookback", ts_mod.DEFAULT_CHANDELIER_LOOKBACK))
    atr_period = trailing_cfg.get("atr_period")
    new_state = ts_mod.advance_stop(
        conn, strategy_id=strategy_id, symbol=symbol,
        entry_price=entry_price, bars=bars, method=method, side="long",
        multiplier=multiplier, pct=pct,
        chandelier_lookback=chandelier_lookback,
        atr_period=atr_period,
        now_iso=now_iso,
    )
    if new_state is None:
        return None
    floor = _entry_time_stop_floor(conn, strategy_id, symbol)
    if floor is not None and new_state["stop_price"] < floor:
        new_state = ts_mod.upsert_stop(
            conn,
            strategy_id=strategy_id, symbol=symbol,
            method=new_state["method"],
            stop_price=floor,
            extreme_price=new_state["extreme_price"],
            side=new_state["side"],
            now_iso=now_iso,
        )
        new_state["floored_to_entry_stop"] = True
    return new_state


def _update_trailing_stops_for_open_positions(
    conn, settings: dict,
    *, bars_fetcher: Optional[Callable],
    tracked_strategies: Optional[List[dict]] = None,
    now_iso: Optional[str] = None,
) -> List[dict]:
    """Walk every open paper_trades buy. For strategies with a trailing-stop
    config, advance their trailing stop using the latest bars. Returns a list
    of update descriptors (one per position) for logging.
    """
    if bars_fetcher is None:
        return []
    rows = conn.execute(
        "SELECT DISTINCT strategy_id, symbol FROM paper_trades "
        " WHERE side='buy' "
        "   AND status IN ('filled', 'partially_filled', 'accepted', 'new') "
    ).fetchall()
    updates: List[dict] = []
    for r in rows:
        sid, sym = r["strategy_id"], r["symbol"]
        if not sid or not sym:
            continue
        # P8 — one broker symbol has one trailing-stop authority. Legacy state
        # can still show multiple strategy holders for a symbol; only the
        # first/priority owner may advance a trailing-stop row.
        try:
            from monitoring import position_manager as pm_owner
            if not pm_owner.owns_symbol(conn, sid, sym):
                owner = pm_owner.symbol_owner(conn, sym)
                log(f"P8 trailing-stop guard: skip non-owner {sid}/{sym} "
                    f"(owner={owner})", "WARNING")
                continue
        except Exception as e:
            log(f"P8 trailing-stop owner check skipped for {sid}/{sym}: {e}",
                "WARNING")
        cfg = _resolve_trailing_config(sid, settings, tracked_strategies)
        if cfg is None:
            continue
        open_buy = _open_buy_for_pair(conn, sid, sym)
        entry_price = (
            float(open_buy["fill_price"]) if open_buy["fill_price"] is not None
            else float(open_buy["limit_price"]) if open_buy["limit_price"]
            else None
        )
        if entry_price is None or entry_price <= 0:
            continue
        new_state = _advance_trailing_stop_for_position(
            conn, strategy_id=sid, symbol=sym,
            entry_price=entry_price, trailing_cfg=cfg,
            bars_fetcher=bars_fetcher, now_iso=now_iso,
        )
        if new_state is None:
            continue
        updates.append({
            "strategy_id": sid, "symbol": sym,
            "method": new_state["method"],
            "stop_price": new_state["stop_price"],
            "extreme_price": new_state["extreme_price"],
            "floored": bool(new_state.get("floored_to_entry_stop")),
        })
    return updates


def _check_trailing_exit(
    conn, *, strategy_id: str, symbol: str, current_price: float,
    strategy_meta: Optional[dict] = None,
    bar_low: Optional[float] = None,
    bar_high: Optional[float] = None,
) -> Optional[dict]:
    """If a trailing stop is in force and current_price has crossed it,
    return the trip descriptor; else None.

    6.4.1 — When `strategy_meta` has `sar_overlay: true`, also consult
    the Parabolic SAR overlay engine. The exit fires on
    `trailing_stop_hit OR sar_flip` (whichever first). The trip
    descriptor's `reason` field reports which.
    """
    from monitoring import trailing_stops as ts_mod
    from monitoring import sar_overlay as sar_mod
    trailing_hit = ts_mod.should_exit_on_trailing_stop(
        conn, strategy_id=strategy_id, symbol=symbol,
        current_price=current_price,
    )
    sar_enabled = sar_mod.strategy_has_sar_overlay(strategy_meta)
    if not trailing_hit and not sar_enabled:
        return None
    if not sar_enabled:
        row = ts_mod.get_stop(conn, strategy_id=strategy_id, symbol=symbol)
        return {
            "stop_price": row["stop_price"] if row else None,
            "method": row["method"] if row else None,
            "extreme_price": row["extreme_price"] if row else None,
            "reason": "trailing_stop_hit",
            "sar_flip": False,
        }
    overlay = sar_mod.should_exit_with_sar_overlay(
        conn, strategy_id=strategy_id, symbol=symbol,
        current_price=current_price,
        bar_low=bar_low, bar_high=bar_high,
        trailing_stop_hit=trailing_hit,
    )
    if not overlay["should_exit"]:
        return None
    row = ts_mod.get_stop(conn, strategy_id=strategy_id, symbol=symbol)
    return {
        "stop_price": row["stop_price"] if row else None,
        "method": row["method"] if row else None,
        "extreme_price": row["extreme_price"] if row else None,
        "reason": overlay["reason"],
        "sar_flip": overlay["sar_flip"],
        "sar": overlay.get("sar"),
    }


def _open_outcome_for_pair(conn, strategy_id: str, symbol: str):
    """Most recent OPEN outcome for (strategy, symbol) with entry ts/price,
    or None. Used to window MFE/MAE on a trailing-stop close (F5)."""
    return conn.execute(
        "SELECT o.signal_id AS signal_id, o.entry_ts AS entry_ts, "
        "       o.entry_price AS entry_price "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='open' AND s.strategy_id=? AND s.symbol=? "
        " ORDER BY o.entry_ts DESC LIMIT 1",
        (strategy_id, symbol),
    ).fetchone()


def _flatten_paused_holdings(
    conn, client, *, dry_run: bool,
    resolve_client=None,
) -> List[dict]:
    """M5 (Sprint 3) — enforce the paused-strategy position policy.

    Pause must mean BOTH "no new entries" AND "no silent holding". The entry
    gate already refuses entries; this closes the other half: for every PAUSED
    strategy that still OWNS a holding, flatten it via the owner authority
    (`safe_submit_sell` — reconciles resting orders, caps to broker-available,
    nets the run ledger, never oversells past flat). Once flat, the symbol is no
    longer owned, so M2's owner gate also stops any new stop-arming for it.

    Runs once per pass BEFORE the signal loop so a paused strategy's stale carry
    is cleared before anything else acts on it. Best-effort per holding — one
    bad flatten never blocks the rest. Returns one record per holding acted on.

    `resolve_client(strategy_id)` lets the live path route a flatten to the
    same (paper/live) client the strategy trades on; defaults to `client`.
    """
    from monitoring import strategy_health as sh_mod
    from monitoring import position_manager as pm_mod
    out: List[dict] = []
    if dry_run:
        return out
    resolve_client = resolve_client or (lambda _sid: client)
    try:
        paused = sh_mod.list_paused(conn)
    except Exception as e:
        log(f"_flatten_paused_holdings: list_paused failed: {e}", "WARNING")
        return out
    for p in paused:
        sid = p.get("strategy_id")
        if not sid:
            continue
        try:
            symbols = pm_mod.owned_symbols_for(conn, sid)
        except Exception as e:
            log(f"_flatten_paused_holdings: owned_symbols_for({sid}) "
                f"failed: {e}", "WARNING")
            continue
        for sym in symbols:
            open_buy = _open_buy_for_pair(conn, sid, sym)
            if open_buy is None:
                continue
            req_qty = int(open_buy["qty"] or 0)
            if req_qty < 1:
                continue
            try:
                strat_client = resolve_client(sid)
            except Exception:
                strat_client = client
            try:
                res = pm_mod.safe_submit_sell(
                    strat_client, symbol=sym, requested_qty=req_qty,
                    submit_fn=_submit_market_order,
                )
            except Exception as e:
                log(f"_flatten_paused_holdings: flatten {sid}/{sym} "
                    f"failed: {e}", "ERROR")
                out.append({"action": "PAUSE_FLATTEN_ERROR",
                            "strategy_id": sid, "symbol": sym,
                            "error": str(e)[:200]})
                continue
            if res is None or res.get("action") != "SUBMITTED":
                out.append({"action": "PAUSE_FLATTEN_SKIP",
                            "strategy_id": sid, "symbol": sym,
                            "requested_qty": req_qty,
                            "available": (res or {}).get("available")})
                continue
            order = res["order"]
            qty = res["qty"]
            db.record_paper_trade(conn, {
                "alpaca_order_id": str(getattr(order, "id", "")),
                "strategy_id": sid, "symbol": sym, "side": "sell", "qty": qty,
                "order_type": "market",
                "submitted_at": str(getattr(order, "submitted_at", _utc_now())),
                "status": str(getattr(order, "status", "submitted")),
                "notes": f"M5 pause-flatten ({p.get('reason') or 'paused'})",
            })
            try:
                from monitoring import trailing_stops as ts_mod
                ts_mod.clear_stop(conn, strategy_id=sid, symbol=sym)
            except Exception:
                pass
            log(f"M5 pause-flatten: SELL {qty} {sym} for paused {sid} "
                f"({order.id})", "SUCCESS")
            out.append({"action": "PAUSE_FLATTEN", "strategy_id": sid,
                        "symbol": sym, "qty": qty,
                        "order_id": str(order.id)})
    return out


def _process_exit(
    conn, client, settings: dict, sig, dry_run: bool,
    *, trailing_triggered: Optional[dict] = None,
    exit_reason_override: Optional[str] = None,
    bars_fetcher: Optional[Callable] = None,
) -> dict:
    sid, sym = sig["strategy_id"], sig["symbol"]
    _src_x = _skip_source_for_bar_interval(
        sig["bar_interval"] if "bar_interval" in sig.keys() else "1d"
    )
    if _already_traded(conn, sig["id"], "sell"):
        _record_skip(
            conn, sig=sig, gate="already_submitted",
            reason_detail=f"signal_id={sig['id']} already has a sell",
            source=_src_x,
        )
        return {"action": "SKIP_DUPLICATE", "strategy_id": sid, "symbol": sym,
                "signal_id": sig["id"]}
    # If the caller hasn't pre-computed a trailing trip (and isn't forcing a
    # specific exit reason, e.g. a time-stop), check trailing now using the
    # signal's close as the proxy current price.
    if trailing_triggered is None and exit_reason_override is None:
        current_price = float(sig["close"] or 0)
        if current_price > 0:
            trailing_triggered = _check_trailing_exit(
                conn, strategy_id=sid, symbol=sym,
                current_price=current_price,
            )
    open_buy = _open_buy_for_pair(conn, sid, sym)
    if open_buy is None:
        # F7 (audit 2026-06-03): being flat when an exit signal fires is the
        # NORMAL case, not a noteworthy skip — the exit scanner emits one per
        # (strategy, symbol, bar) and persisting each bloated intraday_skips
        # with 187,814 pure-noise rows. Skip the control flow (decision
        # unchanged) WITHOUT writing the DB row.
        return {"action": "SKIP_NO_POSITION", "strategy_id": sid, "symbol": sym}

    # M2 (Sprint 3) — owner authority on the exit side. Only the single owner of
    # a symbol (the first/priority holder) may submit an exit/stop/flatten for
    # it. A non-owner that holds a legacy shared position (multiple strategies on
    # one symbol pre-M2) must NOT fire its own SELL against the ONE shared broker
    # position — that is the duplicate-exit / wash-trade source. Its exit is
    # suppressed; the owner's exit flattens the shared position. A forced exit
    # (trailing/time-stop override) is still gated: a non-owner can't force a
    # sell it doesn't control. New positions are single-owner by construction
    # (M2 entry gate), so this only ever fires on legacy shared symbols.
    from monitoring import position_manager as pm_owner
    if not pm_owner.owns_symbol(conn, sid, sym):
        owner = pm_owner.symbol_owner(conn, sym)
        return {"action": "SKIP_NOT_OWNER", "strategy_id": sid, "symbol": sym,
                "signal_id": sig["id"], "owner": owner}

    # M5 (Sprint 2): an exit is already accepted/working for this pair (a
    # resting stop or an in-flight market sell). Suppress this redundant exit
    # signal — no duplicate order, no skip-row write — so the 5,868/day exit
    # signals don't stack conflicting SELLs. The genuine first exit already
    # fired; this one is a no-op until that one resolves. A forced exit
    # (trailing/time-stop override) still proceeds, since those are deliberate.
    if (trailing_triggered is None and exit_reason_override is None
            and _exit_already_working_for_pair(conn, sid, sym)):
        return {"action": "SKIP_EXIT_ALREADY_WORKING", "strategy_id": sid,
                "symbol": sym, "signal_id": sig["id"]}
    qty = int(open_buy["qty"])

    exit_reason = "long_exit_signal"
    notes_extra = ""
    if trailing_triggered is not None:
        exit_reason = "trailing_stop"
        notes_extra = (
            f"; trailing stop hit @ ${trailing_triggered.get('stop_price')} "
            f"({trailing_triggered.get('method')})"
        )
    elif exit_reason_override:
        # A5 (audit 2026-06-03): the caller forces the exit reason for a
        # bounded model exit (e.g. 'time_stop' on a 1d trend outcome that
        # never tripped its trailing stop and never reversed).
        exit_reason = exit_reason_override
        notes_extra = f"; {exit_reason_override}"

    if dry_run:
        tag = "TRAILING_STOP " if trailing_triggered is not None else ""
        log(f"[DRY-RUN] {tag}SELL {qty} {sym} (close position from "
            f"{open_buy['submitted_at'][:10]}) for {sid}", "INFO")
        out = {"action": "DRY_SELL", "strategy_id": sid, "symbol": sym,
               "qty": qty, "signal_id": sig["id"],
               "exit_reason": exit_reason,
               "from_order_id": open_buy["alpaca_order_id"]}
        if trailing_triggered is not None:
            out["trailing"] = trailing_triggered
        return out

    # M1 (Sprint 2): route every exit through the single per-symbol
    # reservation layer. It cancels any conflicting resting SELL (wash-trade
    # guard), re-reads the broker's net-available qty, and caps the sell so we
    # never oversell a position that another strategy/stop already exited
    # (the unintended-short root cause). A 0-available result is a clean SKIP,
    # not an error — the position is already flat or fully reserved.
    from monitoring import position_manager as pm_mod
    try:
        res = pm_mod.safe_submit_sell(
            client, symbol=sym, requested_qty=qty,
            submit_fn=_submit_market_order,
        )
    except Exception as e:
        log(f"order submit failed for {sid}/{sym}: {e}", "ERROR")
        return {"action": "ERROR", "strategy_id": sid, "symbol": sym,
                "error": str(e)[:200]}
    if res is None or res.get("action") != "SUBMITTED":
        log(f"exit suppressed for {sid}/{sym}: no broker-available qty "
            f"(requested {qty})", "INFO")
        try:
            from monitoring import trailing_stops as ts_mod
            ts_mod.clear_stop(conn, strategy_id=sid, symbol=sym)
        except Exception:
            pass
        return {"action": "SKIP_NO_AVAILABLE_QTY", "strategy_id": sid,
                "symbol": sym, "signal_id": sig["id"], "requested_qty": qty,
                "available": (res or {}).get("available", 0)}
    order = res["order"]
    qty = res["qty"]

    db.record_paper_trade(conn, {
        "alpaca_order_id": str(getattr(order, "id", "")),
        "signal_id": sig["id"],
        "strategy_id": sid, "symbol": sym, "side": "sell", "qty": qty,
        "order_type": "market",
        "submitted_at": str(getattr(order, "submitted_at", _utc_now())),
        "status": str(getattr(order, "status", "submitted")),
        "notes": f"auto-exit on bar_ts={sig['bar_ts']}; "
                 f"closing buy {open_buy['alpaca_order_id']}{notes_extra}",
    })
    # Clear the trailing stop row now that the position is closing.
    try:
        from monitoring import trailing_stops as ts_mod
        ts_mod.clear_stop(conn, strategy_id=sid, symbol=sym)
    except Exception as e:
        log(f"trailing stop clear failed for {sid}/{sym}: {e}", "WARNING")
    # F5 (audit 2026-06-03): on a trailing-stop exit, close the outcome HERE
    # with exit_reason='trailing_stop' (+ MFE/MAE) so the later generic 1d
    # signal-exit reconcile finds no open outcome and can't overwrite the
    # reason as 'long_exit_signal' / strip excursion.
    #
    # A2 (audit 2026-06-03): a PLAIN intraday signal exit
    # (trailing_triggered is None, bar_interval != '1d') ALSO closes its
    # outcome here. Previously it was "left for the reconcile", but no
    # reconcile pass ever closes intraday outcomes (the 1d EOD reconcile
    # filters bar_interval='1d'; the intraday reconcile runs open_only) so
    # the broker position closed while the outcome stranded OPEN forever.
    # 1d plain exits stay owned by the EOD 1d reconcile (skipped here) to
    # avoid double-closing.
    _exit_interval = (sig["bar_interval"]
                      if "bar_interval" in sig.keys() else "1d")
    _is_intraday_exit = str(_exit_interval or "1d").lower() != "1d"
    # A5: a forced exit reason (e.g. time_stop) also closes the outcome here,
    # so a bounded model exit lands on the ledger with the right reason.
    _close_outcome_here = (
        trailing_triggered is not None or _is_intraday_exit
        or bool(exit_reason_override)
    )
    if _close_outcome_here:
        try:
            outcome = _open_outcome_for_pair(conn, sid, sym)
            if outcome is not None:
                exit_ts = str(getattr(order, "filled_at", None)
                              or getattr(order, "submitted_at", None)
                              or _utc_now())
                exit_price = getattr(order, "filled_avg_price", None)
                if exit_price in (None, ""):
                    exit_price = float(sig["close"] or 0) or None
                if exit_price is not None:
                    mfe = mae = None
                    if bars_fetcher is not None:
                        try:
                            from monitoring import excursion
                            bars = bars_fetcher(sym)
                            mfe, mae = excursion.compute_mfe_mae(
                                bars, entry_price=outcome["entry_price"],
                                entry_ts=outcome["entry_ts"], exit_ts=exit_ts,
                                side="long",
                            )
                        except Exception:
                            mfe = mae = None
                    db.close_outcome(
                        conn, signal_id=int(outcome["signal_id"]),
                        exit_ts=exit_ts, exit_price=float(exit_price),
                        exit_reason=exit_reason, mfe_pct=mfe, mae_pct=mae,
                    )
        except Exception as e:
            log(f"exit outcome close failed for {sid}/{sym} "
                f"(sell still recorded): {e}", "WARNING")
    log(f"SELL {qty} {sym} order submitted: {order.id}", "SUCCESS")
    out = {"action": "SELL", "strategy_id": sid, "symbol": sym, "qty": qty,
           "order_id": str(order.id), "signal_id": sig["id"],
           "exit_reason": exit_reason}
    if trailing_triggered is not None:
        out["trailing"] = trailing_triggered
    return out


def _check_trailing_exits_for_open_positions(
    conn, settings: dict,
    *, client, dry_run: bool,
    bars_fetcher: Optional[Callable],
    tracked_strategies: Optional[List[dict]] = None,
    asof: Optional[date] = None,
) -> List[dict]:
    """For every open position whose strategy uses trailing stops, check if
    the latest bar's close crossed the stop. If so, synthesize a long_exit
    signal and route it through _process_exit.

    This is what makes trailing stops fire on bars where the strategy
    itself didn't emit an exit signal.

    6.4.2 — Also records a SAR shadow A/B row for any open position whose
    strategy declares ``sar_overlay: "shadow"``. The shadow check runs
    independently of the live exit decision — when SAR would have fired
    on the latest bar but the trailing stop didn't, a parallel
    ``paper_trades_sar_overlay`` row captures the hypothetical exit so
    Ross can compare 30 days of "SAR overlay on" vs "off" without
    disturbing live PnL.
    """
    if bars_fetcher is None:
        return []
    rows = conn.execute(
        "SELECT DISTINCT strategy_id, symbol FROM paper_trades "
        " WHERE side='buy' "
        "   AND status IN ('filled', 'partially_filled', 'accepted', 'new') "
    ).fetchall()
    actions: List[dict] = []
    for r in rows:
        sid, sym = r["strategy_id"], r["symbol"]
        if not sid or not sym:
            continue
        if _open_buy_for_pair(conn, sid, sym) is None:
            continue
        cfg = _resolve_trailing_config(sid, settings, tracked_strategies)
        if cfg is None:
            continue
        try:
            bars = bars_fetcher(sym)
        except Exception:
            continue
        if not bars:
            continue
        last_close = float(bars[-1].get("close") or 0)
        if last_close <= 0:
            continue
        last_bar = bars[-1]
        bar_low = _safe_float(last_bar.get("low"))
        bar_high = _safe_float(last_bar.get("high"))
        trip = _check_trailing_exit(
            conn, strategy_id=sid, symbol=sym, current_price=last_close,
        )
        # 6.4.2 — shadow A/B record runs even when the real exit doesn't.
        _maybe_record_sar_shadow(
            conn, strategy_id=sid, symbol=sym,
            current_price=last_close,
            bar_low=bar_low, bar_high=bar_high,
            real_trip=trip,
            tracked_strategies=tracked_strategies,
        )
        if trip is None:
            continue
        # Has a long_exit signal for this bar from THIS strategy already
        # been processed in this run? If so, skip — _process_exit already
        # handled it (and saw the trailing trigger via the in-line check).
        synthetic_sig = {
            "id": None,
            "strategy_id": sid, "symbol": sym,
            "signal_type": "long_exit",
            "bar_ts": (asof or date.today()).isoformat(),
            "close": last_close,
        }
        # Dedupe: synthetic exits aren't tied to a signal_id; we guard via
        # _open_buy_for_pair (returns None once SELL is submitted).
        action = _process_exit(
            conn, client, settings, synthetic_sig, dry_run,
            trailing_triggered=trip, bars_fetcher=bars_fetcher,
        )
        action["synthetic_trailing_exit"] = True
        actions.append(action)
    return actions


def _resolve_time_stop_config(
    strategy_id: str,
    settings: dict,
    tracked_strategies: Optional[List[dict]] = None,
) -> Optional[dict]:
    """Per-strategy time-stop config, or None when the strategy doesn't opt in.

    Resolution: per-strategy declaration `time_stop` block first, then the
    global settings.auto_trade.time_stop block. A positive `max_days_held`
    is required to be active.
    """
    cfg: dict = {}
    decl = _resolve_strategy_declaration(strategy_id, tracked_strategies)
    if decl is not None and isinstance(decl.get("time_stop"), dict):
        cfg.update(decl["time_stop"])
    global_cfg = settings.get("time_stop")
    if isinstance(global_cfg, dict):
        for k, v in global_cfg.items():
            cfg.setdefault(k, v)
    try:
        max_days = int(cfg.get("max_days_held") or 0)
    except (TypeError, ValueError):
        return None
    if max_days <= 0:
        return None
    cfg["max_days_held"] = max_days
    return cfg


def _days_held(entry_ts, asof: date) -> Optional[int]:
    """Calendar days between an outcome's entry_ts (ISO date/datetime) and
    asof. Returns None when entry_ts is unparseable."""
    if entry_ts is None:
        return None
    try:
        entry_date = date.fromisoformat(str(entry_ts)[:10])
    except (ValueError, TypeError):
        return None
    return (asof - entry_date).days


def _check_time_stops_for_open_positions(
    conn, settings: dict,
    *, client, dry_run: bool,
    bars_fetcher: Optional[Callable] = None,
    tracked_strategies: Optional[List[dict]] = None,
    asof: Optional[date] = None,
) -> List[dict]:
    """A5 (audit 2026-06-03): bounded model exit for trend (and any
    time_stop-declaring) strategies. For every open position whose strategy
    declares a time_stop, close it (broker sell + outcome) with
    exit_reason='time_stop' once it has been held > max_days_held.

    Runs AFTER the trailing-exit pass so the ATR trailing stop always wins
    when it trips first (a tripped trailing exit already closed the buy, so
    _open_buy_for_pair returns None and this pass skips). Synthesizes a
    long_exit routed through _process_exit with exit_reason_override so the
    full close path (sell, stop clear, MFE/MAE, outcome close) is reused.

    Idempotent: once the SELL is submitted the position drops out.
    """
    asof = asof or date.today()
    rows = conn.execute(
        "SELECT DISTINCT strategy_id, symbol FROM paper_trades "
        " WHERE side='buy' "
        "   AND status IN ('filled', 'partially_filled', 'accepted', 'new') "
    ).fetchall()
    actions: List[dict] = []
    for r in rows:
        sid, sym = r["strategy_id"], r["symbol"]
        if not sid or not sym:
            continue
        cfg = _resolve_time_stop_config(sid, settings, tracked_strategies)
        if cfg is None:
            continue
        open_buy = _open_buy_for_pair(conn, sid, sym)
        if open_buy is None:
            continue
        outcome = _open_outcome_for_pair(conn, sid, sym)
        if outcome is None:
            continue
        held = _days_held(outcome["entry_ts"], asof)
        if held is None or held <= cfg["max_days_held"]:
            continue
        # Proxy current price for the synthetic exit: latest bar close when a
        # fetcher is available, else the outcome entry price (close still
        # records; MFE/MAE best-effort).
        last_close = None
        if bars_fetcher is not None:
            try:
                bars = bars_fetcher(sym)
                if bars:
                    last_close = float(bars[-1].get("close") or 0) or None
            except Exception:
                last_close = None
        if last_close is None:
            try:
                last_close = float(outcome["entry_price"] or 0) or None
            except (TypeError, ValueError):
                last_close = None
        if last_close is None:
            continue
        synthetic_sig = {
            "id": None,
            "strategy_id": sid, "symbol": sym,
            "signal_type": "long_exit",
            "bar_ts": asof.isoformat(),
            "close": last_close,
        }
        action = _process_exit(
            conn, client, settings, synthetic_sig, dry_run,
            exit_reason_override="time_stop", bars_fetcher=bars_fetcher,
        )
        action["synthetic_time_stop"] = True
        action["days_held"] = held
        actions.append(action)
    return actions


def _resolve_max_loss_cap_config(
    strategy_id: str,
    settings: dict,
    tracked_strategies: Optional[List[dict]] = None,
) -> Optional[dict]:
    """M10 (Sprint 3) — per-strategy hard max-loss cap config, or None when
    the strategy doesn't opt in.

    Resolution mirrors time_stop / trailing_stop: a per-strategy declaration
    `max_loss_cap` block in TRACKED_STRATEGIES wins, then the global
    settings.max_loss_cap block. A positive `max_loss_pct` is required to be
    active; 0 / null / negative at either level disables the cap.
    """
    cfg: dict = {}
    decl = _resolve_strategy_declaration(strategy_id, tracked_strategies)
    if decl is not None and isinstance(decl.get("max_loss_cap"), dict):
        cfg.update(decl["max_loss_cap"])
    global_cfg = settings.get("max_loss_cap")
    if isinstance(global_cfg, dict):
        for k, v in global_cfg.items():
            cfg.setdefault(k, v)
    try:
        max_loss_pct = float(cfg.get("max_loss_pct") or 0)
    except (TypeError, ValueError):
        return None
    if max_loss_pct <= 0:
        return None
    cfg["max_loss_pct"] = max_loss_pct
    return cfg


def _check_max_loss_caps_for_open_positions(
    conn, settings: dict,
    *, client, dry_run: bool,
    bars_fetcher: Optional[Callable] = None,
    tracked_strategies: Optional[List[dict]] = None,
    asof: Optional[date] = None,
) -> List[dict]:
    """M10 (Sprint 3) — trend loser cap. A HARD per-position max-loss floor.

    For every open position whose strategy declares a max_loss_cap, if the
    latest bar's close is at or below entry_price * (1 - max_loss_pct/100),
    force-close it (broker sell + outcome close, exit_reason='max_loss_cap')
    even though the ATR trailing stop hasn't tripped. The trailing stop only
    ratchets DOWN from the running high, so a position that gaps/bleeds
    straight off entry never engages it and can blow out far past any sane
    single-name loss (ENPH −16%, AVGO −16% the week of 2026-06-03). This cap
    bounds that tail.

    Runs AFTER the trailing + time-stop passes so either of those wins when it
    trips first (a position they already closed has no open buy, so this pass
    skips it). Synthesizes a long_exit routed through _process_exit with
    exit_reason_override='max_loss_cap', so the full close path (sell, stop
    clear, MFE/MAE, outcome close) is reused — no parallel exit system.

    A winner or a small loser ABOVE the cap is left completely untouched.
    Requires a bars_fetcher (the live current price); without one this is a
    no-op (it never closes on the stale entry price, since that can't breach).
    Idempotent: once the SELL is submitted the position drops out.
    """
    if bars_fetcher is None:
        return []
    asof = asof or date.today()
    rows = conn.execute(
        "SELECT DISTINCT strategy_id, symbol FROM paper_trades "
        " WHERE side='buy' "
        "   AND status IN ('filled', 'partially_filled', 'accepted', 'new') "
    ).fetchall()
    actions: List[dict] = []
    for r in rows:
        sid, sym = r["strategy_id"], r["symbol"]
        if not sid or not sym:
            continue
        cfg = _resolve_max_loss_cap_config(sid, settings, tracked_strategies)
        if cfg is None:
            continue
        if _open_buy_for_pair(conn, sid, sym) is None:
            continue
        outcome = _open_outcome_for_pair(conn, sid, sym)
        if outcome is None:
            continue
        try:
            entry_price = float(outcome["entry_price"] or 0)
        except (TypeError, ValueError):
            entry_price = 0.0
        if entry_price <= 0:
            continue
        try:
            bars = bars_fetcher(sym)
        except Exception:
            continue
        if not bars:
            continue
        try:
            last_close = float(bars[-1].get("close") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if last_close <= 0:
            continue
        loss_pct = (last_close - entry_price) / entry_price * 100.0
        if loss_pct > -cfg["max_loss_pct"]:
            # Winner or a loser still inside the cap — leave it alone.
            continue
        synthetic_sig = {
            "id": None,
            "strategy_id": sid, "symbol": sym,
            "signal_type": "long_exit",
            "bar_ts": asof.isoformat(),
            "close": last_close,
        }
        action = _process_exit(
            conn, client, settings, synthetic_sig, dry_run,
            exit_reason_override="max_loss_cap", bars_fetcher=bars_fetcher,
        )
        action["synthetic_max_loss_cap"] = True
        action["loss_pct"] = round(loss_pct, 2)
        action["max_loss_pct"] = cfg["max_loss_pct"]
        actions.append(action)
    return actions


def _safe_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _maybe_record_sar_shadow(
    conn, *, strategy_id: str, symbol: str,
    current_price: float,
    bar_low: Optional[float],
    bar_high: Optional[float],
    real_trip: Optional[dict],
    tracked_strategies: Optional[List[dict]],
) -> Optional[int]:
    """6.4.2 — observational SAR shadow record.

    For strategies declaring ``sar_overlay: "shadow"`` (or live), check
    whether the current bar would have triggered a SAR flip and write a
    parallel ``paper_trades_sar_overlay`` row when it would. Never
    affects the live exit decision — the caller already settled that
    via ``_check_trailing_exit``.
    """
    from monitoring import sar_overlay as sar_mod
    decl = _resolve_strategy_declaration(strategy_id, tracked_strategies)
    if not sar_mod.strategy_has_sar_shadow(decl):
        return None
    sar_row = sar_mod.get_sar_state(
        conn, strategy_id=strategy_id, symbol=symbol,
    )
    if sar_row is None:
        return None
    lo = bar_low if bar_low is not None else current_price
    hi = bar_high if bar_high is not None else current_price
    if not sar_mod.is_sar_flip(
        sar=float(sar_row["sar"]),
        direction=sar_row["direction"],
        bar_low=lo, bar_high=hi,
    ):
        return None
    open_buy = _open_buy_for_pair(conn, strategy_id, symbol)
    entry_price = None
    qty = None
    entry_order_id = None
    if open_buy is not None:
        entry_order_id = open_buy["alpaca_order_id"]
        entry_price = (
            float(open_buy["fill_price"]) if open_buy["fill_price"] is not None
            else float(open_buy["limit_price"]) if open_buy["limit_price"]
            else None
        )
        qty = float(open_buy["qty"]) if open_buy["qty"] is not None else None
    # Real exit metadata: when the live trailing exit fired this bar,
    # capture its price + reason for the A/B delta. Otherwise the
    # position stays open and real_pnl is left None (the comparison
    # helper will exclude it from delta math until the live exit lands).
    real_exit_price = None
    real_exit_reason = None
    if real_trip is not None:
        real_exit_price = current_price
        real_exit_reason = real_trip.get("reason")
    return sar_mod.record_shadow_exit(
        conn,
        strategy_id=strategy_id, symbol=symbol,
        side=sar_row["direction"],
        entry_order_id=entry_order_id,
        entry_price=entry_price,
        qty=qty,
        shadow_exit_price=float(current_price),
        shadow_sar=float(sar_row["sar"]),
        shadow_reason="sar_flip",
        real_exit_price=real_exit_price,
        real_exit_reason=real_exit_reason,
    )


def _maybe_record_llm_filter_shadow(
    conn, *, sig, market_context: Dict[str, Any],
    asof: Optional[date],
    settings: Dict,
) -> Optional[Dict[str, Any]]:
    """7.1.1 — observational LLM-filter shadow record.

    For every fire auto_trader sees, ask the LLM filter for a verdict
    and persist it to ``paper_trades_llm_filter``. Returns the verdict
    dict so 7.1.3 callers can optionally consume it when
    ``auto_trade.llm_filter_live`` is true. The persisted shadow row
    is written regardless of whether the verdict is consumed.

    Always fail-open; helper itself swallows any unexpected error so a
    busted filter cannot break trading.
    """
    try:
        from monitoring import llm_filter as llmf
        sid = sig["strategy_id"]
        sym = sig["symbol"]
        signal_dict = {
            "strategy_id": sid,
            "symbol": sym,
            "bar_ts": sig["bar_ts"],
            "signal_type": sig["signal_type"],
            "side": "long",  # all current strategies are long-side
            "close": sig["close"],
        }
        llm_settings = settings.get("llm_filter") or {}
        model = llm_settings.get("model") or llmf.DEFAULT_MODEL
        daily_cap = int(llm_settings.get("daily_cap", llmf.DAILY_CALL_CAP))
        timeout = float(llm_settings.get(
            "timeout_sec", llmf.DEFAULT_TIMEOUT_SEC))
        recent_news = llmf.gather_recent_news(conn, sym)
        earnings = llmf.gather_earnings(conn, sym, asof=asof)
        prior = llmf.gather_prior_outcomes(conn, sid)
        verdict = llmf.assess_signal(
            signal_dict, conn,
            market_context=market_context,
            recent_news=recent_news,
            earnings=earnings,
            prior_outcomes=prior,
            model=model, daily_cap=daily_cap,
            timeout_sec=timeout,
        )
        return verdict
    except Exception as e:
        log(f"llm_filter shadow record failed (non-fatal): "
            f"{type(e).__name__}", "WARNING")
        return None


LLM_FILTER_LIVE_SKIP_GATE = "llm_filter_skip"
LLM_FILTER_DOWNSIZE_FACTOR = 0.5


def _llm_filter_live_action(
    *, settings: Dict, verdict: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """7.1.3 — derive the consume action from an LLM verdict.

    Returns a dict with keys:
      - action: "skip" | "downsize" | "pass"
      - qty_multiplier: float (only meaningful when action == "downsize")
      - reason: short string for skip/downsize logging

    When ``auto_trade.llm_filter_live`` is false (default), this always
    returns ``action='pass'`` regardless of verdict — the filter is
    observed but not consumed.
    """
    auto = (settings.get("auto_trade") or {})
    if not bool(auto.get("llm_filter_live", False)):
        return {"action": "pass", "qty_multiplier": 1.0, "reason": "filter_off"}
    if not isinstance(verdict, dict):
        return {"action": "pass", "qty_multiplier": 1.0, "reason": "no_verdict"}
    # Fail-open verdicts (failure_mode is non-None) always pass.
    if (verdict.get("rationale") or "").startswith("fail-open"):
        return {"action": "pass", "qty_multiplier": 1.0,
                "reason": "fail_open_passthrough"}
    v = (verdict.get("verdict") or "").lower()
    if v == "skip":
        return {
            "action": "skip", "qty_multiplier": 0.0,
            "reason": (verdict.get("rationale") or "llm verdict=skip")[:200],
        }
    if v == "downsize":
        return {
            "action": "downsize",
            "qty_multiplier": LLM_FILTER_DOWNSIZE_FACTOR,
            "reason": (verdict.get("rationale") or "llm verdict=downsize")[:200],
        }
    return {"action": "pass", "qty_multiplier": 1.0,
            "reason": (verdict.get("rationale") or "allow")[:200]}


def _maybe_record_intraday_confirm_shadow(
    conn, *, sig, decl: Optional[Dict[str, Any]], side: str = "long",
) -> Optional[int]:
    """7.5.4 — observational intraday-confirmation shadow record.

    When the strategy declaration opts in via ``intraday_confirm: "shadow"``,
    read the day's 1m bars after the signal's bar_ts and record whether
    a confirmation gate (close > trigger for long) would have fired.
    **Never affects the live entry path** — caller never reads the
    return value. Always wraps unexpected errors so a busted overlay
    cannot break trading.
    """
    try:
        from monitoring import intraday_confirm as ic
        if not ic.strategy_has_intraday_confirm_shadow(decl):
            return None
        sid = sig["strategy_id"]
        sym = sig["symbol"]
        bar_ts = sig["bar_ts"]
        trigger = sig["close"]
        if trigger is None:
            ic.record_intraday_confirm(
                conn,
                strategy_id=sid, symbol=sym,
                daily_signal_ts=bar_ts,
                trigger_price=None,
                signal_id=sig["id"] if "id" in sig.keys() else None,
                side=side,
                bars=None,
            )
            return None
        bars = ic.fetch_intraday_bars(
            conn, symbol=sym, after_ts_utc=str(bar_ts),
        )
        return ic.record_intraday_confirm(
            conn,
            strategy_id=sid, symbol=sym,
            daily_signal_ts=str(bar_ts),
            trigger_price=float(trigger),
            signal_id=sig["id"] if "id" in sig.keys() else None,
            side=side,
            bars=bars,
        )
    except Exception as e:
        log(f"intraday_confirm shadow record failed (non-fatal): "
            f"{type(e).__name__}", "WARNING")
        return None


DEFAULT_MAX_PCT_PER_SYMBOL = 0.30
DEFAULT_MAX_OPEN_PER_STRATEGY = 3
DEFAULT_MAX_NEW_ENTRIES_PER_DAY = 5  # 5.5.4.2


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


def _coerce_max_new_entries_per_day(raw) -> int:
    """Cap value for `auto_trade.max_new_entries_per_day` (5.5.4.2).

    Missing / non-numeric → DEFAULT_MAX_NEW_ENTRIES_PER_DAY (5).
    0 or negative → 0 (cap disabled). Positive integers pass through.
    """
    if raw is None:
        return DEFAULT_MAX_NEW_ENTRIES_PER_DAY
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_NEW_ENTRIES_PER_DAY
    if v < 0:
        return 0
    return v


def _reorder_signals_by_rank(
    sigs,
    *,
    regime: str,
    tracked_strategies,
    conn,
):
    """Sort `long_entry` signals by signal_ranker score DESC; keep
    `long_exit` and other signal types in their original relative order
    at the start (exits before entries so positions close first).

    `sigs` is an iterable of sqlite3.Row from the signals SELECT.
    Returns a list. Pure ordering — no DB writes.
    """
    sig_list = list(sigs)
    entries = [s for s in sig_list if s["signal_type"] == "long_entry"]
    others = [s for s in sig_list if s["signal_type"] != "long_entry"]
    if not entries:
        return sig_list

    from monitoring import signal_ranker as _sr
    fire_dicts = [
        {"strategy_id": s["strategy_id"], "symbol": s["symbol"], "_id": s["id"]}
        for s in entries
    ]
    sharpe = _sr.sharpe_lookup_from_db(
        {f["strategy_id"] for f in fire_dicts}, conn=conn,
    )
    dvol = _sr.dollar_volume_lookup_from_db(
        {f["symbol"] for f in fire_dicts}, conn=conn,
    )
    ranked = _sr.rank_signals(
        fire_dicts, regime=regime,
        strategy_decls=tracked_strategies,
        sharpe_by_strategy=sharpe,
        dollar_volume_by_symbol=dvol,
    )
    by_id = {s["id"]: s for s in entries}
    ranked_sigs = [by_id[r["_id"]] for r in ranked if r["_id"] in by_id]
    # Exits first, then ranked entries — exits closing positions before
    # new entries lets the capacity counter see the corrected position
    # count for any session-end exits.
    return others + ranked_sigs


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



def _recent_intraday_atr_pct(conn, symbol: str, price, *,
                             period: int = 14) -> Optional[float]:
    """Expected-move proxy for the M6 gate: ATR over the most recent intraday
    bars for `symbol`, as a percent of `price`. None when there aren't enough
    bars or price is invalid (caller then does NOT veto)."""
    try:
        rows = conn.execute(
            "SELECT high, low, close FROM intraday_bars "
            " WHERE symbol=? ORDER BY ts_utc DESC LIMIT ?",
            (symbol, period + 1),
        ).fetchall()
    except Exception:
        return None
    if not rows or len(rows) < period + 1:
        return None
    bars = [dict(r) for r in reversed(rows)]  # chronological
    from monitoring import stops as stops_mod
    from monitoring import intraday_edge_gate as eg_mod
    atr = stops_mod.compute_atr(bars, period=period)
    return eg_mod.expected_move_pct_from_atr(atr, price)


def _intraday_edge_veto(conn, sig, settings: dict) -> Optional[dict]:
    """M6: veto an intraday entry whose modeled expected move doesn't clear
    estimated friction. Returns the edge-gate descriptor (with veto=bool) or
    None when the gate is disabled. Enabled by default with conservative
    thresholds; set settings.intraday.edge_gate_enabled=false to disable."""
    intraday_cfg = settings.get("intraday")
    enabled = True
    if isinstance(intraday_cfg, dict) and "edge_gate_enabled" in intraday_cfg:
        enabled = bool(intraday_cfg.get("edge_gate_enabled"))
    elif "intraday_edge_gate_enabled" in settings:
        enabled = bool(settings.get("intraday_edge_gate_enabled"))
    if not enabled:
        return None
    from monitoring import intraday_edge_gate as eg_mod
    try:
        price = float(sig["close"] or 0)
    except (TypeError, ValueError, KeyError):
        price = 0.0
    expected_move = _recent_intraday_atr_pct(conn, sig["symbol"], price)
    return eg_mod.evaluate_edge_gate(
        expected_move_pct=expected_move, settings=settings,
    )


def _ma_cross_strength_veto(sig, settings: dict, bars_fetcher) -> Optional[dict]:
    """M7: confirm trend strength for a trend-ma-cross-20-50 entry. Returns the
    ma_cross_filter descriptor (with confirmed=bool) or None when bars can't be
    fetched (then NOT blocked — never veto on missing data)."""
    try:
        bars = bars_fetcher(sig["symbol"])
    except Exception:
        return None
    if not bars:
        return None
    from monitoring import ma_cross_filter as mac_mod
    return mac_mod.evaluate_ma_cross_strength(bars, settings=settings)


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
    strategy_class: Optional[str] = None,
    market_regime: Optional[str] = None,
    regime_confidence: Optional[float] = None,
    entry_filled: bool = False,
) -> Optional[dict]:
    """Compute + (if not dry-run) submit an initial stop. Routes through
    sizing.resolve_initial_stop so the same path serves trend (4.6),
    mean-reversion, and breakout (6.3) strategies, with per-strategy
    `stops.atr_multiplier` overrides and a fixed-percent fallback when
    ATR can't be computed (e.g. <14 bars of history).

    Returns a dict describing the stop, or None when stops are disabled
    /not actionable.

    The returned dict shape:
      {"requested_multiple": N,
       "atr": float | None,
       "stop_price": float | None,
       "stop_method": "atr_initial" | "fixed_percent" | None,
       "fallback_percent": float | None,
       "status": "disabled" | "no_bars" | "no_stop" | "submitted"
                  | "submit_failed" | "dry_run",
       "order_id": str | None,
       "stop_order_client_id": str | None,
       "error": str | None}
    """
    from monitoring import stops as stops_mod
    from monitoring import sizing as sizing_mod
    settings_stops = settings.get("stops") if isinstance(settings, dict) else None
    legacy_multiple = stops_mod._coerce_multiple(
        settings.get("stop_loss_atr_multiple"),
    )
    # Stops are disabled when neither legacy nor the new `stops` section
    # asks for them. legacy_multiple > 0 keeps Phase 4.6 behavior; an
    # `stops` block with a positive atr_multiplier enables 6.1.1 behavior.
    new_block_enabled = (
        isinstance(settings_stops, dict)
        and any(
            settings_stops.get(k) not in (None, 0, "0")
            for k in ("atr_multiplier", "per_strategy", "fixed_percent_fallback")
        )
    )
    if legacy_multiple <= 0 and not new_block_enabled:
        return None
    multiplier = sizing_mod.resolve_atr_multiplier(
        strategy_id=sig["strategy_id"],
        settings_stops=settings_stops,
        legacy_multiple=legacy_multiple if legacy_multiple > 0 else None,
        strategy_class=strategy_class,
    )
    # 6.1.3 — Regime-aware multiplier is opt-in via settings.stops.regime_aware.
    # Default off so existing 6.1.1/6.1.2 tests / behaviors stay stable; flip
    # to true in settings.json to enable.
    regime_aware = False
    if isinstance(settings_stops, dict):
        regime_aware = bool(settings_stops.get("regime_aware", False))
    effective_regime = market_regime if regime_aware else None
    info: dict = {
        "requested_multiple": multiplier,
        "atr": None,
        "stop_price": None,
        "stop_method": None,
        "fallback_percent": None,
        "base_multiplier": multiplier,
        "regime_multiplier": 1.0,
        "regime": market_regime,
        "regime_aware": regime_aware,
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
    # Period precedence: explicit `stops.atr_period` setting >
    # legacy 20-bar window (when only `stop_loss_atr_multiple` was set) >
    # the new 14-bar default. Preserving 20 for the legacy path keeps
    # Phase 4.6 trend strategies on the exact window they were tuned for.
    if isinstance(settings_stops, dict) and settings_stops.get("atr_period"):
        try:
            p = int(settings_stops["atr_period"])
            atr_period = p if p > 0 else sizing_mod.DEFAULT_ATR_INITIAL_PERIOD
        except (TypeError, ValueError):
            atr_period = sizing_mod.DEFAULT_ATR_INITIAL_PERIOD
    elif legacy_multiple > 0 and not new_block_enabled:
        atr_period = stops_mod.DEFAULT_ATR_PERIOD
    else:
        atr_period = sizing_mod.DEFAULT_ATR_INITIAL_PERIOD
    atr = stops_mod.compute_atr(bars, period=atr_period)
    info["atr"] = atr
    try:
        sig_type = str(sig["signal_type"] or "")
    except (KeyError, IndexError, TypeError):
        sig_type = ""
    side = "short" if sig_type.startswith("short") else "long"
    resolved = sizing_mod.resolve_initial_stop(
        entry_price=entry_fill, atr=atr,
        strategy_id=sig["strategy_id"],
        settings_stops=settings_stops,
        legacy_multiple=legacy_multiple if legacy_multiple > 0 else None,
        side=side,
        strategy_class=strategy_class,
        regime=effective_regime,
        regime_confidence=regime_confidence,
    )
    info["base_multiplier"] = resolved.get("base_multiplier", multiplier)
    info["regime_multiplier"] = resolved.get("regime_multiplier", 1.0)
    info["requested_multiple"] = resolved.get("multiplier", multiplier)
    info["stop_price"] = resolved["stop_price"]
    info["stop_method"] = resolved["method"]
    info["fallback_percent"] = resolved["fallback_percent"]
    if resolved["stop_price"] is None:
        info["status"] = "no_stop"
        return info
    # Quantize to a broker-valid tick (2dp for >= $1, finer below) so the
    # on-book stop, the recorded paper_trade, and the log note all agree and
    # Alpaca doesn't reject the order for a sub-penny increment.
    stop_price = stops_mod.quantize_stop_price(resolved["stop_price"])
    info["stop_price"] = stop_price
    stop_cid = (client_order_id + "-stop")[:MAX_CLIENT_ORDER_ID_LEN] \
        if client_order_id else None
    info["stop_order_client_id"] = stop_cid
    if dry_run:
        info["status"] = "dry_run"
        return info
    # M2 (Sprint 3) — owner authority on the stop side. A long-side protective
    # stop is a SELL against the ONE shared broker position; only the symbol's
    # owner may arm it. A non-owner arming a stop is what stacked competing
    # SELL-STOPs on a shared symbol (40310000 wash rejects, held_for_orders
    # blocking the real flatten). The entry path arms its stop right after the
    # owner's own buy, so this never blocks a legitimate first stop; it only
    # suppresses a non-owner's stop on a legacy shared symbol.
    if side != "short":
        try:
            from monitoring import position_manager as pm_owner
            if not pm_owner.owns_symbol(conn, sig["strategy_id"], sig["symbol"]):
                info["status"] = "skip_not_owner"
                info["owner"] = pm_owner.symbol_owner(conn, sig["symbol"])
                return info
        except Exception:
            pass
    # M3 (Sprint 3) — idempotent stop on the long side. Route through
    # position_manager.safe_submit_stop so re-arming a symbol that already has a
    # resting SELL (incl. a prior stop) CANCELS/REPLACES it rather than STACKING
    # a second SELL STOP (the 40310000 wash / double-held_for_orders source).
    # It also caps the qty to net-available (held_for_orders + run-ledger) so the
    # stop never reserves more than the position holds and never crosses zero
    # into a short. Short-cover stops keep the existing raw path. A 0-available
    # read falls back to the requested qty (freshly-filled entry not yet visible).
    if side != "short":
        from monitoring import position_manager as pm_stop

        def _stop_submit(c, *, symbol, qty, stop_price):
            return stops_mod.submit_atr_stop(
                c, symbol=symbol, qty=qty, stop_price=stop_price,
                client_order_id=stop_cid,
            )
        try:
            res = pm_stop.safe_submit_stop(
                client, symbol=sig["symbol"], requested_qty=qty,
                stop_price=stop_price, submit_fn=_stop_submit,
                entry_filled=entry_filled,
            )
        except Exception as e:
            log(f"stop submit failed for {sig['strategy_id']}/{sig['symbol']}: {e}",
                "ERROR")
            info["status"] = "submit_failed"
            info["error"] = str(e)[:200]
            return info
        if res is None or res.get("action") != "SUBMITTED":
            info["status"] = "no_stop"
            info["available"] = (res or {}).get("available")
            return info
        stop_order = res["order"]
        qty = res["qty"]
    else:
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
    method_label = resolved["method"] or "unknown"
    if resolved["method"] == sizing_mod.STOP_METHOD_ATR_INITIAL:
        note = (f"ATR({atr_period})={atr} × {multiplier} "
                f"= stop @ ${stop_price}; "
                f"linked to entry signal_id={sig['id']}")
    else:
        note = (f"fixed_percent fallback={resolved['fallback_percent']} "
                f"= stop @ ${stop_price}; "
                f"linked to entry signal_id={sig['id']}")
    db.record_paper_trade(conn, {
        "alpaca_order_id": info["order_id"],
        "signal_id": sig["id"],
        "strategy_id": sig["strategy_id"], "symbol": sig["symbol"],
        "side": "buy" if side == "short" else "sell",
        "qty": qty,
        "order_type": "stop",
        "stop_price": stop_price,
        "entry_stops": method_label,
        "submitted_at": str(getattr(stop_order, "submitted_at", _utc_now())),
        "status": str(getattr(stop_order, "status", "submitted")),
        "notes": note,
    })
    cover_side = "BUY" if side == "short" else "SELL"
    log(
        f"STOP {cover_side} {qty} {sig['symbol']} @ ${stop_price} "
        f"(method={method_label}; ATR={atr} × {multiplier})",
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
    bar_interval: str = "1d",
) -> dict:
    """Walk today's signals at `bar_interval`; submit Alpaca paper market orders
    per eligibility + dedupe.

    Defaults to bar_interval='1d' so existing EOD callers are unaffected.
    Intraday callers pass bar_interval='5m', '15m', '1h', etc.; the SELECT
    then matches signals.bar_ts that fall within the asof date AND have a
    matching bar_interval. The same eligibility, sizing, regime, pyramid,
    and risk-gate logic applies regardless of interval.

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
    # M1 (Sprint 3): start each pass from the broker's settled truth — clear the
    # in-run sell-reservation ledger so this pass's exits/flattens net against a
    # fresh slate (prior passes' sells are already reflected in broker qty).
    from monitoring import position_manager as _pm_reset
    _pm_reset.reset_run_reservations()
    built_own_client = client is None and not dry_run
    if built_own_client:
        client = client_factory()

    # Wire a default daily-bars fetcher for the live path when the caller
    # didn't supply one. ATR initial stops and the trailing-stop engine both
    # need bars; without this they no-op. Built only for real runs (dry-run
    # and tests pass their own fetcher or have no stop/trailing config, so a
    # lazily-built fetcher is never invoked there).
    if bars_fetcher is None and not dry_run:
        bars_fetcher = _build_default_bars_fetcher()

    # Backfill broker fills into paper_trades BEFORE evaluating signals, so
    # the open-position view (concentration caps, per-strategy caps, trailing
    # stops) sees this run's real fills instead of lagging until the nightly
    # reconcile. Best-effort: a broker hiccup must never block trading.
    # Gated on built_own_client so the live scheduled path (client=None ->
    # we build the real client) syncs, while callers that inject an explicit
    # client (tests) keep their controlled paper_trades state untouched.
    if built_own_client:
        # F5-LIVE: reconcile ATR stop-loss fills in the same in-loop pass as
        # order_sync. F5 taught reconcile_stop_fills to record MFE/MAE and the
        # correct exit_reason='stop_loss_atr', but nothing called it from the
        # live loop — stop fills were left unreconciled (orphan open outcomes,
        # NULL excursion). Pass the same default bars_fetcher used by the
        # ATR/trailing-stop engines so the excursion window is computed.
        #
        # ORDER MATTERS: reconcile runs BEFORE order_sync. reconcile_stop_fills
        # only considers stop rows in a non-terminal status; order_sync would
        # otherwise flip the stop paper_trade to 'filled' first, hiding it from
        # reconcile and orphaning the outcome. Running reconcile first lets it
        # close the outcome + mark the row filled; order_sync then sees it
        # terminal and skips it.
        # Best-effort: a failure here must never crash the trading loop.
        try:
            from monitoring import stops
            rec_res = stops.reconcile_stop_fills(
                conn, client, bars_fetcher=bars_fetcher)
            if rec_res.get("closed"):
                log(f"auto_trader: reconcile_stop_fills closed "
                    f"{rec_res['closed']} stop outcome(s) "
                    f"({rec_res['filled']} filled)", "INFO")
        except Exception as e:
            log(f"auto_trader: reconcile_stop_fills skipped "
                f"({type(e).__name__}: {e})", "WARNING")
        try:
            from monitoring import order_sync
            sync_res = order_sync.sync_order_fills(conn, client)
            if sync_res.get("updated"):
                log(f"auto_trader: order_sync backfilled {sync_res['updated']} "
                    f"row(s), {sync_res['filled']} newly filled", "INFO")
        except Exception as e:
            log(f"auto_trader: order_sync skipped "
                f"({type(e).__name__}: {e})", "WARNING")
        # A3 (audit 2026-06-03): close OPEN outcomes whose real broker
        # position is already gone (stop fill / manual close / missed
        # reconcile). Runs AFTER order_sync so a just-backfilled sell gives
        # the best last-known exit mark. Broker positions are the source of
        # truth — an outcome whose symbol is still held is left untouched.
        # Paper-mode gated upstream (BLOCKED_LIVE_MODE). Best-effort: a
        # broker hiccup must never crash the trading loop.
        try:
            from monitoring import reconcile_positions
            held = reconcile_positions.alpaca_open_positions(client)
            orph = reconcile_positions.sweep_orphan_outcomes(
                conn, set(held.keys()))
            if orph.get("swept"):
                log(f"auto_trader: orphan-outcome sweep closed "
                    f"{orph['swept']} outcome(s) with no broker position "
                    f"({orph['skipped']} skipped)", "INFO")
        except Exception as e:
            log(f"auto_trader: orphan-outcome sweep skipped "
                f"({type(e).__name__}: {e})", "WARNING")

    live_set = _live_strategies(settings)
    if live_client_factory is None:
        live_client_factory = lambda: get_alpaca_client(live=True)
    live_cache: Dict[str, object] = {}

    # M5 (Sprint 3) — paused-strategy position policy. Before evaluating any
    # signals, flatten every PAUSED strategy's still-owned holdings via the owner
    # authority. Pause = no new entries (entry gate) AND no silent holding (here).
    # Routed through the same paper/live client the strategy trades on. Runs only
    # on the EOD pass (bar_interval=='1d') so it executes once per day, not on
    # every intraday sub-pass. Best-effort: never blocks the trading loop.
    pause_flatten_actions: List[dict] = []
    if not dry_run and bar_interval == "1d":
        def _flatten_route(_sid):
            try:
                return _resolve_strategy_client(
                    _sid, live_set=live_set, paper_client=client,
                    live_client_factory=live_client_factory,
                    live_cache=live_cache)
            except ValueError:
                return client
        try:
            pause_flatten_actions = _flatten_paused_holdings(
                conn, client, dry_run=dry_run, resolve_client=_flatten_route)
        except Exception as e:
            log(f"process_signals: pause-flatten pass skipped "
                f"({type(e).__name__}: {e})", "WARNING")

    if bar_interval == "1d":
        sigs = conn.execute(
            "SELECT id, ts, bar_ts, bar_interval, strategy_id, symbol, signal_type, close "
            "  FROM signals "
            " WHERE bar_ts = ? AND bar_interval = ? "
            " ORDER BY id ASC",
            (asof.isoformat(), bar_interval),
        ).fetchall()
    else:
        # Intraday bar_ts is an ISO datetime ("YYYY-MM-DDTHH:MM:SS..."); match
        # any signal whose bar_ts starts with the asof date string.
        sigs = conn.execute(
            "SELECT id, ts, bar_ts, bar_interval, strategy_id, symbol, signal_type, close "
            "  FROM signals "
            " WHERE bar_ts LIKE ? AND bar_interval = ? "
            " ORDER BY id ASC",
            (f"{asof.isoformat()}%", bar_interval),
        ).fetchall()

    # portfolio_value is needed by Kelly sizing, by the per-symbol
    # concentration cap, AND by the daily drawdown circuit breaker. We
    # fetch the account summary once per pipeline invocation so all three
    # consumers see the same number.
    needs_kelly = str(settings.get("sizing_method") or "").lower() in (
        "kelly", "kelly_quarter")
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
                long_market_value=(account_summary.get("long_market_value")
                                   if account_summary else None),
                short_market_value=(account_summary.get("short_market_value")
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

    # Regime-aware strategy rotation (3.3.3). One lookup per pipeline run.
    from monitoring import regime_router as rr_mod
    from monitoring.config import TRACKED_STRATEGIES as _TRACKED
    current_regime = rr_mod.latest_regime(conn)
    regime_tracked = _TRACKED  # captured for tests via monkeypatch

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

    # 4.7.1 — Advance trailing stops for every open position BEFORE we
    # evaluate any signals this run. The ratchet is monotonic; advancing
    # here means same-bar exit signals see the freshest stop.
    trailing_updates = _update_trailing_stops_for_open_positions(
        conn, settings,
        bars_fetcher=bars_fetcher,
        tracked_strategies=regime_tracked,
    )

    # 5.5.4.2 — Wide-universe capacity cap. When the scanner fires 30+
    # signals on a trending day, we can only hold ~10 concurrent positions.
    # `max_new_entries_per_day` caps the count of NEW entries this run
    # submits; lower-ranked signals get logged as SKIP_CAPACITY. 0 disables.
    max_new_entries_per_day = _coerce_max_new_entries_per_day(
        settings.get("max_new_entries_per_day"))
    # When the cap is active, reorder long_entry signals by signal_ranker
    # score so the BEST ones are tried first. Non-entry signals (long_exit)
    # stay in their original position.
    if max_new_entries_per_day > 0:
        sigs = _reorder_signals_by_rank(
            sigs, regime=current_regime,
            tracked_strategies=regime_tracked,
            conn=conn,
        )
    entries_submitted_this_run = 0

    # Aggregate buying-power budget for this run. We WANT maximum capital
    # deployment (full margin line) to stress-test which strategies hold up
    # aggressively — but a rejected order yields no data, so we cap new
    # notional just under the broker's buying power (98% headroom for
    # slippage between the close-price estimate and the actual fill). That
    # keeps every order fundable/fillable rather than bounced. Falls back to
    # cash, then unbounded when no account summary is available.
    _bp_base = (account_summary.get("buying_power")
                or account_summary.get("cash")) if account_summary else None
    bp_ceiling = (float(_bp_base) * 0.98
                  if _bp_base not in (None, "") else None)
    bp_committed_this_run = 0.0

    # Stage 1.2 — portfolio heat budget for this run: cap total open dollar-risk
    # (Σ stop distance × size) at risk.max_portfolio_heat_pct of equity so a
    # correlated selloff can't stop out the whole book at once. Disabled (None)
    # when the cap is unset or equity is unknown.
    max_heat_pct = float((settings.get("risk") or {}).get(
        "max_portfolio_heat_pct", 0) or 0)
    heat_cap_usd = (portfolio_value * max_heat_pct
                    if (max_heat_pct > 0 and portfolio_value) else None)
    heat_used_usd = portfolio_heat_usd(conn) if heat_cap_usd is not None else 0.0
    heat_committed_this_run = 0.0

    # Seed with M5 pause-flatten actions so they surface in the run report.
    actions: List[dict] = list(pause_flatten_actions)
    cool_down_cache: Dict[str, Optional[dict]] = {}
    earnings_cache: Dict[str, Optional[dict]] = {}
    sentiment_cache: Dict[str, Optional[dict]] = {}
    # 7.1.1 — strict shadow LLM filter. For every fire auto_trader sees
    # (EOD + intraday), call the filter and record its verdict to
    # paper_trades_llm_filter. The verdict is NOT consumed in the live
    # decision path — this is a 30-day observability run before 7.1.3
    # graduation. Default OFF; flip on via settings.llm_filter.enabled.
    llm_filter_enabled = bool(
        ((settings.get("llm_filter") or {}).get("enabled", False))
    )
    llm_filter_market_context: Dict[str, Any] = {}
    llm_filter_market_context_loaded = False
    # 7.1.3 — when settings.auto_trade.llm_filter_live is True, the
    # verdict the filter returns is consumed in the live decision path.
    # Default is False; the filter remains strict-shadow.
    llm_filter_verdicts: Dict[int, Dict[str, Any]] = {}
    for sig in sigs:
        sig_src = _skip_source_for_bar_interval(
            sig["bar_interval"] if "bar_interval" in sig.keys() else "1d"
        )
        if llm_filter_enabled:
            if not llm_filter_market_context_loaded:
                from monitoring import llm_filter as _llmf
                try:
                    llm_filter_market_context = _llmf.gather_market_context(
                        conn, asof=asof,
                    )
                except Exception:
                    llm_filter_market_context = {}
                llm_filter_market_context_loaded = True
            verdict = _maybe_record_llm_filter_shadow(
                conn, sig=sig,
                market_context=llm_filter_market_context,
                asof=asof,
                settings=settings,
            )
            if verdict and "id" in sig.keys():
                llm_filter_verdicts[sig["id"]] = verdict
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
                _record_skip(
                    conn, sig=sig, gate="kill_switch",
                    reason_detail=(
                        f"reason={kill_switch_state.get('reason') or '(none)'}; "
                        f"set_at={kill_switch_state.get('set_at') or '?'}"
                    ),
                    source=sig_src,
                )
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
                _record_skip(
                    conn, sig=sig, gate="drawdown_breaker",
                    reason_detail=drawdown_block.get("reason"),
                    source=sig_src,
                )
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
                _record_skip(
                    conn, sig=sig, gate="earnings_veto",
                    reason_detail=ev.get("reason") or (
                        f"{sym} within earnings window"
                    ),
                    source=sig_src,
                )
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
                _record_skip(
                    conn, sig=sig, gate="negative_sentiment_veto",
                    reason_detail=ns.get("reason") or (
                        f"{sym} negative sentiment"
                    ),
                    source=sig_src,
                )
                actions.append({
                    "action": "SKIP_NEGATIVE_SENTIMENT",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sym,
                    "signal_id": sig["id"],
                    **ns,
                })
                continue
            # M6 (Sprint 2): intraday cost/slippage edge gate. Veto an intraday
            # entry whose modeled expected move (ATR over recent intraday bars,
            # as % of price) doesn't clear estimated round-trip friction
            # (spread + slippage) plus a buffer. Addresses the negative-avg-
            # return-despite-decent-win-rate strategies whose edge is eaten by
            # cost. EOD (1d) entries are out of scope. A missing estimate never
            # vetoes.
            _eg_bar_interval = (sig["bar_interval"]
                                if "bar_interval" in sig.keys() else "1d")
            if (_eg_bar_interval or "1d") != "1d":
                eg = _intraday_edge_veto(conn, sig, settings)
                if eg is not None and eg.get("veto"):
                    _record_skip(
                        conn, sig=sig, gate="intraday_edge_gate",
                        reason_detail=eg.get("reason"),
                        source=sig_src,
                    )
                    actions.append({
                        "action": "SKIP_INTRADAY_EDGE_GATE",
                        "strategy_id": sig["strategy_id"],
                        "symbol": sym,
                        "signal_id": sig["id"],
                        **eg,
                    })
                    continue
            sid = sig["strategy_id"]
            if sid not in cool_down_cache:
                cool_down_cache[sid] = _cool_down_state(
                    conn, sid, settings, asof=asof,
                )
            cd = cool_down_cache[sid]
            if cd is not None:
                _record_skip(
                    conn, sig=sig, gate="cool_down",
                    reason_detail=cd.get("reason"),
                    source=sig_src,
                )
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
                _record_skip(
                    conn, sig=sig, gate="concentration_cap",
                    reason_detail=(
                        f"cap=${block.get('cap_usd')}, used=${block.get('used_usd')}, "
                        f"next=${block.get('next_position_usd')}"
                    ),
                    source=sig_src,
                )
                actions.append(block)
                continue
            regime_skip_info = rr_mod.regime_skip(
                sig["strategy_id"],
                regime=current_regime,
                tracked_strategies=regime_tracked,
            )
            if regime_skip_info is not None:
                _record_skip(
                    conn, sig=sig, gate="regime_mismatch",
                    reason_detail=(
                        regime_skip_info.get("reason")
                        or f"current_regime={current_regime}"
                    ),
                    source=sig_src,
                )
                actions.append({
                    "action": "SKIP_REGIME_MISMATCH",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    **regime_skip_info,
                })
                continue
            # Auto-pause from 3.3.4 — refuse entries on strategies the
            # divergence checker has flagged. Exits remain unaffected so
            # currently-open positions still close cleanly.
            from monitoring import strategy_health as sh_mod
            if sh_mod.is_paused(conn, sig["strategy_id"]):
                row = conn.execute(
                    "SELECT reason, paused_at, expires_at, source "
                    "  FROM paused_strategies WHERE strategy_id=?",
                    (sig["strategy_id"],),
                ).fetchone()
                _paused_reason = (row["reason"] if row else "") or ""
                _record_skip(
                    conn, sig=sig, gate="paused_strategy",
                    reason_detail=(
                        f"paused: {_paused_reason}; "
                        f"expires_at={(row['expires_at'] if row else '') or 'n/a'}"
                    ),
                    source=sig_src,
                )
                actions.append({
                    "action": "SKIP_PAUSED_STRATEGY",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    "reason": _paused_reason,
                    "paused_at": (row["paused_at"] if row else "") or "",
                    "expires_at": (row["expires_at"] if row else "") or "",
                    "source": (row["source"] if row else "") or "",
                })
                continue
            # M7 (Sprint 2): regime/trend-strength confirmation for
            # trend-ma-cross-20-50. It catches weak continuations / large
            # drawdowns; rather than pause it, gate its entries — a cross with
            # a thin EMA spread, a flat/falling slow EMA, or price below the
            # EMAs is a weak continuation and is vetoed. Genuine strong-trend
            # crosses pass. Other strategies are unaffected.
            if (sig["strategy_id"] == "trend-ma-cross-20-50"
                    and bars_fetcher is not None):
                mac = _ma_cross_strength_veto(sig, settings, bars_fetcher)
                if mac is not None and not mac.get("confirmed", True):
                    _record_skip(
                        conn, sig=sig, gate="ma_cross_weak_continuation",
                        reason_detail=mac.get("reason"), source=sig_src,
                    )
                    actions.append({
                        "action": "SKIP_MA_CROSS_WEAK_CONTINUATION",
                        "strategy_id": sig["strategy_id"],
                        "symbol": sig["symbol"],
                        "signal_id": sig["id"],
                        **mac,
                    })
                    continue
            if max_open_per_strategy > 0:
                cur_open = open_per_strategy.get(sig["strategy_id"], 0)
                if cur_open >= max_open_per_strategy:
                    _reason_mop = (
                        f"strategy already has {cur_open} open "
                        f"position(s) (cap={max_open_per_strategy})"
                    )
                    _record_skip(
                        conn, sig=sig, gate="max_open_per_strategy",
                        reason_detail=_reason_mop, source=sig_src,
                    )
                    actions.append({
                        "action": "SKIP_MAX_OPEN_PER_STRATEGY",
                        "strategy_id": sig["strategy_id"],
                        "symbol": sig["symbol"],
                        "signal_id": sig["id"],
                        "open_count": cur_open,
                        "cap": max_open_per_strategy,
                        "reason": _reason_mop,
                    })
                    continue
            # 5.4.1 — Pattern Day Trader guard. Only applies to intraday
            # entries (EOD signals are by definition not day trades). When
            # account_value < $25k AND >= 3 round trips in the last 5 days,
            # refuse the entry. Paper accounts effectively never trip the
            # guard (100k seed), but the guard is always computed so that
            # any strategy promoted to live via auto_trade.live_strategies
            # automatically picks up the same enforcement.
            sig_bar_interval = sig["bar_interval"] if "bar_interval" in sig.keys() else "1d"
            if (sig_bar_interval or "1d") != "1d":
                from monitoring import pdt_guard as pdt_mod
                pdt_block = pdt_mod.check_pdt_guard(
                    conn,
                    account_value=portfolio_value,
                    asof=asof,
                )
                if pdt_block is not None:
                    _record_skip(
                        conn, sig=sig, gate="pdt_guard",
                        reason_detail=pdt_block.get("reason") or "PDT guard tripped",
                        source=sig_src,
                    )
                    actions.append({
                        "action": "SKIP_PDT_GUARD",
                        "strategy_id": sig["strategy_id"],
                        "symbol": sig["symbol"],
                        "signal_id": sig["id"],
                        **pdt_block,
                    })
                    continue
                # 5.5.2 — Per-symbol same-day round-trip cap. Default 2;
                # configurable via auto_trade.max_intraday_round_trips_per_symbol.
                # Only intraday entries can trip this — EOD entries
                # don't accumulate same-day round trips by definition.
                from monitoring import intraday_symbol_cap as isc_mod
                sym_cap = int(settings.get(
                    "max_intraday_round_trips_per_symbol",
                    isc_mod.DEFAULT_MAX_INTRADAY_ROUND_TRIPS_PER_SYMBOL,
                ))
                sym_block = isc_mod.check_intraday_symbol_cap(
                    conn, symbol=sig["symbol"], asof=asof, cap=sym_cap,
                )
                if sym_block is not None:
                    _record_skip(
                        conn, sig=sig, gate="intraday_symbol_cap",
                        reason_detail=(
                            sym_block.get("reason")
                            or f"intraday symbol cap reached for {sig['symbol']}"
                        ),
                        source=sig_src,
                    )
                    actions.append({
                        "action": "SKIP_INTRADAY_SYMBOL_CAP",
                        "strategy_id": sig["strategy_id"],
                        "symbol": sig["symbol"],
                        "signal_id": sig["id"],
                        **sym_block,
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
                _record_skip(
                    conn, sig=sig, gate="live_creds_missing",
                    reason_detail=str(e), source=sig_src,
                )
                actions.append({
                    "action": "SKIP_LIVE_CREDS_MISSING",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    "reason": str(e),
                })
                continue
            # 4.7.2 — When there's an open position from this strategy/symbol
            # already, route to the pyramiding decision branch instead of
            # treating this as a fresh entry. The decision branch handles
            # pyramidable opt-in, regime alignment, max tiers, and
            # aggregate-cap enforcement.
            if _open_buy_for_pair(conn, sig["strategy_id"], sig["symbol"]) is not None:
                addon_action = _process_pyramid_addon(
                    conn, strategy_client, settings, sig, dry_run,
                    tracked_strategies=regime_tracked,
                    market_regime=current_regime,
                    asof=asof,
                )
                actions.append(addon_action)
                continue
            # 5.5.4.2 — Capacity cap. Applied AFTER all skip gates but
            # BEFORE _process_entry so we don't waste an Alpaca call on a
            # signal we'd just skip. Pyramid add-ons aren't gated here
            # (they're handled above and don't consume new-entry budget).
            if (max_new_entries_per_day > 0
                    and entries_submitted_this_run >= max_new_entries_per_day):
                _reason_cap = (
                    f"capacity reached: {entries_submitted_this_run} "
                    f"entries already submitted this run "
                    f"(cap={max_new_entries_per_day})"
                )
                _record_skip(
                    conn, sig=sig, gate="max_orders_per_day",
                    reason_detail=_reason_cap, source=sig_src,
                )
                actions.append({
                    "action": "SKIP_CAPACITY",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    "max_new_entries_per_day": max_new_entries_per_day,
                    "entries_submitted_this_run": entries_submitted_this_run,
                    "reason": _reason_cap,
                })
                continue
            # 7.1.3 — consume the LLM filter verdict iff llm_filter_live=true.
            # Skip blocks the entry entirely (with intraday_skips row);
            # downsize halves qty via throttle_multiplier; allow / fail-open
            # pass through unchanged.
            llm_action = _llm_filter_live_action(
                settings=settings,
                verdict=llm_filter_verdicts.get(sig["id"]),
            )
            if llm_action["action"] == "skip":
                _record_skip(
                    conn, sig=sig, gate=LLM_FILTER_LIVE_SKIP_GATE,
                    reason_detail=llm_action["reason"],
                    source=sig_src,
                )
                actions.append({
                    "action": "SKIP_LLM_FILTER",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    "reason": llm_action["reason"],
                })
                continue
            local_throttle = throttle_multiplier * float(
                llm_action["qty_multiplier"]
            )
            remaining_bp = (bp_ceiling - bp_committed_this_run
                            if bp_ceiling is not None else None)
            remaining_heat = (
                heat_cap_usd - heat_used_usd - heat_committed_this_run
                if heat_cap_usd is not None else None)
            entry_action = _process_entry(
                conn, strategy_client, settings, sig, dry_run,
                asof=asof, sleep_fn=sleep_fn, now_fn=now_fn,
                data_client=data_client,
                portfolio_value=portfolio_value,
                bars_fetcher=bars_fetcher,
                throttle_multiplier=local_throttle,
                market_regime=current_regime,
                tracked_strategies=regime_tracked,
                remaining_bp_budget=remaining_bp,
                remaining_heat_usd=remaining_heat,
            )
            if llm_action["action"] == "downsize":
                entry_action["llm_filter_downsize"] = True
                entry_action["llm_filter_reason"] = llm_action["reason"]
            actions.append(entry_action)
            if entry_action.get("action") in ("BUY", "DRY_BUY"):
                entries_submitted_this_run += 1
                bp_committed_this_run += float(
                    entry_action.get("notional") or 0)
                heat_committed_this_run += float(
                    entry_action.get("entry_risk_usd") or 0)
                if max_open_per_strategy > 0:
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
                _record_skip(
                    conn, sig=sig, gate="live_creds_missing",
                    reason_detail=str(e), source=sig_src,
                )
                actions.append({
                    "action": "SKIP_LIVE_CREDS_MISSING",
                    "strategy_id": sig["strategy_id"],
                    "symbol": sig["symbol"],
                    "signal_id": sig["id"],
                    "reason": str(e),
                })
                continue
            actions.append(_process_exit(
                conn, strategy_client, settings, sig, dry_run,
                bars_fetcher=bars_fetcher))

    # 4.7.1 — After the explicit signal pass, check whether the latest bar
    # for any open position crossed its trailing stop. The default Alpaca
    # paper client handles both paper and live for the trailing exits
    # (live strategies are not currently expected to opt into trailing).
    trailing_actions = _check_trailing_exits_for_open_positions(
        conn, settings,
        client=client, dry_run=dry_run,
        bars_fetcher=bars_fetcher,
        tracked_strategies=regime_tracked,
        asof=asof,
    )
    actions.extend(trailing_actions)

    # A5 — after trailing (which wins on a price trip), close any open
    # position held past its strategy's time_stop with exit_reason='time_stop'
    # so a 1d trend outcome can't sit OPEN indefinitely waiting on a rare
    # channel/MA breakdown.
    time_stop_actions = _check_time_stops_for_open_positions(
        conn, settings,
        client=client, dry_run=dry_run,
        bars_fetcher=bars_fetcher,
        tracked_strategies=regime_tracked,
        asof=asof,
    )
    actions.extend(time_stop_actions)

    # M10 (Sprint 3) — trend loser cap. After trailing + time-stop (each of
    # which wins when it trips first), force-close any open position whose
    # unrealised loss from ENTRY has breached its strategy's hard max_loss_pct.
    # The ATR trail only ratchets down from the high, so a name that gaps/bleeds
    # straight off entry (ENPH −16%, AVGO −16%) never engages it; this bounds
    # that tail without touching entry/exit logic or any risk.* limit.
    max_loss_cap_actions = _check_max_loss_caps_for_open_positions(
        conn, settings,
        client=client, dry_run=dry_run,
        bars_fetcher=bars_fetcher,
        tracked_strategies=regime_tracked,
        asof=asof,
    )
    actions.extend(max_loss_cap_actions)

    # Stage 0.4 — re-sync THIS pass's own orders at the END. The top-of-pass
    # order_sync only sees the PREVIOUS run's orders; the sells/stops this pass
    # just submitted are never re-queried, so on the EOD final run (no later
    # pass) they strand at status='accepted'/NULL fill forever — the documented
    # precursor to orphan reconciled_no_position outcomes (4 such sells were
    # stuck at authoring). Re-running here backfills them. Same built_own_client
    # guard as the top-of-pass sync. Best-effort: a broker hiccup never
    # poisons the returned result.
    if built_own_client:
        try:
            from monitoring import order_sync
            end_sync = order_sync.sync_order_fills(conn, client)
            if end_sync.get("updated"):
                log(f"auto_trader: end-of-pass order_sync backfilled "
                    f"{end_sync['updated']} row(s), {end_sync['filled']} newly "
                    f"filled", "INFO")
        except Exception as e:
            log(f"auto_trader: end-of-pass order_sync skipped "
                f"({type(e).__name__}: {e})", "WARNING")

    out = {"status": "OK", "dry_run": dry_run, "asof": asof.isoformat(),
           "actions": actions, "market_regime": current_regime}
    if throttle_info is not None:
        out["throttle"] = throttle_info
    if trailing_updates:
        out["trailing_updates"] = trailing_updates
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
