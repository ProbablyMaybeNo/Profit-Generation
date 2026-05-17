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
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

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
DOCS_DIR = ROOT / "docs"
SETTINGS_FILE = ROOT / "config" / "settings.json"

# Whitelist of guides exposed to the UI. Anything not in here returns 404 —
# prevents path traversal AND keeps the panel's chooser well-defined.
GUIDES = {
    "tradingview": "TRADINGVIEW_GUIDE.md",
}

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
    from monitoring import strategy_forecast as sf
    from monitoring import strategy_health as sh
    health_rows = sh.compute_strategy_health(conn)
    health_by_sid = {h["strategy_id"]: h for h in health_rows}
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
        forecast = sf.compute_forecast(conn, sid)
        health = health_by_sid.get(sid, {})
        out.append({
            "strategy_id": sid,
            "health": {
                "degraded": bool(health.get("degraded", False)),
                "all_time_sharpe": health.get("all_time_sharpe", 0.0),
                "last_n_sharpe": health.get("last_n_sharpe", 0.0),
                "n_recent": health.get("n_recent", 0),
                "ratio": health.get("ratio", 0.0),
                "reason": health.get("reason", ""),
            },
            "n": n,
            "mean_ret": round(mean, 3),
            "win_rate": round(wr, 4),
            "sharpe_ish": round(sharpe, 3),
            "max_loss": round(min(rets), 2),
            "max_win": round(max(rets), 2),
            "forecast": {
                "fires_per_month": forecast["fires_per_month"],
                "median_return_pct": forecast["median_return_pct"],
                "summary": forecast["summary"],
                "confidence": forecast["confidence"],
                "observation_days": forecast["observation_days"],
                "n_signals_observed": forecast["n_signals_observed"],
            },
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


def _state_kill_switch() -> dict:
    """Read config/kill_switch.json. Always returns a well-formed dict,
    even when the file is absent / malformed."""
    from monitoring import kill_switch
    return kill_switch.load_state()


def _state_tunnel_url() -> dict:
    """Read data/tunnel_url.txt if present. Empty dict otherwise."""
    path = DATA_DIR / "tunnel_url.txt"
    if not path.exists():
        return {"url": None, "updated_at": None, "available": False}
    try:
        url = path.read_text(encoding="utf-8").strip()
        ts = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        return {"url": None, "updated_at": None, "available": False}
    if not url:
        return {"url": None, "updated_at": ts, "available": False}
    return {"url": url, "updated_at": ts, "available": True}


def _state_macro_strip() -> list:
    """Latest VIX / T10Y2Y / DXY rollup for the dashboard header strip."""
    from monitoring import macro_fetcher
    try:
        return macro_fetcher.latest_snapshot()
    except Exception:
        return []


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
            "macro": _state_macro_strip(),
            "tv_tunnel": _state_tunnel_url(),
            "kill_switch": _state_kill_switch(),
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /api/health — UptimeRobot-style external poll target (3.5.3).
# ---------------------------------------------------------------------------

# Stale thresholds — once exceeded the health endpoint flips `ok` to False
# and surfaces the offending subsystem in `degraded`.
HEALTH_INTRADAY_STALE_MIN = 30           # last intraday scan
HEALTH_DAILY_REPORT_STALE_HOURS = 36     # last daily_report row (allows weekends)
HEALTH_TUNNEL_STALE_HOURS = 24           # last tunnel_url.txt update


def _file_age_minutes(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return int(age.total_seconds() // 60)


def _file_age_hours(path: Path) -> Optional[float]:
    mins = _file_age_minutes(path)
    return round(mins / 60, 2) if mins is not None else None


def _health_alpaca() -> str:
    """ok | blocked | unreachable. 'blocked' covers account_blocked /
    trading_blocked / pattern_day_trader. 'unreachable' is the safe
    default when the API throws."""
    try:
        acct = get_account_summary()
    except Exception:
        return "unreachable"
    if not acct:
        return "unreachable"
    status = (acct.get("status") or "").upper()
    if any(acct.get(k) for k in ("account_blocked", "trading_blocked")):
        return "blocked"
    if status and status not in ("ACTIVE", "OK"):
        return "blocked"
    return "ok"


def _health_db(conn) -> str:
    """ok | broken. Pings the schema_version row in `meta`."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    except Exception:
        return "broken"
    return "ok" if row is not None else "broken"


def _health_daily_report_age_hours(conn) -> Optional[float]:
    """Hours since the most recent daily_reports row's generated_at."""
    try:
        row = conn.execute(
            "SELECT generated_at FROM daily_reports "
            "ORDER BY report_date DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return None
    if row is None or not row["generated_at"]:
        return None
    try:
        gen = datetime.fromisoformat(str(row["generated_at"]).replace("Z", "+00:00"))
    except Exception:
        return None
    # Compute the delta in a tz-aware fashion to avoid utcnow() deprecation
    # and DST traps. If `gen` came in as naive, assume UTC.
    from datetime import timezone as _tz
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=_tz.utc)
    delta = datetime.now(_tz.utc) - gen
    return round(delta.total_seconds() / 3600, 2)


def _health_open_positions(conn) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT signal_id FROM paper_trades "
            "  WHERE side='buy' AND status NOT IN ('rejected','canceled') "
            "  GROUP BY signal_id "
            "  EXCEPT "
            "  SELECT signal_id FROM paper_trades "
            "  WHERE side='sell' AND status NOT IN ('rejected','canceled') "
            "  GROUP BY signal_id"
            ")"
        ).fetchone()
    except Exception:
        return 0
    return int(row["n"]) if row else 0


@app.route("/api/health")
def api_health():
    """UptimeRobot-style external poll target.

    Returns 200 with `{ok: bool, ...}` regardless of degradation — HTTP
    code stays 200 so the monitor sees the JSON body; consumers parse
    `ok` + `degraded` to decide what to alert on.
    """
    conn = db.init_db()
    try:
        intraday_age = _file_age_minutes(INTRADAY_LOG)
        daily_age = _health_daily_report_age_hours(conn)
        tunnel_age = _file_age_hours(DATA_DIR / "tunnel_url.txt")
        ks_state = _state_kill_switch()
        ks_engaged = bool(ks_state.get("live_trading_halted"))
        alpaca = _health_alpaca()
        db_status = _health_db(conn)
        open_pos = _health_open_positions(conn)

        degraded: List[str] = []
        if alpaca != "ok":
            degraded.append(f"alpaca:{alpaca}")
        if db_status != "ok":
            degraded.append(f"db:{db_status}")
        # Intraday is only expected to be fresh during market hours.
        if intraday_age is None:
            degraded.append("intraday:no_log")
        elif intraday_age > HEALTH_INTRADAY_STALE_MIN and market_is_open():
            degraded.append(f"intraday:stale_{intraday_age}min")
        if daily_age is None:
            degraded.append("daily_report:none")
        elif daily_age > HEALTH_DAILY_REPORT_STALE_HOURS:
            degraded.append(f"daily_report:stale_{daily_age}h")
        if tunnel_age is None:
            degraded.append("tunnel:no_file")
        elif tunnel_age > HEALTH_TUNNEL_STALE_HOURS:
            degraded.append(f"tunnel:stale_{tunnel_age}h")
        if ks_engaged:
            degraded.append("kill_switch:engaged")

        body = {
            "ok": len(degraded) == 0,
            "now": datetime.now().isoformat(timespec="seconds"),
            "alpaca": alpaca,
            "db": db_status,
            "intraday_age_min": intraday_age,
            "daily_report_age_h": daily_age,
            "tunnel_age_h": tunnel_age,
            "kill_switch": ks_engaged,
            "open_positions": open_pos,
            "degraded": degraded,
        }
        return jsonify(body), 200
    finally:
        conn.close()


@app.route("/api/kill_switch", methods=["POST"])
def kill_switch_post():
    """Engage or release the live-trading kill switch. Loopback only.

    Body: {"action": "engage", "reason": "..."} or {"action": "release"}.
    Returns the new state. Engaging is idempotent (re-engages with the new
    reason / set_at); releasing is idempotent too.
    """
    if not _is_loopback_request():
        return jsonify({"error": "loopback only"}), 403
    try:
        body = request.get_json(force=True, silent=False)
    except Exception:
        body = None
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    action = (body.get("action") or "").lower().strip()
    from monitoring import kill_switch as ks
    if action == "engage":
        reason = body.get("reason") or "(manual via dashboard)"
        state = ks.engage(str(reason))
    elif action == "release":
        state = ks.release()
    else:
        return jsonify({"error": "action must be 'engage' or 'release'"}), 400
    return jsonify({"ok": True, "kill_switch": state})


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


# ----- Manual trigger registry -----
# Maps trigger_id → list of args fed to `python -m`. Anything not in here is
# rejected (no arbitrary subprocess execution from a POST body).
TRIGGER_REGISTRY: dict[str, list[str]] = {
    "daily_report":   ["monitoring.daily_report"],
    "intraday_scan":  ["monitoring.intraday_monitor", "--once", "--no-market-check"],
    "auto_trader":    ["monitoring.auto_trader"],
}

# Process handles + last-launch metadata, in-memory only.
_LAST_TRIGGERED: dict[str, dict] = {}


def _spawn_trigger(trigger_id: str) -> dict:
    """Spawn the registered command in a detached subprocess. Returns metadata.
    Uses `subprocess.Popen` looked up at call time so tests can monkeypatch it.
    """
    args = TRIGGER_REGISTRY[trigger_id]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    log_dir = LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"trigger_{trigger_id}.log"
    log_handle = open(log_path, "ab")
    started_at = datetime.now().isoformat(timespec="seconds")
    log_handle.write(("\n=== triggered " + started_at + " ===\n").encode("utf-8"))
    log_handle.flush()
    proc = subprocess.Popen(
        [sys.executable, "-m", *args],
        cwd=str(ROOT), env=env,
        stdout=log_handle, stderr=log_handle,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                       | getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    meta = {
        "trigger_id": trigger_id,
        "args": args,
        "pid": proc.pid,
        "started_at": started_at,
        "log_path": str(log_path),
    }
    _LAST_TRIGGERED[trigger_id] = meta
    return meta


def _state_equity_curve(conn, strategy_id: str) -> dict:
    """Return per-strategy cumulative-return points and drawdown overlay.

    Pulls closed outcomes for the strategy (bar_interval='1d' only,
    matching the strategy_edge card), orders chronologically by exit_ts,
    builds cumulative return as sum of per-trade return_pct, and
    derives drawdown as (cum - running_max).
    Result shape:
      {
        "strategy_id": "...",
        "points": [{"date": "YYYY-MM-DD", "cum_pct": float,
                    "drawdown_pct": float, "trade_pct": float}, ...],
        "n_trades": int,
        "final_pct": float,
        "max_drawdown_pct": float,
      }
    Empty for unknown strategies / zero outcomes.
    """
    rows = conn.execute(
        "SELECT o.exit_ts, o.return_pct "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status = 'closed' AND o.return_pct IS NOT NULL "
        "   AND s.strategy_id = ? AND s.bar_interval = '1d' "
        " ORDER BY o.exit_ts ASC, o.signal_id ASC",
        (strategy_id,),
    ).fetchall()
    points: list = []
    cum = 0.0
    running_max = 0.0
    max_dd = 0.0
    for r in rows:
        ret = float(r["return_pct"])
        cum += ret
        if cum > running_max:
            running_max = cum
        dd = cum - running_max  # <= 0
        if dd < max_dd:
            max_dd = dd
        exit_ts = r["exit_ts"] or ""
        points.append({
            "date": exit_ts[:10],
            "cum_pct": round(cum, 4),
            "drawdown_pct": round(dd, 4),
            "trade_pct": round(ret, 4),
        })
    return {
        "strategy_id": strategy_id,
        "points": points,
        "n_trades": len(points),
        "final_pct": round(cum, 4) if points else 0.0,
        "max_drawdown_pct": round(max_dd, 4) if points else 0.0,
    }


@app.route("/api/equity_curve/<strategy_id>", methods=["GET"])
def equity_curve(strategy_id: str):
    conn = db.init_db()
    try:
        return jsonify(_state_equity_curve(conn, strategy_id))
    finally:
        conn.close()


@app.route("/api/macro", methods=["GET"])
def macro():
    """Latest macro snapshot — VIX / T10Y2Y / DXY (broad dollar)."""
    return jsonify({"series": _state_macro_strip()})


@app.route("/api/tunnel", methods=["GET"])
def tunnel():
    """Latest TradingView webhook tunnel URL from data/tunnel_url.txt."""
    return jsonify(_state_tunnel_url())


@app.route("/api/edge_slices", methods=["GET"])
def edge_slices():
    """Per-strategy edge sliced by day-of-week, market regime, and VIX
    quartile. The rollup is computed against trading.db on every call —
    fast enough at this dataset size, and avoids stale-cache issues."""
    from monitoring import edge_slicer  # local import, optional dep on db
    conn = db.init_db()
    try:
        return jsonify(edge_slicer.compute_edge_slices(conn))
    finally:
        conn.close()


@app.route("/api/strategy_correlation", methods=["GET"])
def strategy_correlation():
    """Pairwise daily-P&L correlation between active strategies."""
    from monitoring import strategy_correlation as sc
    conn = db.init_db()
    try:
        return jsonify(sc.compute_strategy_correlation(conn))
    finally:
        conn.close()


@app.route("/api/edge_diff", methods=["GET"])
def edge_diff():
    """Realized-vs-theoretical edge per strategy. Computed live from
    paper_trades + strategies.raw_record_json.test_runs — same shape as
    the snapshot written by scripts/edge_diff.py."""
    from monitoring import edge_diff as ed
    conn = db.init_db()
    try:
        return jsonify(ed.compute_edge_diff(conn))
    finally:
        conn.close()


@app.route("/api/slippage_burn", methods=["GET"])
def slippage_burn():
    """Compact ranked-by-burn view of edge_diff (milestone 3.6.1).

    One row per strategy with a usable theoretical baseline AND closed
    paper pairs, expressed as: expected /signal, actual /signal, burn %.
    Sorted by burn % descending (worst first)."""
    from monitoring import edge_diff as ed
    conn = db.init_db()
    try:
        return jsonify(ed.compute_slippage_burn(conn))
    finally:
        conn.close()


@app.route("/api/fill_latency", methods=["GET"])
def fill_latency():
    """Per-strategy fill-time latency rollup (milestone 3.6.3).

    Median + p95 fill-delta (filled_at - submitted_at) per strategy,
    plus a count of outliers (> 5min). Sorted by median desc (worst
    latency first)."""
    from monitoring import fill_latency as fl
    conn = db.init_db()
    try:
        return jsonify(fl.compute_fill_latency(conn))
    finally:
        conn.close()


@app.route("/api/news_sentiment_overlay", methods=["GET"])
def news_sentiment_overlay():
    """Per-strategy outcome returns sliced by entry-day news sentiment
    (positive / neutral / negative / no_news)."""
    from monitoring import news_sentiment_overlay as nso
    conn = db.init_db()
    try:
        return jsonify(nso.compute_overlay(conn))
    finally:
        conn.close()


@app.route("/api/strategy_forecast/<strategy_id>", methods=["GET"])
def strategy_forecast(strategy_id: str):
    """Calibrated fires-per-month + median-return expectation for one
    strategy. Same shape as the forecast block embedded in /api/state's
    strategy_edge rows."""
    from monitoring import strategy_forecast as sf
    conn = db.init_db()
    try:
        return jsonify(sf.compute_forecast(conn, strategy_id))
    finally:
        conn.close()


@app.route("/api/guide/<name>", methods=["GET"])
def get_guide(name: str):
    """Serve a markdown guide as plain text. Whitelist enforced."""
    fname = GUIDES.get(name.lower())
    if fname is None:
        return jsonify({
            "error": f"unknown guide '{name}'",
            "available": sorted(GUIDES.keys()),
        }), 404
    path = DOCS_DIR / fname
    if not path.exists():
        return jsonify({"error": f"guide file missing: {fname}"}), 500
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return jsonify({"error": f"read failed: {e}"}), 500
    return text, 200, {"Content-Type": "text/markdown; charset=utf-8"}


@app.route("/api/guide", methods=["GET"])
def list_guides():
    return jsonify({"available": sorted(GUIDES.keys())})


@app.route("/api/run/<trigger_id>", methods=["POST"])
def manual_trigger(trigger_id: str):
    if not _is_loopback_request():
        return jsonify({"error": "loopback only"}), 403
    if trigger_id not in TRIGGER_REGISTRY:
        return jsonify({
            "error": f"unknown trigger; allowed: {sorted(TRIGGER_REGISTRY.keys())}"
        }), 404
    try:
        meta = _spawn_trigger(trigger_id)
    except Exception as e:
        return jsonify({"error": f"spawn failed: {e}"}), 500
    return jsonify({"ok": True, **meta}), 202


@app.route("/api/run", methods=["GET"])
def manual_trigger_status():
    return jsonify({
        "available": sorted(TRIGGER_REGISTRY.keys()),
        "last_triggered": _LAST_TRIGGERED,
    })


# Public, sanitized, rate-limited read-only endpoints (milestone 4.4.1).
# Registered LAST so private routes always win the route table.
from dashboard import public_api as _public_api  # noqa: E402

_public_api.register(app, db_module=db)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind-all", action="store_true",
                        help="Bind 0.0.0.0 (LAN-exposed). Default is 127.0.0.1 "
                             "loopback-only — PG-011 security fix (3.5.1).")
    parser.add_argument("--port", type=int, default=None,
                        help="Override settings.dashboard_port")
    args = parser.parse_args()
    settings = load_settings()
    port = args.port if args.port is not None else settings.get("dashboard_port", 8080)
    host = "0.0.0.0" if args.bind_all else "127.0.0.1"
    if args.bind_all:
        print(f"[dashboard] WARNING: binding 0.0.0.0:{port} (LAN-exposed). "
              f"Account data is unauthenticated — only do this on a trusted network.")
    print(f"Profit Generation dashboard at http://{host}:{port}/")
    app.run(host=host, port=port, debug=False)
