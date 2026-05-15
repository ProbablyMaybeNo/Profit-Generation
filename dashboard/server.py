"""
server.py — Lightweight Flask dashboard for the trading system.

Serves dashboard/index.html at http://localhost:8080/ and exposes:
  GET /api/status  — legacy account heartbeat (kept for back-compat)
  GET /api/state   — full panel state in one round-trip (preferred)

State is computed from data/trading.db so the page renders instantly even
when external APIs (Alpaca, Polygon) are unreachable.
"""

import json
import os
import statistics
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from config.utils import get_account_summary, market_is_open, load_settings  # noqa: E402
from data import db  # noqa: E402

app = Flask(__name__)
DASHBOARD_DIR = Path(__file__).parent
LOG_DIR = ROOT / "logs"
HEARTBEAT_LOG = LOG_DIR / "heartbeat.log"
INTRADAY_LOG = LOG_DIR / "intraday_alerts.log"
DATA_DIR = ROOT / "data"
SETTINGS_FILE = ROOT / "config" / "settings.json"

# Only these keys are mutable via the toggle endpoint. Everything else has to
# go through editing settings.json directly (deliberate friction).
TOGGLE_ALLOWED_KEYS = {"enabled", "dry_run"}
LOOPBACK_IPS = {"127.0.0.1", "::1", "localhost"}


def _last_heartbeat():
    if not HEARTBEAT_LOG.exists():
        return None, None
    try:
        lines = HEARTBEAT_LOG.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return None, None
    if not lines:
        return None, None
    last_line = lines[-1]
    try:
        ts_str = last_line[1:20]
        last_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        minutes_ago = int((datetime.now() - last_dt).total_seconds() / 60)
        return last_dt.isoformat(), minutes_ago
    except Exception:
        return None, None


def _safe_account():
    try:
        return get_account_summary()
    except Exception:
        return None


def _today_iso() -> str:
    return date.today().isoformat()


def _state_strategy_edge(conn) -> list:
    rows = conn.execute(
        "SELECT s.strategy_id, o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' AND o.return_pct IS NOT NULL "
        "   AND s.bar_interval = '1d'"
    ).fetchall()
    by_strat: dict = {}
    for r in rows:
        by_strat.setdefault(r["strategy_id"], []).append(r["return_pct"])
    out = []
    for sid, rets in by_strat.items():
        n = len(rets)
        mean = sum(rets) / n
        wr = sum(1 for x in rets if x > 0) / n
        sd = statistics.stdev(rets) if n > 1 else 0.0
        sharpe = (mean / sd) if sd > 0 else 0.0
        out.append({
            "strategy_id": sid,
            "n": n,
            "mean_ret": round(mean, 3),
            "win_rate": round(wr, 4),
            "sharpe_ish": round(sharpe, 3),
            "max_loss": round(min(rets), 2),
            "max_win": round(max(rets), 2),
        })
    out.sort(key=lambda x: x["mean_ret"], reverse=True)
    return out


def _state_open_positions(conn) -> list:
    rows = conn.execute(
        "SELECT s.strategy_id, s.symbol, o.entry_ts, o.entry_price "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'open' "
        " ORDER BY o.entry_ts ASC"
    ).fetchall()
    today = date.today()
    out = []
    for r in rows:
        sym = r["symbol"]
        latest_snap = conn.execute(
            "SELECT close, snapshot_date FROM snapshots "
            " WHERE symbol = ? "
            " ORDER BY snapshot_date DESC LIMIT 1",
            (sym,),
        ).fetchone()
        current_price = latest_snap["close"] if latest_snap else None
        unrealised_pct = None
        if current_price is not None and r["entry_price"] not in (None, 0):
            unrealised_pct = round(
                (current_price - r["entry_price"]) / r["entry_price"] * 100, 2
            )
        try:
            entry_d = date.fromisoformat(r["entry_ts"][:10])
            days_open = (today - entry_d).days
        except Exception:
            days_open = None
        out.append({
            "strategy_id": r["strategy_id"],
            "symbol": sym,
            "entry_ts": r["entry_ts"],
            "entry_price": r["entry_price"],
            "current_price": current_price,
            "current_as_of": latest_snap["snapshot_date"] if latest_snap else None,
            "unrealised_pct": unrealised_pct,
            "days_open": days_open,
        })
    return out


def _state_today_signals(conn, *, limit: int = 30) -> list:
    today = _today_iso()
    rows = conn.execute(
        "SELECT id, ts, bar_ts, bar_interval, strategy_id, symbol, "
        "       signal_type, close "
        "  FROM signals "
        " WHERE bar_ts = ? "
        " ORDER BY ts DESC, id DESC LIMIT ?",
        (today, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _state_recent_news(conn, *, limit: int = 15) -> list:
    rows = conn.execute(
        "SELECT id, published_utc, symbol, publisher, title, url "
        "  FROM news "
        " ORDER BY published_utc DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


REVIEW_LOSS_THRESHOLD_PCT = -8.0


def _tv_url(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={symbol}"


def _short_strat(sid: str) -> str:
    return (sid or "").replace("botnet101-", "")


def _state_action_queue(conn) -> list:
    """
    Prioritised action list — what to actually do today.

    EXIT  rows: today's long_exit signals on (strategy, symbol) we currently hold
    ENTER rows: today's long_entry signals on (strategy, symbol) we don't already hold
    REVIEW rows: currently-open positions with unrealised <= REVIEW_LOSS_THRESHOLD_PCT

    Deduped across bar_interval (1d / 1d-intraday / tv-webhook); intervals merged
    onto each row so the user sees "fired in 1d-intraday + 1d" in one place.
    """
    today = _today_iso()

    open_rows = conn.execute(
        "SELECT s.strategy_id, s.symbol, o.entry_price, o.entry_ts "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'open'"
    ).fetchall()
    open_set = {(r["strategy_id"], r["symbol"]): r for r in open_rows}

    sig_rows = conn.execute(
        "SELECT strategy_id, symbol, signal_type, bar_interval, close, ts "
        "  FROM signals WHERE bar_ts = ?",
        (today,),
    ).fetchall()

    grouped: dict = {}
    for s in sig_rows:
        key = (s["strategy_id"], s["symbol"], s["signal_type"])
        bucket = grouped.setdefault(key, {
            "strategy_id": s["strategy_id"],
            "symbol":       s["symbol"],
            "signal_type":  s["signal_type"],
            "intervals":    set(),
            "close":        None,
        })
        bucket["intervals"].add(s["bar_interval"])
        if s["close"] is not None:
            bucket["close"] = float(s["close"])

    def latest_close(sym):
        row = conn.execute(
            "SELECT close FROM snapshots WHERE symbol=? "
            "ORDER BY snapshot_date DESC LIMIT 1", (sym,),
        ).fetchone()
        return float(row["close"]) if row and row["close"] is not None else None

    queue = []

    for (sid, sym, stype), b in grouped.items():
        if stype == "long_exit" and (sid, sym) in open_set:
            entry = open_set[(sid, sym)]
            cur = b["close"] or latest_close(sym)
            unreal = None
            if cur is not None and entry["entry_price"]:
                unreal = round((cur - entry["entry_price"]) / entry["entry_price"] * 100, 2)
            queue.append({
                "action": "EXIT",
                "priority": 1,
                "strategy_id": sid,
                "symbol": sym,
                "current_price": cur,
                "entry_price": entry["entry_price"],
                "unrealised_pct": unreal,
                "intervals": sorted(b["intervals"]),
                "tv_url": _tv_url(sym),
                "paste": f"SELL {sym} ~{cur:.2f} {sid}" if cur is not None
                         else f"SELL {sym} {sid}",
                "note": f"exit signal fired today; entered {entry['entry_ts'][:10]}",
            })
        elif stype == "long_entry" and (sid, sym) not in open_set:
            cur = b["close"] or latest_close(sym)
            queue.append({
                "action": "ENTER",
                "priority": 2,
                "strategy_id": sid,
                "symbol": sym,
                "current_price": cur,
                "entry_price": None,
                "unrealised_pct": None,
                "intervals": sorted(b["intervals"]),
                "tv_url": _tv_url(sym),
                "paste": f"BUY {sym} ~{cur:.2f} {sid}" if cur is not None
                         else f"BUY {sym} {sid}",
                "note": "new entry signal today",
            })
        # long_entry on a (sid, sym) we already hold = no-op (mechanical strategies
        # only hold one position at a time; outcome_tracker handles dedupe)

    today_d = date.today()
    for k, entry in open_set.items():
        sid, sym = k
        cur = latest_close(sym)
        if cur is None or not entry["entry_price"]:
            continue
        unreal = (cur - entry["entry_price"]) / entry["entry_price"] * 100
        if unreal > REVIEW_LOSS_THRESHOLD_PCT:
            continue
        if any(q["action"] == "EXIT" and q["strategy_id"] == sid
               and q["symbol"] == sym for q in queue):
            continue
        try:
            days_open = (today_d - date.fromisoformat(entry["entry_ts"][:10])).days
        except Exception:
            days_open = None
        queue.append({
            "action": "REVIEW",
            "priority": 3,
            "strategy_id": sid,
            "symbol": sym,
            "current_price": cur,
            "entry_price": entry["entry_price"],
            "unrealised_pct": round(unreal, 2),
            "intervals": [],
            "tv_url": _tv_url(sym),
            "paste": f"REVIEW {sym} ({entry['entry_price']:.2f} -> {cur:.2f}, {unreal:+.2f}%)",
            "note": f"down {unreal:.1f}% over {days_open}d; no exit signal — manual review",
        })

    queue.sort(key=lambda x: (x["priority"], x["symbol"], x["strategy_id"]))
    return queue


def _state_today_report(conn) -> dict:
    today = _today_iso()
    row = conn.execute(
        "SELECT report_date, market_regime, importance, has_notable_pattern, "
        "       fires_count, watchlist_count, notable_movers_count, "
        "       tags_json, notion_page_id, generated_at "
        "  FROM daily_reports WHERE report_date = ?",
        (today,),
    ).fetchone()
    if not row:
        return {}
    out = dict(row)
    try:
        out["tags"] = json.loads(out.pop("tags_json") or "[]")
    except Exception:
        out["tags"] = []
    return out


def _read_auto_trade_settings() -> dict:
    """Always read from disk so the dashboard reflects external edits too."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return (json.load(f).get("auto_trade") or {})
    except Exception:
        return {}


def _atomic_write_settings(updated: dict) -> None:
    """Replace settings.json atomically (temp file + rename)."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _is_loopback_request() -> bool:
    addr = (request.remote_addr or "").split("%")[0]
    return addr in LOOPBACK_IPS


def _state_paper_trades_today(conn) -> list:
    """Today's submitted paper orders (any status, any side)."""
    rows = conn.execute(
        "SELECT id, alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        "       order_type, submitted_at, filled_at, fill_price, status, notes "
        "  FROM paper_trades "
        " WHERE DATE(submitted_at) = DATE(?) "
        " ORDER BY submitted_at DESC, id DESC",
        (_today_iso(),),
    ).fetchall()
    return [dict(r) for r in rows]


def _state_intraday_tail(n: int = 10) -> list:
    if not INTRADAY_LOG.exists():
        return []
    try:
        lines = INTRADAY_LOG.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return []
    return lines[-n:][::-1]


@app.route("/")
def index():
    return send_from_directory(str(DASHBOARD_DIR), "index.html")


@app.route("/api/status")
def status():
    """Legacy endpoint — kept for any old consumer."""
    account = _safe_account()
    last_seen, minutes_ago = _last_heartbeat()
    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "market_open": market_is_open(),
        "account": account or {"error": "unreachable"},
        "heartbeat": {"last_seen": last_seen, "minutes_ago": minutes_ago},
        "active_strategies": [],
        "recent_logs": [],
    })


@app.route("/api/state")
def state():
    """One round-trip rollup powering the live panel."""
    conn = db.init_db()
    try:
        account = _safe_account()
        last_seen, minutes_ago = _last_heartbeat()
        is_open = False
        try:
            is_open = market_is_open()
        except Exception:
            pass
        return jsonify({
            "now": datetime.now().isoformat(timespec="seconds"),
            "market_open": is_open,
            "account": account,
            "heartbeat": {"last_seen": last_seen, "minutes_ago": minutes_ago},
            "today_report": _state_today_report(conn),
            "action_queue": _state_action_queue(conn),
            "strategy_edge": _state_strategy_edge(conn),
            "open_positions": _state_open_positions(conn),
            "today_signals": _state_today_signals(conn),
            "recent_news": _state_recent_news(conn),
            "paper_trades_today": _state_paper_trades_today(conn),
            "auto_trade_settings": _read_auto_trade_settings(),
            "intraday_alerts_tail": _state_intraday_tail(),
        })
    finally:
        conn.close()


@app.route("/api/auto_trade/toggle", methods=["POST"])
def auto_trade_toggle():
    """Mutate one boolean key in settings.json.auto_trade. Loopback only."""
    if not _is_loopback_request():
        return jsonify({"error": "loopback only"}), 403
    try:
        body = request.get_json(force=True, silent=False)
    except Exception:
        body = None
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    key = body.get("key")
    value = body.get("value")
    if key not in TOGGLE_ALLOWED_KEYS:
        return jsonify({"error": f"key must be one of {sorted(TOGGLE_ALLOWED_KEYS)}"}), 400
    if not isinstance(value, bool):
        return jsonify({"error": "value must be boolean"}), 400

    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            settings = json.load(f)
    except Exception as e:
        return jsonify({"error": f"settings read failed: {e}"}), 500

    auto = settings.setdefault("auto_trade", {})
    prev = auto.get(key)
    auto[key] = value

    try:
        _atomic_write_settings(settings)
    except Exception as e:
        return jsonify({"error": f"settings write failed: {e}"}), 500

    return jsonify({
        "ok": True,
        "key": key,
        "previous": prev,
        "current": value,
        "auto_trade": auto,
    })


if __name__ == "__main__":
    settings = load_settings()
    port = settings.get("dashboard_port", 8080)
    print(f"Trading dashboard at http://localhost:{port}/")
    app.run(host="0.0.0.0", port=port, debug=False)
