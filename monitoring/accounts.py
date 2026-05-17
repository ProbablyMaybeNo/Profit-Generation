"""
accounts.py — Multi-account capital allocation registry.

Schema (config/accounts.json):
{
  "accounts": [
    {
      "id":              "paper-main",
      "type":            "paper" | "live",
      "credentials_section": "alpaca" | "alpaca_live",
      "capital_pct":     100.0,
      "live_strategies": [],     # only honored when type == "live"
      "enabled":         true
    },
    ...
  ]
}

`capital_pct` values across enabled accounts MUST sum to 100 (±1.0 for
rounding). When the file is missing or empty, the system defaults to a
single paper account at 100% so existing setups keep working without
any new file.

This module is intentionally side-effect-free: callers use:
  - load_accounts(path?) → list[dict]
  - validate_accounts(accounts) → (ok, errors)
  - split_notional(notional, accounts) → {account_id: notional}

The auto_trader doesn't iterate accounts in this milestone — that
ships as Phase-4 work. We commit the registry + math now so adding a
second account is a config edit, not a code edit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_FILE = ROOT / "config" / "accounts.json"

DEFAULT_ACCOUNTS = [
    {
        "id":                  "paper-main",
        "type":                "paper",
        "credentials_section": "alpaca",
        "capital_pct":         100.0,
        "live_strategies":     [],
        "enabled":             True,
    },
]

REQUIRED_KEYS = ("id", "type", "credentials_section", "capital_pct")
VALID_TYPES = {"paper", "live"}


def load_accounts(path: Optional[Path] = None) -> List[Dict]:
    """Read accounts.json, return list-of-dict. Missing / unparseable
    file → DEFAULT_ACCOUNTS (single paper account at 100%)."""
    p = Path(path) if path is not None else ACCOUNTS_FILE
    if not p.exists():
        return [dict(a) for a in DEFAULT_ACCOUNTS]
    try:
        with open(p, encoding="utf-8") as f:
            body = json.load(f)
    except Exception:
        return [dict(a) for a in DEFAULT_ACCOUNTS]
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(accounts, list) or not accounts:
        return [dict(a) for a in DEFAULT_ACCOUNTS]
    return [dict(a) for a in accounts if isinstance(a, dict)]


def validate_accounts(accounts: List[Dict]) -> Tuple[bool, List[str]]:
    """Return (ok, errors). Errors are human-readable strings."""
    errors: List[str] = []
    if not accounts:
        errors.append("no accounts configured")
        return False, errors
    seen_ids = set()
    enabled_total = 0.0
    for i, a in enumerate(accounts):
        prefix = f"accounts[{i}]"
        for k in REQUIRED_KEYS:
            if k not in a:
                errors.append(f"{prefix}: missing key '{k}'")
        if "type" in a and a["type"] not in VALID_TYPES:
            errors.append(
                f"{prefix}: type must be one of {sorted(VALID_TYPES)} "
                f"(got {a['type']!r})"
            )
        aid = a.get("id")
        if aid in seen_ids:
            errors.append(f"{prefix}: duplicate id {aid!r}")
        seen_ids.add(aid)
        try:
            pct = float(a.get("capital_pct", 0))
        except (TypeError, ValueError):
            errors.append(f"{prefix}: capital_pct must be numeric")
            pct = 0.0
        if pct < 0 or pct > 100:
            errors.append(f"{prefix}: capital_pct must be in [0, 100]")
        if a.get("enabled", True):
            enabled_total += pct
        if (a.get("type") == "paper"
                and a.get("live_strategies")):
            errors.append(
                f"{prefix}: paper account must not declare live_strategies"
            )
    if abs(enabled_total - 100.0) > 1.0:
        errors.append(
            f"capital_pct across enabled accounts sums to {enabled_total} "
            f"(must be 100 ± 1)"
        )
    return (not errors), errors


def enabled_accounts(accounts: List[Dict]) -> List[Dict]:
    return [a for a in accounts if a.get("enabled", True)]


def split_notional(
    notional: float, accounts: List[Dict],
    *, strategy_id: Optional[str] = None,
) -> Dict[str, float]:
    """Split `notional` across enabled accounts pro-rata by capital_pct.

    When `strategy_id` is provided AND any live account lists it in
    `live_strategies`, the split honors those overrides: that strategy's
    capital flows ONLY to accounts that include it in live_strategies.
    Otherwise it flows pro-rata across all enabled accounts.

    Returns {account_id: notional_share}. Empty when no accounts enabled.
    Rounds each share to 2 decimals; remainder lands on the largest share
    so the sum exactly matches `notional`.
    """
    accs = enabled_accounts(accounts)
    if not accs:
        return {}
    # Filter for strategy-specific live routing.
    if strategy_id is not None:
        matching = [a for a in accs
                     if a.get("type") == "live"
                     and strategy_id in (a.get("live_strategies") or [])]
        if matching:
            accs = matching
    total_pct = sum(float(a.get("capital_pct", 0)) for a in accs)
    if total_pct <= 0:
        return {}
    shares: Dict[str, float] = {}
    rounded: Dict[str, float] = {}
    for a in accs:
        pct = float(a.get("capital_pct", 0))
        share = notional * pct / total_pct
        shares[a["id"]] = share
        rounded[a["id"]] = round(share, 2)
    drift = round(notional - sum(rounded.values()), 2)
    if abs(drift) >= 0.01:
        largest = max(rounded.keys(), key=lambda k: rounded[k])
        rounded[largest] = round(rounded[largest] + drift, 2)
    return rounded
