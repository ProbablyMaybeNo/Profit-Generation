"""
setup_live_credentials.py — Interactive wizard to onboard Alpaca LIVE
trading credentials into config/credentials.json.

Flow:
  1. Read existing credentials.json (or refuse if missing)
  2. If `alpaca_live` already exists and is populated, refuse unless
     --force is passed. Idempotency: re-running with the same keys is
     a no-op (no write, no Notion post).
  3. Prompt for API key + secret (or read via --api-key / --secret-key
     CLI args for non-interactive runs)
  4. Validate against the live Alpaca API: TradingClient must return an
     account with status=ACTIVE and not blocked
  5. Write to credentials.json under `alpaca_live` (paper=false,
     base_url=https://api.alpaca.markets)
  6. Post a confirmation page to the daily-reports Notion DB tagged
     "Live-Credentials"
  7. Print explicit next-step instructions (preflight + first add to
     auto_trade.live_strategies)

The wizard NEVER prints API keys or secrets back to stdout — keys are
echoed via getpass / partially-masked.

Usage:
  py -3.13 scripts/setup_live_credentials.py
  py -3.13 scripts/setup_live_credentials.py --dry-run    # validate but don't write
  py -3.13 scripts/setup_live_credentials.py --force       # overwrite existing
  py -3.13 scripts/setup_live_credentials.py --api-key X --secret-key Y --no-notion
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402


LIVE_BASE_URL = "https://api.alpaca.markets"
LIVE_SECTION = "alpaca_live"
PLACEHOLDER_MARKERS = ("PASTE_", "YOUR_", "secret_PASTE")


class WizardError(RuntimeError):
    pass


def _is_placeholder(value: str) -> bool:
    if not value:
        return True
    return any(m in value for m in PLACEHOLDER_MARKERS)


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}...{value[-3:]}"


def _read_credentials(path: Path) -> Dict:
    if not path.exists():
        raise WizardError(
            f"credentials.json not found at {path}. "
            f"Copy config/credentials.example.json first."
        )
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise WizardError(f"credentials.json is not valid JSON: {e}") from e


def _live_section_populated(creds: Dict) -> bool:
    section = creds.get(LIVE_SECTION)
    if not isinstance(section, dict) or not section:
        return False
    api_key = section.get("api_key") or ""
    secret_key = section.get("secret_key") or ""
    if not api_key or not secret_key:
        return False
    if _is_placeholder(api_key) or _is_placeholder(secret_key):
        return False
    return True


def validate_live_keys(
    api_key: str,
    secret_key: str,
    *,
    validator_fn: Optional[Callable] = None,
) -> Dict:
    """Hit the live Alpaca API and return the account summary.

    validator_fn is the indirection seam for tests. By default it builds
    a real `alpaca.trading.client.TradingClient(paper=False)` and calls
    get_account(). Raises WizardError on any failure.
    """
    if not api_key or _is_placeholder(api_key):
        raise WizardError("api_key is empty or a placeholder")
    if not secret_key or _is_placeholder(secret_key):
        raise WizardError("secret_key is empty or a placeholder")

    if validator_fn is None:
        def _default_validator(k: str, s: str) -> Dict:
            from alpaca.trading.client import TradingClient
            client = TradingClient(api_key=k, secret_key=s, paper=False)
            acct = client.get_account()
            return {
                "status": str(getattr(acct, "status", "")).upper(),
                "blocked": bool(
                    getattr(acct, "trading_blocked", False)
                    or getattr(acct, "account_blocked", False)
                ),
                "account_number": str(getattr(acct, "account_number", "")),
                "currency": str(getattr(acct, "currency", "")),
            }
        validator_fn = _default_validator

    try:
        summary = validator_fn(api_key, secret_key)
    except Exception as e:
        raise WizardError(
            f"Alpaca LIVE validation failed: {e}. "
            f"Verify the keys are LIVE (not paper) and that your account "
            f"is approved for live trading."
        ) from e

    status = summary.get("status", "")
    if status != "ACTIVE":
        raise WizardError(
            f"Alpaca account status={status!r} (expected ACTIVE). "
            f"Refusing to record live keys against a non-ACTIVE account."
        )
    if summary.get("blocked"):
        raise WizardError(
            "Alpaca account is blocked (trading_blocked or "
            "account_blocked). Refusing to record live keys."
        )
    return summary


def write_live_section(
    creds: Dict,
    *,
    api_key: str,
    secret_key: str,
) -> Dict:
    """Return an UPDATED credentials dict with alpaca_live populated.

    Pure — does not write to disk. Caller is responsible for atomic
    persistence.
    """
    new = dict(creds)
    new[LIVE_SECTION] = {
        "api_key": api_key,
        "secret_key": secret_key,
        "paper": False,
        "base_url": LIVE_BASE_URL,
    }
    return new


def save_credentials(path: Path, creds: Dict) -> None:
    """Atomic write — temp file + rename. Same pattern as save_state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(creds, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def post_confirmation_to_notion(
    *,
    account_number: str,
    currency: str,
    api_key_masked: str,
    database_id: Optional[str] = None,
) -> Dict:
    """Post a confirmation page so the live-keys event is auditable."""
    from monitoring import notion_writer
    from monitoring.config import NOTION_DAILY_REPORTS_DB_ID

    db_id = database_id or NOTION_DAILY_REPORTS_DB_ID
    today = datetime.now(timezone.utc).date().isoformat()
    title = f"Alpaca LIVE credentials onboarded — {today}"

    markdown = "\n".join([
        f"# {title}",
        "",
        f"**Account:** `{account_number}` ({currency})",
        f"**API key:** `{api_key_masked}`",
        f"**Base URL:** `{LIVE_BASE_URL}`",
        "",
        "_Next: add at least one strategy_id to "
        "`auto_trade.live_strategies` in `config/settings.json`, then "
        "run `py -3.13 scripts/preflight.py` before the next market open._",
    ])
    properties = {
        "Report": {"title": [{"text": {"content": title}}]},
        "Date": {"date": {"start": today}},
        "Importance": {"number": 4},
        "Has Notable Pattern": {"checkbox": True},
        "Watchlist Count": {"number": 0},
        "Strategy Fires": {"number": 0},
        "Symbols Watched": {"multi_select": []},
        "Tags": {"multi_select": [{"name": "Live-Credentials"}]},
        "Status": {"select": {"name": "Generated"}},
        "Source": {"select": {"name": "setup_live_credentials"}},
    }
    body = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji", "emoji": "\U0001f510"},
        "properties": properties,
        "children": notion_writer._markdown_to_blocks(markdown)[:100],
    }
    import requests
    r = requests.post(
        f"{notion_writer.NOTION_API}/pages",
        headers=notion_writer._headers(),
        json=body, timeout=30,
    )
    if r.status_code >= 400:
        raise WizardError(f"Notion API {r.status_code}: {r.text[:500]}")
    return r.json()


def run_wizard(
    *,
    credentials_path: Path,
    api_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
    no_notion: bool = False,
    validator_fn: Optional[Callable] = None,
    prompt_fn: Optional[Callable] = None,
    confirm_fn: Optional[Callable] = None,
    notion_fn: Optional[Callable] = None,
) -> Dict:
    """End-to-end wizard. Returns a dict describing the action taken:

      {action, account_number, api_key_masked, wrote, notion_page_id?}

    action ∈ {"installed", "noop_dry_run", "noop_already_present"}
    """
    creds = _read_credentials(credentials_path)

    if _live_section_populated(creds) and not force:
        raise WizardError(
            f"credentials.json already contains a populated "
            f"`{LIVE_SECTION}` section. Re-run with --force to overwrite, "
            f"or remove the section manually first."
        )

    # Interactive fallbacks — tests pass api_key/secret_key directly.
    if api_key is None:
        prompt_fn = prompt_fn or input
        api_key = prompt_fn("Alpaca LIVE api_key: ").strip()
    if secret_key is None:
        confirm_fn = confirm_fn or (
            lambda prompt: getpass.getpass(prompt).strip()
        )
        secret_key = confirm_fn("Alpaca LIVE secret_key: ").strip()

    summary = validate_live_keys(
        api_key, secret_key, validator_fn=validator_fn,
    )
    api_key_masked = _mask(api_key)

    if dry_run:
        return {
            "action": "noop_dry_run",
            "account_number": summary["account_number"],
            "currency": summary["currency"],
            "api_key_masked": api_key_masked,
            "wrote": False,
        }

    new_creds = write_live_section(
        creds, api_key=api_key, secret_key=secret_key,
    )
    save_credentials(credentials_path, new_creds)

    result: Dict = {
        "action": "installed",
        "account_number": summary["account_number"],
        "currency": summary["currency"],
        "api_key_masked": api_key_masked,
        "wrote": True,
    }

    if not no_notion:
        try:
            poster = notion_fn or post_confirmation_to_notion
            resp = poster(
                account_number=summary["account_number"],
                currency=summary["currency"],
                api_key_masked=api_key_masked,
            )
            result["notion_page_id"] = resp.get("id")
        except Exception as e:
            # Notion is non-fatal — the keys are already written.
            log(f"Notion confirmation post failed: {e}", "WARNING")

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", help="Alpaca LIVE api_key (else prompted)")
    parser.add_argument("--secret-key",
                        help="Alpaca LIVE secret_key (else prompted via getpass)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite existing alpaca_live section")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate keys against live API but don't write")
    parser.add_argument("--no-notion", action="store_true",
                        help="skip the Notion confirmation post")
    args = parser.parse_args()

    creds_path = ROOT / "config" / "credentials.json"
    try:
        result = run_wizard(
            credentials_path=creds_path,
            api_key=args.api_key,
            secret_key=args.secret_key,
            force=args.force,
            dry_run=args.dry_run,
            no_notion=args.no_notion,
        )
    except WizardError as e:
        log(str(e), "ERROR")
        sys.exit(2)

    if result["action"] == "noop_dry_run":
        log(
            f"DRY RUN: keys validated against live Alpaca "
            f"(account={result['account_number']}, "
            f"key={result['api_key_masked']}). "
            f"Nothing written.",
            "SUCCESS",
        )
    else:
        log(
            f"Installed alpaca_live section "
            f"(account={result['account_number']}, "
            f"key={result['api_key_masked']}). "
            f"Next: add a strategy_id to auto_trade.live_strategies, then "
            f"run `py -3.13 scripts/preflight.py` before the next open.",
            "SUCCESS",
        )


if __name__ == "__main__":
    main()
