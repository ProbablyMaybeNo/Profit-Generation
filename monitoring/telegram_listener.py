"""
telegram_listener.py — Long-poll worker that listens for slash commands
from the configured Telegram chat and routes them to handlers.

Supported commands (only from the configured chat_id — others ignored):
  /halt <reason>   Engage the kill switch with optional reason.
  /resume          Release the kill switch.
  /status          One-line system health (alpaca, kill switch, open positions).
  /positions       List open paper positions from Alpaca.
  /pnl             Today's realized P&L from paper_trades.

Run as `py -3.13 -m monitoring.telegram_listener`. Polls Telegram with
`getUpdates` (long-poll timeout 25s), de-dupes via `update_id` offset
state stored in `data/telegram_offset.json`. Restart-safe: missed
messages within Telegram's 24h retention window are replayed.

If Telegram creds are absent or the placeholder, the worker logs once
and exits 0 — same gentle no-op shape as telegram_alerter.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from monitoring import telegram_alerter as ta  # noqa: E402

API_BASE = "https://api.telegram.org"
DATA_DIR = ROOT / "data"
OFFSET_FILE = DATA_DIR / "telegram_offset.json"
POLL_TIMEOUT_S = 25
SLEEP_ON_ERROR_S = 10


# ---- Offset persistence ----------------------------------------------------

def load_offset(path: Optional[Path] = None) -> int:
    p = Path(path) if path is not None else OFFSET_FILE
    if not p.exists():
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            return int(json.load(f).get("offset", 0))
    except Exception:
        return 0


def save_offset(offset: int, *, path: Optional[Path] = None) -> None:
    p = Path(path) if path is not None else OFFSET_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"offset": int(offset)}, f)


# ---- Command parsing -------------------------------------------------------

def parse_command(text: str) -> Optional[Dict[str, str]]:
    """Return {"cmd": "halt", "args": "..."} or None if not a slash command.

    Strips the leading `/`, lowercases the command name, and trims
    whitespace from args. Mentions like `/halt@MyBot reason` are handled
    by dropping anything between `@` and the next space in the command
    token.
    """
    if not text:
        return None
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split(maxsplit=1)
    if not parts:
        return None
    head = parts[0]
    if "@" in head:
        head = head.split("@", 1)[0]
    cmd = head.lower().strip()
    if not cmd:
        return None
    args = parts[1].strip() if len(parts) > 1 else ""
    return {"cmd": cmd, "args": args}


# ---- Handlers --------------------------------------------------------------

def handle_halt(args: str) -> str:
    from monitoring import kill_switch
    reason = args or "(via telegram /halt)"
    state = kill_switch.engage(reason)
    return (
        f"⛔ Kill switch ENGAGED — auto-trader will refuse all new entries.\n"
        f"reason: {state['reason']}\n"
        f"set at: {state['set_at']}"
    )


def handle_resume(args: str) -> str:
    from monitoring import kill_switch
    state = kill_switch.release()
    return f"✅ Kill switch RELEASED at {state['set_at']} — entries will resume."


def handle_status(args: str) -> str:
    from monitoring import kill_switch
    from data import db
    ks = kill_switch.load_state()
    halted = ks.get("live_trading_halted")
    try:
        from config.utils import get_account_summary
        acct = get_account_summary()
        acct_line = f"equity ${acct['portfolio_value']:.2f} · cash ${acct['cash']:.2f}"
    except Exception as e:
        acct_line = f"alpaca: unreachable ({str(e)[:60]})"
    try:
        conn = db.init_db()
        open_n = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE status='open'"
        ).fetchone()[0]
        conn.close()
    except Exception:
        open_n = "?"
    state_word = "HALTED" if halted else "RUNNING"
    line = f"status: {state_word} · {acct_line} · {open_n} open positions"
    if halted:
        line += f"\nhalt reason: {ks.get('reason') or '(none)'}"
    return line


def handle_positions(args: str) -> str:
    try:
        from config.utils import get_alpaca_client
        client = get_alpaca_client()
        positions = client.get_all_positions()
    except Exception as e:
        return f"positions: alpaca unreachable ({str(e)[:80]})"
    if not positions:
        return "positions: none open"
    lines = ["📊 Open positions:"]
    for p in positions:
        sym = getattr(p, "symbol", "?")
        qty = getattr(p, "qty", "?")
        avg = float(getattr(p, "avg_entry_price", 0) or 0)
        cur = float(getattr(p, "current_price", 0) or 0)
        pl_pct = ((cur - avg) / avg * 100) if avg else 0.0
        sign = "+" if pl_pct >= 0 else ""
        lines.append(f"• {sym} ×{qty} @ ${avg:.2f} → ${cur:.2f} ({sign}{pl_pct:.2f}%)")
    return "\n".join(lines)


def handle_pnl(args: str) -> str:
    """Compute today's realised P&L from paper_trades closed pairs.

    Approximation: for each SELL submitted today, find the most-recent
    non-canceled BUY of the same (strategy, symbol) submitted at or before
    that sell and compute qty * (sell_fill - buy_fill). Pairs missing a
    fill_price on either leg are skipped.
    """
    from datetime import date
    from data import db
    today_iso = date.today().isoformat()
    try:
        conn = db.init_db()
        rows = conn.execute(
            "SELECT strategy_id, symbol, side, qty, fill_price, submitted_at "
            "  FROM paper_trades "
            " WHERE DATE(submitted_at) = DATE(?) "
            " ORDER BY submitted_at ASC",
            (today_iso,),
        ).fetchall()
    except Exception as e:
        return f"pnl: db unreachable ({str(e)[:80]})"
    if not rows:
        try:
            conn.close()
        except Exception:
            pass
        return f"pnl: no trades today ({today_iso})"
    realised = 0.0
    closed = 0
    try:
        for r in rows:
            if r["side"] != "sell":
                continue
            sell_px = r["fill_price"]
            qty = r["qty"]
            if sell_px is None or qty is None:
                continue
            buy = conn.execute(
                "SELECT fill_price FROM paper_trades "
                " WHERE strategy_id=? AND symbol=? AND side='buy' "
                "   AND submitted_at <= ? "
                "   AND status NOT IN ('canceled', 'rejected') "
                " ORDER BY submitted_at DESC LIMIT 1",
                (r["strategy_id"], r["symbol"], r["submitted_at"]),
            ).fetchone()
            if buy is None or buy["fill_price"] is None:
                continue
            realised += float(qty) * (float(sell_px) - float(buy["fill_price"]))
            closed += 1
    finally:
        conn.close()
    sign = "+" if realised >= 0 else ""
    return (
        f"📈 Today's realised P&L: {sign}${realised:.2f} "
        f"({closed} closed pair(s), {len(rows)} total order(s))"
    )


HANDLERS: Dict[str, Callable[[str], str]] = {
    "halt":      handle_halt,
    "resume":    handle_resume,
    "status":    handle_status,
    "positions": handle_positions,
    "pnl":       handle_pnl,
}


def dispatch(parsed: Dict[str, str]) -> Optional[str]:
    """Run the handler for a parsed command. Returns reply text or None."""
    if not parsed:
        return None
    handler = HANDLERS.get(parsed["cmd"])
    if handler is None:
        return None
    try:
        return handler(parsed["args"])
    except Exception as e:
        return f"⚠️ {parsed['cmd']} failed: {str(e)[:120]}"


# ---- Update filtering ------------------------------------------------------

def _expected_chat_id() -> Optional[str]:
    """The chat_id we accept commands from. None when telegram isn't configured."""
    creds = ta._resolve_creds()
    if creds is None:
        return None
    return str(creds["chat_id"])


def is_authorised(update: Dict, expected_chat_id: str) -> bool:
    """True iff `update.message.chat.id` matches the configured chat_id.

    Defensive: any missing/non-message update returns False.
    """
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    actual = chat.get("id")
    if actual is None:
        return False
    return str(actual) == str(expected_chat_id)


def extract_text(update: Dict) -> str:
    msg = update.get("message") or update.get("edited_message") or {}
    return msg.get("text") or ""


# ---- Network seams (mocked in tests) ---------------------------------------

def _http_get(url: str, params: Dict, timeout: float = POLL_TIMEOUT_S + 5):
    return requests.get(url, params=params, timeout=timeout)


def poll_once(bot_token: str, *, offset: int,
              timeout_s: int = POLL_TIMEOUT_S) -> List[Dict]:
    """One getUpdates round-trip. Returns the list of update objects
    (possibly empty). Raises on any non-200."""
    url = f"{API_BASE}/bot{bot_token}/getUpdates"
    params = {"offset": offset, "timeout": timeout_s,
              "allowed_updates": json.dumps(["message", "edited_message"])}
    r = _http_get(url, params)
    if r.status_code != 200:
        raise RuntimeError(f"getUpdates {r.status_code}: {r.text[:200]}")
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"getUpdates not ok: {body.get('description')}")
    return body.get("result") or []


# ---- Main loop -------------------------------------------------------------

def run_forever(*,
                sleep_fn=None,
                send_fn=None,
                poll_fn=None,
                offset_path: Optional[Path] = None,
                max_iterations: Optional[int] = None) -> None:
    """Long-poll loop. Pluggable seams for tests:

    - sleep_fn(seconds)
    - send_fn(text) → bool       (defaults to telegram_alerter.send_message)
    - poll_fn(token, offset)     (defaults to poll_once)
    - max_iterations             (None = run forever; test passes a number)
    """
    sleep_fn = sleep_fn or time.sleep
    send_fn = send_fn or ta.send_message
    poll_fn = poll_fn or (lambda tok, off: poll_once(tok, offset=off))

    creds = ta._resolve_creds()
    if creds is None:
        log("telegram_listener: not configured — exiting", "INFO")
        return
    chat_id = str(creds["chat_id"])
    bot_token = creds["bot_token"]
    offset = load_offset(offset_path)
    log(f"telegram_listener: started, offset={offset}, chat_id={chat_id}", "SUCCESS")

    iterations = 0
    while True:
        if max_iterations is not None and iterations >= max_iterations:
            return
        iterations += 1
        try:
            updates = poll_fn(bot_token, offset)
        except Exception as e:
            log(f"telegram_listener: poll failed: {e}", "WARNING")
            sleep_fn(SLEEP_ON_ERROR_S)
            continue
        for u in updates:
            uid = int(u.get("update_id", 0))
            if uid >= offset:
                offset = uid + 1
            if not is_authorised(u, chat_id):
                log(f"telegram_listener: ignoring update from chat "
                    f"{(u.get('message') or {}).get('chat', {}).get('id')}",
                    "INFO")
                continue
            text = extract_text(u)
            parsed = parse_command(text)
            if parsed is None:
                continue
            reply = dispatch(parsed)
            if reply is None:
                send_fn(f"unknown command: /{parsed['cmd']}\n"
                        f"try: {', '.join('/'+k for k in HANDLERS)}")
                continue
            send_fn(reply)
            log(f"telegram_listener: handled /{parsed['cmd']}", "INFO")
        save_offset(offset, path=offset_path)


def main():
    parser = argparse.ArgumentParser(description="Telegram command listener.")
    parser.add_argument("--once", action="store_true",
                        help="One getUpdates round-trip then exit (smoke test)")
    args = parser.parse_args()
    run_forever(max_iterations=1 if args.once else None)


if __name__ == "__main__":
    main()
