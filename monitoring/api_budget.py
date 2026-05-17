"""
api_budget.py — Daily USD budget gate for the Claude codegen path
(milestone 4.3.3).

Pattern: every Claude call records its spend in `api_spend` keyed by
(provider, UTC_date). Before each call, codegen_claude consults
`can_spend(provider, daily_cap)` — if today's running spend already
exceeds the cap, the call is refused with a BudgetExhausted error
so the caller can fall back to the Ollama path.

The reset is implicit: querying a new UTC date pulls zero rows, so
spending naturally resumes at midnight UTC.

Config: `config/api_budget.json`. Override with CLI flag or env var
in the caller — this module is purely the engine.

The `api_spend` table:
  provider     TEXT   -- e.g. "anthropic"
  spend_date   TEXT   -- ISO YYYY-MM-DD, UTC
  spend_usd    REAL   -- cumulative for the day
  calls        INTEGER
  updated_at   TEXT
  PRIMARY KEY(provider, spend_date)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402


DEFAULT_PROVIDER = "anthropic"
BUDGET_CONFIG_PATH = ROOT / "config" / "api_budget.json"
DEFAULT_DAILY_CAP_USD = 5.0


class BudgetExhausted(RuntimeError):
    """Raised by the codegen_claude path when the day's USD cap is hit."""

    def __init__(self, *, provider: str, today: str, spent_usd: float,
                 cap_usd: float):
        self.provider = provider
        self.today = today
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd
        super().__init__(
            f"{provider} daily budget exhausted on {today}: "
            f"spent ${spent_usd:.4f} / cap ${cap_usd:.4f}. "
            f"Falling back to Ollama."
        )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_budget_config(path: Optional[Path] = None) -> Dict:
    """Read config/api_budget.json. Returns {} when missing (no gate)."""
    p = Path(path) if path is not None else BUDGET_CONFIG_PATH
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"api_budget.json unparseable ({e}); gate disabled", "WARNING")
        return {}


def daily_cap_usd(
    config: Optional[Dict] = None, *, provider: str = DEFAULT_PROVIDER,
) -> float:
    """Resolve the per-provider daily cap from config. Returns the
    default when config is missing or malformed. Returns 0.0 (= "no
    spending allowed") only when provider section sets enabled=false."""
    cfg = config if config is not None else load_budget_config()
    section = (cfg or {}).get(provider) or {}
    if section.get("enabled") is False:
        return 0.0
    raw = section.get("daily_usd_cap", DEFAULT_DAILY_CAP_USD)
    try:
        return float(raw)
    except (TypeError, ValueError):
        log(f"api_budget.{provider}.daily_usd_cap unparseable; using default",
            "WARNING")
        return DEFAULT_DAILY_CAP_USD


# ---------------------------------------------------------------------------
# Spend tracking
# ---------------------------------------------------------------------------

def _utc_today_iso(*, now_fn: Optional[Callable] = None) -> str:
    now = (now_fn() if now_fn else datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).date().isoformat()


def todays_spend_usd(
    conn: sqlite3.Connection,
    *,
    provider: str = DEFAULT_PROVIDER,
    now_fn: Optional[Callable] = None,
) -> float:
    today = _utc_today_iso(now_fn=now_fn)
    row = conn.execute(
        "SELECT spend_usd FROM api_spend WHERE provider=? AND spend_date=?",
        (provider, today),
    ).fetchone()
    return float(row["spend_usd"]) if row else 0.0


def record_spend(
    conn: sqlite3.Connection,
    spend_usd: float,
    *,
    provider: str = DEFAULT_PROVIDER,
    calls: int = 1,
    now_fn: Optional[Callable] = None,
) -> Dict:
    """UPSERT today's row, adding spend_usd to the running total. Returns
    the new {spend_usd, calls, spend_date} state."""
    if spend_usd < 0:
        raise ValueError(f"spend_usd must be non-negative, got {spend_usd}")
    today = _utc_today_iso(now_fn=now_fn)
    now_iso = (now_fn() if now_fn else datetime.now(timezone.utc)).isoformat(
        timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO api_spend(provider, spend_date, spend_usd, "
            "                       calls, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(provider, spend_date) DO UPDATE SET "
            "  spend_usd = spend_usd + excluded.spend_usd, "
            "  calls     = calls + excluded.calls, "
            "  updated_at = excluded.updated_at",
            (provider, today, float(spend_usd), int(calls), now_iso),
        )
    row = conn.execute(
        "SELECT spend_usd, calls FROM api_spend "
        " WHERE provider=? AND spend_date=?",
        (provider, today),
    ).fetchone()
    return {"spend_date": today,
            "spend_usd": float(row["spend_usd"]),
            "calls": int(row["calls"])}


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def can_spend(
    conn: sqlite3.Connection,
    *,
    provider: str = DEFAULT_PROVIDER,
    cap_usd: Optional[float] = None,
    now_fn: Optional[Callable] = None,
    config: Optional[Dict] = None,
) -> Dict:
    """Pre-call check. Returns:

      {ok: bool, spent_usd, cap_usd, today}

    ok=False ⇒ caller should refuse the API call and surface a
    BudgetExhausted to its own caller.
    """
    if cap_usd is None:
        cap_usd = daily_cap_usd(config, provider=provider)
    spent = todays_spend_usd(conn, provider=provider, now_fn=now_fn)
    today = _utc_today_iso(now_fn=now_fn)
    return {
        "ok": spent < cap_usd,
        "spent_usd": round(spent, 6),
        "cap_usd": round(cap_usd, 6),
        "today": today,
    }


def assert_can_spend(
    conn: sqlite3.Connection,
    *,
    provider: str = DEFAULT_PROVIDER,
    cap_usd: Optional[float] = None,
    now_fn: Optional[Callable] = None,
    config: Optional[Dict] = None,
) -> Dict:
    """Like `can_spend` but raises BudgetExhausted instead of returning
    ok=False. Returns the check dict on pass."""
    check = can_spend(conn, provider=provider, cap_usd=cap_usd,
                       now_fn=now_fn, config=config)
    if not check["ok"]:
        raise BudgetExhausted(
            provider=provider, today=check["today"],
            spent_usd=check["spent_usd"], cap_usd=check["cap_usd"],
        )
    return check


# ---------------------------------------------------------------------------
# Convenience: a callback for codegen_claude.on_usage
# ---------------------------------------------------------------------------

def make_usage_recorder(
    conn: sqlite3.Connection,
    *,
    provider: str = DEFAULT_PROVIDER,
    pricing_fn: Optional[Callable[[Dict], float]] = None,
    now_fn: Optional[Callable] = None,
) -> Callable[[Dict], None]:
    """Returns a callable suitable for codegen_claude.on_usage that
    converts token counts → USD and writes the row to api_spend.

    `pricing_fn(usage_dict) -> float USD`. Defaults to the pricing
    table in scripts.codegen_ab.
    """
    if pricing_fn is None:
        # Late import to avoid a circular path during test collection.
        from scripts import codegen_ab
        pricing_fn = codegen_ab.usage_to_usd

    def _record(usage: Dict) -> None:
        try:
            spend = float(pricing_fn(usage))
        except Exception as e:
            log(f"pricing_fn failed (non-fatal): {e}", "WARNING")
            return
        if spend <= 0:
            return
        record_spend(conn, spend, provider=provider, calls=1, now_fn=now_fn)

    return _record
