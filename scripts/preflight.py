"""
preflight.py — Pre-flight sanity checks before flipping any live config.

Runs each check in order, prints `PASS` / `FAIL` per check with a short
explanation, and exits non-zero if any check FAILed. Designed to be run
manually before flipping auto_trade settings, switching to live mode,
or going to bed expecting overnight trading.

Checks (each independent, all run even if earlier ones fail):
  1. alpaca         — account ACTIVE + not blocked + paper mode confirmed
  2. credentials    — every section present in credentials.json has a
                       non-empty, non-placeholder value
  3. settings_schema — settings.json parses + required keys present
                       with sensible types
  4. db_schema      — trading.db is initialised + schema_version matches
                       data.db.SCHEMA_VERSION
  5. notion_recency — last 3 daily_reports rows each have notion_page_id
  6. intraday_scan  — if market is open, last intraday scan within 30 min
  7. tunnel_url     — data/tunnel_url.txt exists + < 24h old

Usage:
  py -3.13 scripts/preflight.py
  py -3.13 scripts/preflight.py --json   # machine-readable
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


REQUIRED_CRED_SECTIONS = ("alpaca", "polygon", "fred", "notion",
                           "telegram", "tradingview")
REQUIRED_SETTINGS_KEYS = ("timezone", "dashboard_port",
                           "paper_trading", "risk", "auto_trade")
PLACEHOLDER_MARKERS = ("PASTE_", "YOUR_", "secret_PASTE")
INTRADAY_LOG_NAME = "intraday_alerts.log"
HEARTBEAT_LOG_NAME = "heartbeat.log"
TUNNEL_URL_NAME = "tunnel_url.txt"
INTRADAY_MAX_STALE_MIN = 30
TUNNEL_MAX_STALE_HOURS = 24
NOTION_RECENCY_COUNT = 3


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _pass(name: str, detail: str = "") -> Dict:
    return {"check": name, "status": "PASS", "detail": detail}


def _fail(name: str, detail: str) -> Dict:
    return {"check": name, "status": "FAIL", "detail": detail}


def _skip(name: str, detail: str) -> Dict:
    return {"check": name, "status": "SKIP", "detail": detail}


# ---- Individual checks (each takes no required args, returns dict) --------

def check_alpaca(*, account_summary_fn: Optional[Callable] = None,
                 is_paper_mode_fn: Optional[Callable] = None) -> Dict:
    name = "alpaca"
    try:
        from config.utils import get_alpaca_client, is_paper_mode
        is_paper_mode_fn = is_paper_mode_fn or is_paper_mode
        if not is_paper_mode_fn():
            return _fail(name, "credentials.alpaca.paper is False — "
                                "preflight only allows paper")
        if account_summary_fn is None:
            client = get_alpaca_client()
            acct = client.get_account()
            status = getattr(acct, "status", "?")
            blocked = bool(getattr(acct, "trading_blocked", False)
                            or getattr(acct, "account_blocked", False))
        else:
            acct = account_summary_fn()
            status = acct.get("status", "?")
            blocked = bool(acct.get("blocked", False))
    except Exception as e:
        return _fail(name, f"alpaca client/account lookup failed: {e}")
    if str(status).upper() != "ACTIVE":
        return _fail(name, f"account status={status} (expected ACTIVE)")
    if blocked:
        return _fail(name, "account is blocked")
    return _pass(name, f"status=ACTIVE · paper · not blocked")


def check_credentials(*, path: Optional[Path] = None) -> Dict:
    name = "credentials"
    p = Path(path) if path is not None else ROOT / "config" / "credentials.json"
    if not p.exists():
        return _fail(name, f"missing {p.name}")
    try:
        with open(p, encoding="utf-8") as f:
            creds = json.load(f)
    except Exception as e:
        return _fail(name, f"unparseable: {e}")
    missing: List[str] = []
    placeholder: List[str] = []
    for section in REQUIRED_CRED_SECTIONS:
        s = creds.get(section)
        if not isinstance(s, dict) or not s:
            missing.append(section)
            continue
        for k, v in s.items():
            if k.startswith("_"):
                continue
            sv = str(v) if v is not None else ""
            if v in (None, ""):
                placeholder.append(f"{section}.{k}=empty")
                continue
            if any(m in sv for m in PLACEHOLDER_MARKERS):
                placeholder.append(f"{section}.{k}=placeholder")
    if missing or placeholder:
        parts = []
        if missing:
            parts.append(f"missing sections: {', '.join(missing)}")
        if placeholder:
            parts.append(f"placeholder/empty: {', '.join(placeholder)}")
        return _fail(name, " · ".join(parts))
    return _pass(name, f"{len(REQUIRED_CRED_SECTIONS)} sections all populated")


def check_settings_schema(*, path: Optional[Path] = None) -> Dict:
    name = "settings_schema"
    p = Path(path) if path is not None else ROOT / "config" / "settings.json"
    if not p.exists():
        return _fail(name, f"missing {p.name}")
    try:
        with open(p, encoding="utf-8") as f:
            settings = json.load(f)
    except Exception as e:
        return _fail(name, f"unparseable: {e}")
    missing = [k for k in REQUIRED_SETTINGS_KEYS if k not in settings]
    if missing:
        return _fail(name, f"missing keys: {', '.join(missing)}")
    if not isinstance(settings.get("risk"), dict):
        return _fail(name, "risk must be an object")
    if not isinstance(settings.get("auto_trade"), dict):
        return _fail(name, "auto_trade must be an object")
    if not isinstance(settings.get("paper_trading"), bool):
        return _fail(name, "paper_trading must be a boolean")
    if not isinstance(settings.get("dashboard_port"), int):
        return _fail(name, "dashboard_port must be an integer")
    return _pass(name, f"all {len(REQUIRED_SETTINGS_KEYS)} required keys present + typed")


def check_db_schema(*, db_path: Optional[Path] = None) -> Dict:
    name = "db_schema"
    try:
        from data import db
    except Exception as e:
        return _fail(name, f"data.db import failed: {e}")
    expected = getattr(db, "SCHEMA_VERSION", "1")
    target = Path(db_path) if db_path is not None else db.DB_FILE
    if not target.exists():
        return _fail(name, f"{target.name} not found — run any pipeline once "
                            "to bootstrap")
    try:
        conn = db.connect(target)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'",
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:
        return _fail(name, f"db open/query failed: {e}")
    if not row:
        return _fail(name, "meta.schema_version row missing")
    actual = row["value"]
    if str(actual) != str(expected):
        return _fail(name,
                      f"schema_version={actual} != expected {expected}")
    return _pass(name, f"schema_version={actual}")


def check_notion_recency(*, db_path: Optional[Path] = None,
                          count: int = NOTION_RECENCY_COUNT) -> Dict:
    name = "notion_recency"
    try:
        from data import db
        target = Path(db_path) if db_path is not None else db.DB_FILE
        if not target.exists():
            return _fail(name, f"{target.name} not found")
        conn = db.connect(target)
        try:
            rows = conn.execute(
                "SELECT report_date, notion_page_id FROM daily_reports "
                " ORDER BY report_date DESC LIMIT ?",
                (count,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        return _fail(name, f"db query failed: {e}")
    if len(rows) < count:
        return _fail(name, f"only {len(rows)} daily_reports rows "
                            f"in db (need {count})")
    missing = [r["report_date"] for r in rows if not r["notion_page_id"]]
    if missing:
        return _fail(name, f"no notion_page_id for: {', '.join(missing)}")
    return _pass(name, f"last {count} reports each have notion_page_id "
                        f"(latest {rows[0]['report_date']})")


def check_intraday_scan(*, log_dir: Optional[Path] = None,
                         market_is_open_fn: Optional[Callable] = None,
                         now_fn: Optional[Callable] = None,
                         max_stale_min: int = INTRADAY_MAX_STALE_MIN) -> Dict:
    name = "intraday_scan"
    try:
        from config.utils import market_is_open
        market_is_open_fn = market_is_open_fn or market_is_open
        is_open = market_is_open_fn()
    except Exception as e:
        return _fail(name, f"market_is_open lookup failed: {e}")
    if not is_open:
        return _skip(name, "market closed — staleness only checked while open")
    p = Path(log_dir) if log_dir is not None else ROOT / "logs"
    log_path = p / INTRADAY_LOG_NAME
    fallback = p / HEARTBEAT_LOG_NAME
    target = log_path if log_path.exists() else fallback
    if not target.exists():
        return _fail(name, "no intraday_alerts.log / heartbeat.log found")
    now_fn = now_fn or _now_utc
    mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
    age = now_fn() - mtime
    age_min = int(age.total_seconds() / 60)
    if age_min > max_stale_min:
        return _fail(name, f"{target.name} last touched {age_min}m ago "
                            f"(> {max_stale_min}m threshold)")
    return _pass(name, f"{target.name} last touched {age_min}m ago")


def check_tunnel_url(*, data_dir: Optional[Path] = None,
                      now_fn: Optional[Callable] = None,
                      max_stale_hours: int = TUNNEL_MAX_STALE_HOURS) -> Dict:
    name = "tunnel_url"
    p = Path(data_dir) if data_dir is not None else ROOT / "data"
    f = p / TUNNEL_URL_NAME
    if not f.exists():
        return _fail(name, f"{TUNNEL_URL_NAME} missing — run "
                            "schedulers/start_tv_tunnel.bat")
    now_fn = now_fn or _now_utc
    mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
    age = now_fn() - mtime
    age_hours = age.total_seconds() / 3600.0
    if age_hours > max_stale_hours:
        return _fail(name, f"tunnel_url.txt is {age_hours:.1f}h old "
                            f"(> {max_stale_hours}h threshold)")
    try:
        url = f.read_text(encoding="utf-8").strip()
    except Exception as e:
        return _fail(name, f"read failed: {e}")
    if not url:
        return _fail(name, "tunnel_url.txt is empty")
    return _pass(name, f"{url[:50]}{'…' if len(url) > 50 else ''} "
                        f"({age_hours:.1f}h old)")


ALL_CHECKS: List[Callable[[], Dict]] = [
    check_alpaca,
    check_credentials,
    check_settings_schema,
    check_db_schema,
    check_notion_recency,
    check_intraday_scan,
    check_tunnel_url,
]


def run_all() -> List[Dict]:
    """Run every check, capturing per-check exceptions as FAILs."""
    results = []
    for fn in ALL_CHECKS:
        try:
            results.append(fn())
        except Exception as e:
            results.append(_fail(fn.__name__.replace("check_", ""),
                                  f"unhandled exception: {e}"))
    return results


def format_report(results: List[Dict]) -> str:
    lines = []
    width = max(len(r["check"]) for r in results) + 2
    for r in results:
        tag = r["status"]
        line = f"{tag:4s}  {r['check']:<{width}} {r['detail']}"
        lines.append(line)
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    n_skip = sum(1 for r in results if r["status"] == "SKIP")
    n_pass = sum(1 for r in results if r["status"] == "PASS")
    lines.append("")
    lines.append(f"summary: {n_pass} pass · {n_fail} fail · {n_skip} skip")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Pre-flight checks.")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON results")
    parser.add_argument("--tunnel", action="store_true",
                        help="run ONLY the tunnel_url freshness check "
                             "(milestone 4.5.1). Exits 0 if "
                             "data/tunnel_url.txt is < 24h old, non-zero "
                             "otherwise. RUNBOOK Procedure 6 uses this "
                             "instead of an inline Python one-liner.")
    args = parser.parse_args()
    if args.tunnel:
        result = check_tunnel_url()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            tag = result["status"]
            print(f"{tag}  tunnel_url  {result['detail']}")
        sys.exit(0 if result["status"] == "PASS" else 1)
    results = run_all()
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(format_report(results))
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
