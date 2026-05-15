"""
tv_webhook.py — Tiny Flask receiver for TradingView Pine Script alerts.

POST /webhook
    Accepts JSON like:
        {"secret": "...", "ticker": "AMEX:GDX", "action": "buy",
         "price": 93.95, "strategy": "botnet101-buy-5day-low",
         "time": "2026-05-14T19:30:00Z"}
    Verifies the shared secret, normalizes the ticker (strips exchange
    prefix), maps action → signal_type, auto-creates a stub strategy row
    if unknown, and persists at bar_interval='tv-webhook'.

GET  /health  → liveness probe
GET  /recent  → last 20 webhook signals as JSON

The shared secret comes from credentials.json:
    {"tradingview": {"webhook_secret": "..."}}
or environment variable TV_WEBHOOK_SECRET (env wins).

Configure TradingView's alert webhook URL to point at this server (use
Cloudflare Tunnel / ngrok / etc. — see schedulers/README.md).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import load_credentials, log  # noqa: E402
from data import db  # noqa: E402

ACTION_MAP = {
    "buy":         "long_entry",
    "long":        "long_entry",
    "long_entry":  "long_entry",
    "entry":       "long_entry",
    "sell":        "long_exit",
    "exit":        "long_exit",
    "long_exit":   "long_exit",
    "close":       "long_exit",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_secret() -> Optional[str]:
    env_secret = os.environ.get("TV_WEBHOOK_SECRET")
    if env_secret:
        return env_secret.strip() or None
    try:
        creds = load_credentials("tradingview")
    except Exception:
        return None
    secret = (creds or {}).get("webhook_secret")
    if secret and "PASTE_YOUR" not in str(secret):
        return str(secret).strip()
    return None


def _normalize_ticker(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip().upper()
    # TV tickers often arrive as "AMEX:GDX" or "BINANCE:BTCUSDT"
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    return raw


def _normalize_action(raw: str) -> Optional[str]:
    if not raw:
        return None
    return ACTION_MAP.get(raw.strip().lower())


def _ensure_strategy(conn, strategy_id: str) -> None:
    row = conn.execute("SELECT 1 FROM strategies WHERE strategy_id = ?", (strategy_id,)).fetchone()
    if row:
        return
    db.upsert_strategy(conn, {
        "title": f"TV alert: {strategy_id}",
        "extra": {
            "strategy_id": strategy_id,
            "methodology_family": "tradingview-alert",
            "current_verdict": "UNTESTED",
            "first_logged_iso": _utc_now_iso(),
        },
    })


def _process_payload(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Return (http_status, response_body)."""
    ticker = _normalize_ticker(str(payload.get("ticker") or payload.get("symbol") or ""))
    action = _normalize_action(str(payload.get("action") or payload.get("side") or ""))
    strategy_id = str(payload.get("strategy") or payload.get("strategy_id") or "tv-untagged").strip()
    price_raw = payload.get("price") or payload.get("close")
    bar_ts = (str(payload.get("time") or payload.get("bar_ts") or _utc_now_iso())).strip()

    if not ticker:
        return 400, {"error": "missing 'ticker'"}
    if not action:
        return 400, {"error": f"unrecognized 'action' (got {payload.get('action')!r})"}
    try:
        price = float(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        return 400, {"error": f"non-numeric 'price' (got {price_raw!r})"}

    conn = db.init_db()
    try:
        _ensure_strategy(conn, strategy_id)
        sig_id = db.record_signal(
            conn,
            strategy_id=strategy_id,
            symbol=ticker,
            bar_ts=bar_ts,
            signal_type=action,
            close=price,
            bar_interval="tv-webhook",
            extra={"raw": payload},
        )
    finally:
        conn.close()

    if sig_id is None:
        return 200, {"status": "duplicate", "strategy_id": strategy_id, "symbol": ticker,
                     "signal_type": action, "bar_ts": bar_ts}
    return 200, {"status": "recorded", "signal_id": sig_id, "strategy_id": strategy_id,
                 "symbol": ticker, "signal_type": action, "bar_ts": bar_ts, "price": price}


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "ts": _utc_now_iso()})

    @app.post("/webhook")
    def webhook():
        secret = _resolve_secret()
        try:
            payload = request.get_json(force=True, silent=False)
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return jsonify({"error": "expected JSON object"}), 400
        if secret is not None:
            given = (
                request.headers.get("X-Webhook-Secret")
                or payload.get("secret")
                or payload.get("token")
            )
            if given != secret:
                log("tv_webhook: secret mismatch — rejecting", "WARNING")
                return jsonify({"error": "unauthorized"}), 401
        status, body = _process_payload(payload)
        return jsonify(body), status

    @app.get("/recent")
    def recent():
        conn = db.init_db()
        try:
            rows = conn.execute(
                "SELECT id, ts, bar_ts, strategy_id, symbol, signal_type, close "
                "FROM signals WHERE bar_interval = 'tv-webhook' "
                "ORDER BY id DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
        return jsonify([dict(r) for r in rows])

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    secret = _resolve_secret()
    if secret is None:
        log("tv_webhook: NO secret configured — webhook will accept anonymous "
            "POSTs. Set credentials.json.tradingview.webhook_secret or env "
            "TV_WEBHOOK_SECRET before exposing publicly.", "WARNING")
    else:
        log(f"tv_webhook: secret configured (len={len(secret)})", "INFO")

    app = create_app()
    log(f"tv_webhook listening on http://{args.host}:{args.port}", "SUCCESS")
    app.run(host=args.host, port=args.port, debug=args.debug)
