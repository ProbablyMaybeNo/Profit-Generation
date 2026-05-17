"""
public_api.py — Read-only public endpoints for the trading system
(milestone 4.4.1).

Three endpoints, NO auth, mounted under `/api/public/`:

  GET /api/public/equity_curve     — system-wide equity curve, % returns only
  GET /api/public/strategies        — per-strategy Sharpe + win-rate
  GET /api/public/last_30d_pnl      — trailing 30-day P&L %

Everything is sanitized — sensitive fields are stripped before
serialisation. The sanitizer is the security boundary; tests verify
no $ amounts, no position sizes, no Alpaca account IDs, no raw fill
data, and no credentials leak.

Rate-limited per IP (60 req/min default), keyed by `request.remote_addr`
with X-Forwarded-For honored for Cloudflare-tunnel deployments.

Stateless: rate limiter is an in-process deque-per-IP. Restarting the
dashboard resets every client to zero requests — acceptable trade-off
for a self-hosted service.
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

DEFAULT_RATE_LIMIT_PER_MIN = 60
RATE_WINDOW_SEC = 60.0

# Fields that MUST be stripped before any public-facing serialisation.
# Belt-and-braces: the public functions never read these in the first
# place, but if someone accidentally adds them later, sanitize_dict()
# will catch them at the boundary.
SENSITIVE_KEYS = frozenset({
    # Dollar amounts
    "portfolio_value", "cash", "buying_power", "equity",
    "max_position_usd", "usd", "balance", "notional",
    # Position / order details
    "position_size", "qty", "quantity", "shares",
    "fill_price", "entry_price", "exit_price", "limit_price",
    "stop_price", "submitted_at", "filled_at", "alpaca_order_id",
    # Identifiers
    "account_number", "account_id", "alpaca_account_id",
    "api_key", "secret_key", "secret", "token",
    "integration_token", "webhook_secret",
})


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------

def sanitize_dict(value: Any) -> Any:
    """Recursive: drops any key in SENSITIVE_KEYS at every nesting depth."""
    if isinstance(value, dict):
        return {
            k: sanitize_dict(v) for k, v in value.items()
            if k not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [sanitize_dict(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Rate limiter — per-IP sliding window
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window per-IP limiter. Thread-unsafe by design — Flask's
    default dev server is single-threaded, and gunicorn would route
    each IP to a deterministic worker. Don't deploy this behind a
    multi-worker async server without an external limiter."""

    def __init__(self, *, per_minute: int = DEFAULT_RATE_LIMIT_PER_MIN,
                 now_fn: Optional[Callable[[], float]] = None):
        self.per_minute = int(per_minute)
        self._now = now_fn or time.monotonic
        self._timestamps: Dict[str, Deque[float]] = {}

    def allow(self, ip: str) -> bool:
        now = self._now()
        cutoff = now - RATE_WINDOW_SEC
        bucket = self._timestamps.setdefault(ip, deque())
        # Evict everything older than the window in one pass.
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.per_minute:
            return False
        bucket.append(now)
        return True

    def reset(self, ip: Optional[str] = None) -> None:
        if ip is None:
            self._timestamps.clear()
        else:
            self._timestamps.pop(ip, None)


# ---------------------------------------------------------------------------
# Data shapes — pure functions over the DB
# ---------------------------------------------------------------------------

def system_equity_curve(
    conn: sqlite3.Connection,
    *,
    days: int = 365,
    now_fn: Optional[Callable] = None,
) -> Dict:
    """System-wide equity curve in PERCENT only. Never dollars.

    The dollar series in `equity_snapshots` is normalized into a %
    return relative to the FIRST observation in the window. Caller
    plots this — no absolute capital ever leaves the wire.
    """
    now = (now_fn() if now_fn else datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT recorded_at, portfolio_value FROM equity_snapshots "
        " WHERE recorded_at >= ? "
        " ORDER BY recorded_at ASC",
        (since,),
    ).fetchall()
    if not rows:
        return {"days": days, "points": [], "final_pct": 0.0,
                "max_drawdown_pct": 0.0}

    baseline = float(rows[0]["portfolio_value"])
    if baseline <= 0:
        baseline = 1.0  # avoid div by zero; series will still be flat

    points: List[Dict] = []
    running_max = 0.0
    max_dd = 0.0
    for r in rows:
        pct = ((float(r["portfolio_value"]) / baseline) - 1.0) * 100.0
        if pct > running_max:
            running_max = pct
        dd = pct - running_max
        if dd < max_dd:
            max_dd = dd
        # Date-only string — no second-level timestamps to fingerprint.
        ts = str(r["recorded_at"] or "")[:10]
        points.append({"date": ts, "pct": round(pct, 4),
                        "drawdown_pct": round(dd, 4)})
    final_pct = points[-1]["pct"] if points else 0.0
    return {
        "days": days,
        "points": points,
        "final_pct": round(final_pct, 4),
        "max_drawdown_pct": round(max_dd, 4),
    }


def per_strategy_stats(conn: sqlite3.Connection) -> List[Dict]:
    """Per-strategy Sharpe + win-rate only. No position sizes, no $ amounts.

    Source: closed 1d outcomes. Same shape as the equity-edge card on
    the private dashboard, minus the sensitive bits.
    """
    rows = conn.execute(
        "SELECT s.strategy_id, o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval = '1d'"
    ).fetchall()
    by_strat: Dict[str, List[float]] = {}
    for r in rows:
        sid = r["strategy_id"] or ""
        if not sid:
            continue
        by_strat.setdefault(sid, []).append(float(r["return_pct"]))

    out: List[Dict] = []
    for sid, rets in by_strat.items():
        n = len(rets)
        if n < 2:
            continue
        mean = sum(rets) / n
        sd = statistics.stdev(rets)
        sharpe = (mean / sd) if sd > 0 else 0.0
        wins = sum(1 for r in rets if r > 0)
        out.append({
            "strategy_id": sid,
            "n_trades": n,
            "win_rate": round(wins / n, 4),
            "sharpe": round(sharpe, 4),
            "mean_pct": round(mean, 4),
        })
    out.sort(key=lambda r: (-r["sharpe"], r["strategy_id"]))
    return out


def last_n_days_pnl_pct(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    now_fn: Optional[Callable] = None,
) -> Dict:
    """Trailing-window P&L percentage. Pure % — never the dollar value."""
    now = (now_fn() if now_fn else datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT recorded_at, portfolio_value FROM equity_snapshots "
        " WHERE recorded_at >= ? "
        " ORDER BY recorded_at ASC",
        (cutoff,),
    ).fetchall()
    if len(rows) < 2:
        return {"days": days, "pnl_pct": 0.0, "n_snapshots": len(rows)}
    start_v = float(rows[0]["portfolio_value"])
    end_v = float(rows[-1]["portfolio_value"])
    if start_v <= 0:
        return {"days": days, "pnl_pct": 0.0, "n_snapshots": len(rows)}
    pnl_pct = ((end_v / start_v) - 1.0) * 100.0
    return {
        "days": days,
        "pnl_pct": round(pnl_pct, 4),
        "n_snapshots": len(rows),
    }


# ---------------------------------------------------------------------------
# Flask registration
# ---------------------------------------------------------------------------

def get_client_ip(request) -> str:
    """Honour X-Forwarded-For for Cloudflare-tunnel deployments. First
    address in the chain is the original client."""
    fwd = (request.headers.get("X-Forwarded-For") or "").strip()
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def register(
    app,
    *,
    db_module,
    rate_limiter: Optional[RateLimiter] = None,
    last_updated_fn: Optional[Callable[[], str]] = None,
) -> None:
    """Register the /api/public/* routes on the given Flask app.

    `db_module`: caller passes `data.db` (kept as a param so tests can
    swap in an isolated DB path).
    `rate_limiter`: defaults to 60 req/min if not supplied.
    `last_updated_fn`: returns the ISO timestamp shown on the static
    page footer. Default: now() in UTC.
    """
    from flask import jsonify, request
    limiter = rate_limiter or RateLimiter()
    last_updated = last_updated_fn or (
        lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def _rate_check():
        ip = get_client_ip(request)
        if not limiter.allow(ip):
            return jsonify({
                "error": "rate_limited",
                "limit_per_min": limiter.per_minute,
            }), 429
        return None

    @app.route("/api/public/equity_curve", methods=["GET"])
    def public_equity_curve():
        gate = _rate_check()
        if gate is not None:
            return gate
        days_arg = request.args.get("days", default="365")
        try:
            days = max(1, min(365 * 5, int(days_arg)))
        except ValueError:
            days = 365
        conn = db_module.init_db()
        try:
            payload = system_equity_curve(conn, days=days)
        finally:
            conn.close()
        payload = sanitize_dict(payload)
        payload["last_updated"] = last_updated()
        return jsonify(payload)

    @app.route("/api/public/strategies", methods=["GET"])
    def public_strategies():
        gate = _rate_check()
        if gate is not None:
            return gate
        conn = db_module.init_db()
        try:
            rows = per_strategy_stats(conn)
        finally:
            conn.close()
        payload = {
            "strategies": sanitize_dict(rows),
            "last_updated": last_updated(),
        }
        return jsonify(payload)

    @app.route("/api/public/last_30d_pnl", methods=["GET"])
    def public_last_30d_pnl():
        gate = _rate_check()
        if gate is not None:
            return gate
        conn = db_module.init_db()
        try:
            payload = last_n_days_pnl_pct(conn, days=30)
        finally:
            conn.close()
        payload = sanitize_dict(payload)
        payload["last_updated"] = last_updated()
        return jsonify(payload)
