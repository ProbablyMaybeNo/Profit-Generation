"""
telegram_alerter.py — Push intraday fires + EOD summaries to a Telegram thread.

Setup (one-time, ~3 min):
  1. Open @BotFather on Telegram, /newbot, give it a name. Copy the bot token.
  2. Send any message to your new bot (e.g. "hi") to start a chat.
  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates -> find chat.id
  4. Add to config/credentials.json:
       "telegram": {
         "bot_token": "1234567890:AA...",
         "chat_id":   "123456789"
       }

If the credential is absent, every send is a graceful no-op (logs INFO once
per process). The trading pipelines never fail because Telegram is missing.
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import load_credentials, log  # noqa: E402

API_BASE = "https://api.telegram.org"
_warned_once = False


def _resolve_creds() -> Optional[Dict[str, str]]:
    """Return {bot_token, chat_id} or None if not configured."""
    global _warned_once
    try:
        creds = load_credentials()
    except Exception:
        return None
    section = None
    for k in ("telegram", "Telegram", "TELEGRAM"):
        if k in creds:
            section = creds[k]
            break
    if not section:
        if not _warned_once:
            log("telegram: not configured (add 'telegram' to credentials.json); "
                "alerts will only land in console + log file", "INFO")
            _warned_once = True
        return None
    bot_token = section.get("bot_token") or section.get("token")
    chat_id = section.get("chat_id") or section.get("chat")
    if not bot_token or not chat_id or "PASTE_YOUR" in str(bot_token):
        if not _warned_once:
            log("telegram: section present but bot_token/chat_id incomplete", "WARNING")
            _warned_once = True
        return None
    return {"bot_token": str(bot_token), "chat_id": str(chat_id)}


def _http_post(url: str, json_body: Dict, timeout: float = 10.0):
    """Indirection seam for tests."""
    return requests.post(url, json=json_body, timeout=timeout)


def send_message(text: str, *, parse_mode: str = "Markdown",
                 disable_preview: bool = True) -> bool:
    """Send one message. Returns True on success, False on graceful failure."""
    creds = _resolve_creds()
    if creds is None:
        return False
    payload = {
        "chat_id": creds["chat_id"],
        "text": text,
        "disable_web_page_preview": disable_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = _http_post(f"{API_BASE}/bot{creds['bot_token']}/sendMessage", payload)
    except Exception as e:
        log(f"telegram send failed (network): {e}", "WARNING")
        return False
    if r.status_code != 200:
        body = ""
        try:
            body = r.text[:200]
        except Exception:
            pass
        log(f"telegram send failed: {r.status_code} {body}", "WARNING")
        return False
    return True


def escape_markdown(text: str) -> str:
    """Escape Telegram Markdown (V1) special characters in user content.

    PG-015 (3.5.1): strategy IDs with `_`, `*`, `[` etc. used to break the
    Markdown parser and cause Telegram API 400. This helper escapes the
    minimal V1 set (`_*[\\``) — code-block boundaries (`` ` ``) get
    backslash-escaped too so a strategy id containing a backtick can't
    close a fenced span.
    """
    if not text:
        return ""
    out = []
    for ch in str(text):
        if ch in "_*[`\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def send_intraday_alert(*, kind: str, strategy_id: str, symbol: str,
                        close: float) -> bool:
    """One alert per new intraday-projected fire/exit."""
    side = "BUY" if kind == "FIRE" else "SELL"
    emoji = "\U0001f7e2" if kind == "FIRE" else "\U0001f534"
    sid_safe = escape_markdown(strategy_id)
    sym_safe = escape_markdown(symbol)
    text = (
        f"{emoji} *{kind}* — `{sid_safe}` on *{sym_safe}* @ ${close:.2f}\n"
        f"TradingView paper note: `{side} {sym_safe} ~{close:.2f} {sid_safe}`"
    )
    return send_message(text)


def send_daily_summary(report, *, notion_page_id: Optional[str] = None) -> bool:
    """One Telegram line summarising today's report. Includes Notion link if posted."""
    fires = report.fires or []
    fire_summary = "none"
    if fires:
        fired = [f"{f['symbol']} ({f['strategy_id'].replace('botnet101-', '')})"
                 for f in fires[:5]]
        fire_summary = ", ".join(fired)
        if len(fires) > 5:
            fire_summary += f" +{len(fires) - 5} more"
    notion_line = ""
    if notion_page_id:
        page = notion_page_id.replace("-", "")
        notion_line = f"\n[Open in Notion](https://www.notion.so/{page})"
    text = (
        f"\U0001f4ca *Daily Report* — {report.report_date.isoformat()}\n"
        f"Importance *{report.importance}/5* · regime *{report.market_regime}* · "
        f"{len(fires)} fires · {len(report.notable_movers)} movers\n"
        f"Fires: {fire_summary}"
    )
    if report.tags:
        text += f"\nTags: {', '.join(report.tags)}"
    text += notion_line
    return send_message(text)


if __name__ == "__main__":
    creds = _resolve_creds()
    if creds is None:
        print("telegram: NOT configured")
        sys.exit(1)
    ok = send_message("✅ telegram_alerter smoke test from Profit Generation")
    print(f"send: {'OK' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)
