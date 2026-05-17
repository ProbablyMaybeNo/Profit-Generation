"""
deploy_public.py — Rebuild the public/ static page with the latest
performance numbers and deploy to Vercel (milestone 4.4.3).

Daily schtask `\\TradingSystem\\PublicDeploy` at 23:30 invokes this
through `schedulers/deploy_public.bat`. On success or failure a
Notion alert is posted; on failure a Telegram alert is also fired.

The actual deploy is a `vercel --prod` subprocess. Indirection seam
`runner_fn` is the deploy command — tests inject their own. The
default runner uses `subprocess.run` against the `vercel` CLI.

Refuses to deploy when:
  - VERCEL_TOKEN env var is unset AND .vercel/project.json doesn't exist
    (no Vercel project linked yet — Ross must create one first)
  - The vercel CLI is not on PATH
  - The public/ directory is missing or empty

CLI:
  py -3.13 scripts/deploy_public.py
  py -3.13 scripts/deploy_public.py --dry-run    # validate only
  py -3.13 scripts/deploy_public.py --notion-only # skip vercel, post status
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402


PUBLIC_DIR = ROOT / "public"
VERCEL_LINK_FILE = ROOT / ".vercel" / "project.json"
DEFAULT_VERCEL_BIN = "vercel"
VERCEL_TIMEOUT_SEC = 300


class DeployError(RuntimeError):
    """Raised when a precondition fails OR the deploy returns non-zero."""


def build_deploy_command(
    *,
    vercel_bin: str = DEFAULT_VERCEL_BIN,
    token: Optional[str] = None,
    public_dir: Optional[Path] = None,
    prod: bool = True,
) -> List[str]:
    """Construct the `vercel deploy` argv. Pure — no side effects.

    Layout:
      vercel deploy <public_dir>
        --prod
        --token <token>           (if provided)
        --yes                     (skip interactive prompts)
    """
    public_dir = (public_dir or PUBLIC_DIR).resolve()
    cmd: List[str] = [vercel_bin, "deploy", str(public_dir)]
    if prod:
        cmd.append("--prod")
    if token:
        cmd.extend(["--token", token])
    cmd.append("--yes")
    return cmd


def check_preconditions(
    *,
    public_dir: Optional[Path] = None,
    vercel_bin: str = DEFAULT_VERCEL_BIN,
    token: Optional[str] = None,
    link_file: Optional[Path] = None,
    which_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict:
    """Return a {ok: bool, reason: Optional[str]} dict. Surfaces a
    clear human-readable error without performing any deploy.
    """
    public_dir = public_dir or PUBLIC_DIR
    link_file = link_file if link_file is not None else VERCEL_LINK_FILE
    which = which_fn or shutil.which

    if not public_dir.exists():
        return {"ok": False,
                "reason": f"public/ directory missing at {public_dir}"}
    if not (public_dir / "index.html").exists():
        return {"ok": False,
                "reason": f"{public_dir}/index.html missing — 4.4.2 first"}

    if which(vercel_bin) is None:
        return {"ok": False,
                "reason": f"`{vercel_bin}` not on PATH — install via "
                          "`npm i -g vercel` first"}

    # The "Vercel project must exist first" gate Ross asked for.
    has_token = bool(token or os.environ.get("VERCEL_TOKEN"))
    has_link = link_file.exists()
    if not has_token and not has_link:
        return {"ok": False,
                "reason": (
                    "Vercel project not yet linked. Either run "
                    "`vercel link` in the repo root once, or set the "
                    "VERCEL_TOKEN env var. Ship the page manually "
                    "first; the agent cannot create the Vercel "
                    "project on Ross's behalf."
                )}

    return {"ok": True, "reason": None}


def _default_runner(cmd: List[str], *, timeout: int = VERCEL_TIMEOUT_SEC):
    """Subprocess runner — tests inject their own."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        check=False,
    )


def run_deploy(
    *,
    public_dir: Optional[Path] = None,
    vercel_bin: str = DEFAULT_VERCEL_BIN,
    token: Optional[str] = None,
    prod: bool = True,
    runner_fn: Optional[Callable] = None,
    timeout: int = VERCEL_TIMEOUT_SEC,
) -> Dict:
    """Run the `vercel deploy` subprocess. Returns:

      {ok: bool, deploy_url: Optional[str], cmd: List[str],
       returncode: int, stdout: str, stderr: str}

    `ok=False` either when preconditions fail (no subprocess call) or
    when the subprocess exits non-zero.
    """
    pre = check_preconditions(
        public_dir=public_dir, vercel_bin=vercel_bin, token=token,
    )
    if not pre["ok"]:
        return {
            "ok": False, "deploy_url": None,
            "cmd": [], "returncode": -1,
            "stdout": "", "stderr": pre["reason"] or "",
        }

    cmd = build_deploy_command(
        vercel_bin=vercel_bin, token=token,
        public_dir=public_dir, prod=prod,
    )
    runner = runner_fn or _default_runner
    result = runner(cmd, timeout=timeout)
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    rc = int(result.returncode)
    # Vercel CLI emits the deployed URL on the last stdout line on
    # success.
    deploy_url = ""
    if rc == 0 and stdout:
        last = stdout.splitlines()[-1].strip()
        if last.startswith("https://"):
            deploy_url = last
    return {
        "ok": rc == 0, "deploy_url": deploy_url or None,
        "cmd": cmd, "returncode": rc,
        "stdout": stdout, "stderr": stderr,
    }


# ---------------------------------------------------------------------------
# Alerts (Notion + Telegram on failure)
# ---------------------------------------------------------------------------

def render_notion_markdown(result: Dict) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    if result["ok"]:
        return "\n".join([
            f"# Public deploy — {today}",
            "",
            f"**Status:** \U00002705 success",
            f"**URL:** {result.get('deploy_url') or '(no url returned)'}",
            "",
            "Daily Vercel rebuild from `public/index.html`.",
        ])
    return "\n".join([
        f"# Public deploy — {today}",
        "",
        f"**Status:** \U0000274C failed (rc={result.get('returncode')})",
        "",
        "```",
        (result.get("stderr") or result.get("stdout") or "(no output)")[:1500],
        "```",
    ])


def post_to_notion(
    result: Dict, *,
    database_id: Optional[str] = None,
    poster: Optional[Callable] = None,
) -> Dict:
    """Post deploy outcome to the daily-reports Notion DB tagged
    "Public-Deploy"."""
    today = datetime.now(timezone.utc).date().isoformat()
    title = f"Public deploy {'OK' if result['ok'] else 'FAILED'} — {today}"
    markdown = render_notion_markdown(result)
    if poster is not None:
        return poster(title=title, markdown=markdown, ok=result["ok"])

    from monitoring import notion_writer
    from monitoring.config import NOTION_DAILY_REPORTS_DB_ID
    db_id = database_id or NOTION_DAILY_REPORTS_DB_ID
    properties = {
        "Report": {"title": [{"text": {"content": title}}]},
        "Date": {"date": {"start": today}},
        "Importance": {"number": 2 if result["ok"] else 4},
        "Has Notable Pattern": {"checkbox": not result["ok"]},
        "Watchlist Count": {"number": 0},
        "Strategy Fires": {"number": 0},
        "Symbols Watched": {"multi_select": []},
        "Tags": {"multi_select": [{"name": "Public-Deploy"}]},
        "Status": {"select": {"name": "Generated"}},
        "Source": {"select": {"name": "deploy_public"}},
    }
    body = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji",
                 "emoji": "\U0001f7e2" if result["ok"] else "\U0001f534"},
        "properties": properties,
        "children": notion_writer._markdown_to_blocks(markdown)[:50],
    }
    import requests
    r = requests.post(
        f"{notion_writer.NOTION_API}/pages",
        headers=notion_writer._headers(),
        json=body, timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Notion API {r.status_code}: {r.text[:300]}")
    return r.json()


def send_failure_alert(
    result: Dict, *,
    alert_fn: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Telegram alert on deploy failure. Returns True iff sent."""
    if result["ok"]:
        return False
    if alert_fn is None:
        try:
            from monitoring.telegram_alerter import send_message as alert_fn  # type: ignore[no-redef]
        except Exception:
            return False
    text = (
        "\U0001f534 *Public deploy failed* "
        f"(rc={result.get('returncode')}): "
        f"{(result.get('stderr') or result.get('stdout') or '(no output)')[:300]}"
    )
    try:
        return bool(alert_fn(text))
    except Exception as e:
        log(f"deploy alert send failed: {e}", "WARNING")
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="check preconditions and print the command "
                             "without invoking vercel")
    parser.add_argument("--notion-only", action="store_true",
                        help="skip the deploy subprocess (e.g. for a manual "
                             "Notion-only status post)")
    parser.add_argument("--vercel-bin", default=DEFAULT_VERCEL_BIN)
    parser.add_argument("--no-notion", action="store_true")
    args = parser.parse_args()

    pre = check_preconditions(vercel_bin=args.vercel_bin)
    if not pre["ok"]:
        log(f"deploy aborted: {pre['reason']}", "ERROR")
        sys.exit(2)

    if args.dry_run:
        cmd = build_deploy_command(vercel_bin=args.vercel_bin)
        log(f"DRY RUN: would run: {' '.join(cmd)}", "INFO")
        sys.exit(0)

    if args.notion_only:
        result = {"ok": True, "deploy_url": None,
                  "cmd": [], "returncode": 0,
                  "stdout": "(notion-only manual update)", "stderr": ""}
    else:
        result = run_deploy(vercel_bin=args.vercel_bin)

    if result["ok"]:
        log(f"deploy OK: {result.get('deploy_url') or '(no url)'}",
            "SUCCESS")
    else:
        log(f"deploy FAILED (rc={result['returncode']}): "
            f"{result.get('stderr') or result.get('stdout')}",
            "ERROR")
        send_failure_alert(result)

    if not args.no_notion:
        try:
            post_to_notion(result)
        except Exception as e:
            log(f"Notion deploy-status post skipped: {e}", "WARNING")

    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
