"""
server.py — Lightweight Flask dashboard for the trading system.

Serves dashboard/index.html at http://localhost:8080/ and exposes:
  GET /api/status  — legacy account heartbeat (kept for back-compat)
  GET /api/state   — full panel state in one round-trip (preferred)

State is computed from data/trading.db so the page renders instantly even
when external APIs (Alpaca, Polygon) are unreachable.
"""

import json
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, send_from_directory  # noqa: E402

from config.utils import get_account_summary, market_is_open, load_settings  # noqa: E402
from data import db  # noqa: E402

app = Flask(__name__)
DASHBOARD_DIR = Path(__file__).parent
LOG_DIR = ROOT / "logs"
HEARTBEAT_LOG = LOG_DIR / "heartbeat.log"
INTRADAY_LOG = LOG_DIR / "intraday_alerts.log"
DATA_DIR = ROOT / "data"


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
            "strategy_edge": _state_strategy_edge(conn),
            "open_positions": _state_open_positions(conn),
            "today_signals": _state_today_signals(conn),
            "recent_news": _state_recent_news(conn),
            "intraday_alerts_tail": _state_intraday_tail(),
        })
    finally:
        conn.close()


if __name__ == "__main__":
    settings = load_settings()
    port = settings.get("dashboard_port", 8080)
    print(f"Trading dashboard at http://localhost:{port}/")
    app.run(host="0.0.0.0", port=port, debug=False)
