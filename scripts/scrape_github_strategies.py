"""
scrape_github_strategies.py — Search GitHub for trading-strategy repos,
pull README + strategy-shaped source files, run them through Ollama to
extract structured `{strategy_id, title, entry_rules, exit_rules,
risk_management}` payloads, and append UNTESTED records to records.jsonl.

Pipeline:
  1. For each --query, call GitHub's search/repositories endpoint with
     filters: stars >= --min-stars and pushed >= --since-pushed-days ago.
  2. For each repo (capped by --max-repos), fetch the README plus up to
     3 heuristically-named strategy files matching
     `*strategy*.py`, `*.pine`, `strategies/*.py`.
  3. Concatenate the text (truncated for prompt budget) and ask Ollama
     to extract one strategy in the same JSON shape as 2.1.2a.
  4. Dedupe by repo URL across runs; skip repos already in records.jsonl.
  5. Append accepted records.

CLI flags: --query, --min-stars, --max-repos, --since-pushed-days,
           --model, --temperature, --records-path, --dry-run.

Auth: if `credentials.json` has a `github.token`, we send it as a Bearer
header (5000 req/hr). Otherwise we run unauthenticated (60 req/hr — still
enough for --max-repos 20). The script never writes credentials.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402

RECORDS_PATH = (
    ROOT / "data" / "scrapes"
    / "tradingview-in-daytrading-strategies-2026-04-26"
    / "records.jsonl"
)

GITHUB_API = "https://api.github.com"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL_DEFAULT = os.environ.get(
    "OLLAMA_STRATEGY_MODEL",
    os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b"),
)
OLLAMA_TIMEOUT_SEC = int(os.environ.get("OLLAMA_TIMEOUT_SEC", "300"))

USER_AGENT = "profit-generation-strategy-scraper (https://github.com/ProbablyMaybeNo)"

DEFAULT_QUERIES = (
    '"trading strategy"',
    '"algorithmic trading"',
    '"pine script strategy"',
)

# Max bytes per file we pull through the prompt (~16 KB).
MAX_FILE_BYTES = 16_000
# Max files (besides README) we feed through the LLM per repo.
MAX_STRATEGY_FILES_PER_REPO = 3
# Rate-limit floor between GitHub API hits.
MIN_GH_INTERVAL_SEC = 1.0

STRATEGY_FILE_RES = (
    re.compile(r"strategy", re.I),
    re.compile(r"\.pine$", re.I),
    re.compile(r"^strategies/.*\.py$", re.I),
)


REQUIRED_FIELDS = ("strategy_id", "title", "entry_rules", "exit_rules",
                   "risk_management")


PROMPT_TEMPLATE = textwrap.dedent("""\
    You are a quantitative-trading strategist. The text below comes from
    a GitHub repository (README plus selected source files) that may
    describe a trading strategy.

    Read the text. If it describes ONE concrete daily-bar trading
    strategy with codeable rules, output a SINGLE JSON object with these
    keys:

      "strategy_id"      — short kebab-case id (unique, lowercase, <= 50 chars)
      "title"            — human title (<= 80 chars)
      "entry_rules"      — entry rules with concrete numbers, no look-ahead
      "exit_rules"       — exit rules with concrete numbers
      "risk_management"  — stop-loss / sizing / hedging notes

    If the text describes multiple strategies, pick the ONE most clearly
    specified. If it describes no concrete codeable strategy, output the
    string NONE and nothing else.

    Output ONLY the JSON object (start with `{{`) or the literal NONE.
    NO markdown fences. NO commentary.

    REPO: {repo_full_name}
    URL: {repo_url}

    -------- TEXT (truncated) --------
    {body}
    -------- END TEXT --------
    """)


# ---------------------------------------------------------------------------
# rate limiter — same pattern as scrape_tradingview
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, min_interval: float = MIN_GH_INTERVAL_SEC,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.min_interval = min_interval
        self._last: Optional[float] = None
        self._sleep = sleep
        self._now = now

    def wait(self) -> None:
        if self._last is not None:
            elapsed = self._now() - self._last
            if elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
        self._last = self._now()


# ---------------------------------------------------------------------------
# GitHub token loading — optional, never written
# ---------------------------------------------------------------------------

def load_github_token() -> Optional[str]:
    """Return the github token from credentials.json, or None if absent.

    Never raises if credentials.json is missing — public GitHub is fine
    at 60 req/hr.
    """
    try:
        from config.utils import load_credentials  # local import to avoid cycle
        creds = load_credentials()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    section = creds.get("github") if isinstance(creds, dict) else None
    if isinstance(section, dict):
        tok = section.get("token")
        if isinstance(tok, str) and tok.strip():
            return tok.strip()
    return None


def _gh_headers(token: Optional[str]) -> Dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ---------------------------------------------------------------------------
# HTTP — indirection seam so tests inject fakes
# ---------------------------------------------------------------------------

def _http_get_json(url: str, *, params: Optional[Dict] = None,
                   headers: Optional[Dict] = None,
                   timeout: float = 30.0) -> Dict:
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _http_get_text(url: str, *, headers: Optional[Dict] = None,
                   timeout: float = 30.0) -> str:
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# GitHub fetchers
# ---------------------------------------------------------------------------

def _build_search_q(query: str, *, min_stars: int,
                    since_pushed: date) -> str:
    return f"{query} stars:>={min_stars} pushed:>={since_pushed.isoformat()}"


def search_repos(query: str, *, min_stars: int, since_pushed: date,
                 token: Optional[str],
                 max_results: int,
                 http_get_json: Callable[..., Dict] = _http_get_json,
                 rate_limiter: Optional[RateLimiter] = None) -> List[Dict]:
    """Run a GitHub repo search. Returns at most `max_results` repo dicts."""
    rl = rate_limiter or RateLimiter()
    headers = _gh_headers(token)
    q = _build_search_q(query, min_stars=min_stars, since_pushed=since_pushed)
    per_page = min(max(max_results, 1), 100)
    rl.wait()
    payload = http_get_json(
        f"{GITHUB_API}/search/repositories",
        params={"q": q, "sort": "stars", "order": "desc",
                "per_page": per_page},
        headers=headers,
    )
    items = payload.get("items") or []
    return items[:max_results]


def list_repo_files(full_name: str, *, token: Optional[str],
                    http_get_json: Callable[..., Dict] = _http_get_json,
                    rate_limiter: Optional[RateLimiter] = None) -> List[str]:
    """Best-effort recursive file listing via the trees API. Empty on failure."""
    rl = rate_limiter or RateLimiter()
    headers = _gh_headers(token)
    try:
        rl.wait()
        repo_info = http_get_json(f"{GITHUB_API}/repos/{full_name}",
                                  headers=headers)
    except Exception:
        return []
    branch = repo_info.get("default_branch") or "main"
    try:
        rl.wait()
        tree = http_get_json(
            f"{GITHUB_API}/repos/{full_name}/git/trees/{branch}",
            params={"recursive": "1"},
            headers=headers,
        )
    except Exception:
        return []
    paths: List[str] = []
    for node in tree.get("tree") or []:
        if node.get("type") == "blob":
            p = node.get("path")
            if isinstance(p, str):
                paths.append(p)
    return paths


def pick_strategy_files(paths: Iterable[str], *,
                        max_files: int = MAX_STRATEGY_FILES_PER_REPO) -> List[str]:
    """Filter paths to strategy-shaped files. Stable ordering."""
    keep: List[str] = []
    for p in paths:
        if any(rx.search(p) for rx in STRATEGY_FILE_RES):
            if p not in keep:
                keep.append(p)
        if len(keep) >= max_files:
            break
    return keep


def fetch_readme(full_name: str, *, token: Optional[str],
                 http_get_text: Callable[..., str] = _http_get_text,
                 rate_limiter: Optional[RateLimiter] = None) -> str:
    """Fetch README as raw text. Empty string on any failure."""
    rl = rate_limiter or RateLimiter()
    headers = dict(_gh_headers(token))
    headers["Accept"] = "application/vnd.github.raw"
    try:
        rl.wait()
        return http_get_text(
            f"{GITHUB_API}/repos/{full_name}/readme",
            headers=headers,
        )
    except Exception:
        return ""


def fetch_raw_file(full_name: str, path: str, *, branch: str,
                   token: Optional[str],
                   http_get_text: Callable[..., str] = _http_get_text,
                   rate_limiter: Optional[RateLimiter] = None) -> str:
    """Pull a file via raw.githubusercontent.com. Empty on failure."""
    rl = rate_limiter or RateLimiter()
    url = f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        rl.wait()
        return http_get_text(url, headers=headers)
    except Exception:
        return ""


def assemble_text(readme: str, files: Dict[str, str], *,
                  per_file_limit: int = MAX_FILE_BYTES) -> str:
    """Concatenate README + selected files, truncating each."""
    parts: List[str] = []
    if readme:
        parts.append("# README\n\n" + readme[:per_file_limit])
    for path, body in files.items():
        if not body:
            continue
        parts.append(f"\n\n# FILE: {path}\n\n{body[:per_file_limit]}")
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Ollama plumbing — same seam pattern as llm_strategy_generator
# ---------------------------------------------------------------------------

def _ollama_post(url: str, payload: Dict, timeout: float):
    return requests.post(url, json=payload, timeout=timeout)


def call_ollama(prompt: str, *, model: Optional[str] = None,
                temperature: float = 0.3) -> str:
    url = f"{OLLAMA_URL}/api/generate"
    payload = {
        "model": model or OLLAMA_MODEL_DEFAULT,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": 1200,
        },
    }
    resp = _ollama_post(url, payload, OLLAMA_TIMEOUT_SEC)
    if resp.status_code != 200:
        raise RuntimeError(f"ollama {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    text = body.get("response") or ""
    if not text.strip():
        raise RuntimeError("ollama returned empty response")
    return text


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _strip_fences(raw: str) -> str:
    m = _FENCE_RE.search(raw)
    if m:
        return m.group(1).strip()
    return raw.strip()


def parse_extraction(raw: str) -> Optional[Dict]:
    """Return the extracted strategy dict, or None for NONE/malformed."""
    cleaned = _strip_fences(raw)
    if cleaned.strip().upper().startswith("NONE"):
        return None
    start = cleaned.find("{")
    if start < 0:
        return None
    dec = json.JSONDecoder()
    try:
        obj, _ = dec.raw_decode(cleaned[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if not _is_valid_item(obj):
        return None
    return obj


def _is_valid_item(item: Dict) -> bool:
    for f in REQUIRED_FIELDS:
        val = item.get(f)
        if not isinstance(val, str) or not val.strip():
            return False
    sid = item["strategy_id"].strip()
    if len(sid) > 80 or not re.match(r"^[a-z0-9][a-z0-9_\-]*$", sid):
        return False
    return True


def build_prompt(repo_full_name: str, repo_url: str, body: str) -> str:
    return PROMPT_TEMPLATE.format(
        repo_full_name=repo_full_name,
        repo_url=repo_url,
        body=body[:40_000],
    )


# ---------------------------------------------------------------------------
# records.jsonl I/O
# ---------------------------------------------------------------------------

def load_existing_source_urls(records_path: Path) -> set[str]:
    if not records_path.exists():
        return set()
    urls: set[str] = set()
    with records_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = rec.get("url") or ""
            if u:
                urls.add(u)
    return urls


def append_record(records_path: Path, record: Dict) -> None:
    records_path.parent.mkdir(parents=True, exist_ok=True)
    with records_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_record(item: Dict, *, repo: Dict, model: str) -> Dict:
    today = date.today().isoformat()
    strategy_id = f"gh-{item['strategy_id'].strip().lower()}"
    title = item["title"].strip()
    entry = item["entry_rules"].strip()
    exit_ = item["exit_rules"].strip()
    risk = item["risk_management"].strip()
    repo_url = repo.get("html_url") or ""
    full_name = repo.get("full_name") or ""

    description = f"Entry: {entry} | Exit: {exit_}"

    extra: Dict = {
        "agent_summary": (
            f"GitHub repo {full_name}: extracted strategy `{title}`. "
            f"{description[:300]}"
        ),
        "description_full_readable": (
            f"Repo: {full_name}\nEntry: {entry}\nExit: {exit_}\nRisk: {risk}"
        ),
        "strategy_id": strategy_id,
        "methodology_family": "github-extracted",
        "instruments": [],
        "timeframes": {"execution": "1d"},
        "core_concepts": [],
        "entry_rules": entry,
        "exit_rules": exit_,
        "risk_management": risk,
        "tested": False,
        "test_runs": [],
        "current_verdict": "UNTESTED",
        "verdict_summary": "GitHub-extracted candidate, not yet validated",
        "failure_modes": [],
        "improvement_hypotheses": [],
        "code_paths": {},
        "data_artifacts": [],
        "first_logged_iso": today,
        "last_updated_iso": today,
        "scraper": "scrape_github_strategies",
        "llm_model": model,
        "github_repo": full_name,
        "github_stars": repo.get("stargazers_count"),
        "github_pushed_at": repo.get("pushed_at"),
    }

    return {
        "url": repo_url or f"github://{full_name}",
        "title": title,
        "author": (repo.get("owner") or {}).get("login") or "unknown",
        "description": description[:500],
        "source": "github.com",
        "date_scraped": today,
        "tags": ["UNTESTED", "github", "llm-extracted"],
        "extra": extra,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape(
    *,
    queries: Optional[Iterable[str]] = None,
    min_stars: int = 30,
    max_repos: int = 20,
    since_pushed_days: int = 365,
    model: Optional[str] = None,
    temperature: float = 0.3,
    token: Optional[str] = None,
    records_path: Path = RECORDS_PATH,
    dry_run: bool = False,
    today: Optional[date] = None,
    rate_limiter: Optional[RateLimiter] = None,
    repo_searcher: Optional[Callable[..., List[Dict]]] = None,
    file_lister: Optional[Callable[..., List[str]]] = None,
    readme_fetcher: Optional[Callable[..., str]] = None,
    file_fetcher: Optional[Callable[..., str]] = None,
    ollama_caller: Optional[Callable[[str], str]] = None,
) -> Dict:
    """
    Scrape GitHub for trading-strategy repos, run extractions through the
    LLM, write UNTESTED records.

    Returns: {queries, scanned, new, skipped, malformed, records}
    """
    if max_repos <= 0:
        raise ValueError("max_repos must be positive")
    queries = list(queries) if queries is not None else list(DEFAULT_QUERIES)
    used_model = model or OLLAMA_MODEL_DEFAULT
    today = today or date.today()
    since = today - timedelta(days=since_pushed_days)

    rl = rate_limiter or RateLimiter()
    search_fn = repo_searcher or (lambda **kw: search_repos(
        **kw, rate_limiter=rl,
    ))
    list_files_fn = file_lister or (lambda full_name, **kw: list_repo_files(
        full_name, token=token, rate_limiter=rl, **kw,
    ))
    readme_fn = readme_fetcher or (lambda full_name, **kw: fetch_readme(
        full_name, token=token, rate_limiter=rl, **kw,
    ))
    file_fn = file_fetcher or (lambda full_name, path, branch: fetch_raw_file(
        full_name, path, branch=branch, token=token, rate_limiter=rl,
    ))
    llm_fn = ollama_caller or (lambda prompt: call_ollama(
        prompt, model=used_model, temperature=temperature,
    ))

    existing_urls = load_existing_source_urls(records_path)
    seen_full_names: set[str] = set()

    scanned = 0
    skipped = 0
    malformed = 0
    accepted: List[Dict] = []

    for q in queries:
        if len(accepted) >= max_repos:
            break
        remaining = max_repos - len(accepted)
        try:
            repos = search_fn(
                query=q, min_stars=min_stars, since_pushed=since,
                token=token, max_results=remaining,
            )
        except Exception as e:
            log(f"github search {q!r} failed: {e}", "WARNING")
            continue

        for repo in repos:
            scanned += 1
            full_name = repo.get("full_name") or ""
            html_url = repo.get("html_url") or ""
            branch = repo.get("default_branch") or "main"

            if not full_name or not html_url:
                malformed += 1
                continue
            if full_name in seen_full_names or html_url in existing_urls:
                skipped += 1
                continue
            seen_full_names.add(full_name)

            try:
                readme = readme_fn(full_name)
            except Exception as e:
                log(f"readme fetch failed {full_name}: {e}", "WARNING")
                readme = ""
            try:
                paths = list_files_fn(full_name)
            except Exception as e:
                log(f"tree fetch failed {full_name}: {e}", "WARNING")
                paths = []
            picked = pick_strategy_files(paths)
            files: Dict[str, str] = {}
            for p in picked:
                try:
                    files[p] = file_fn(full_name, p, branch)
                except Exception as e:
                    log(f"file fetch failed {full_name}:{p}: {e}", "WARNING")
                    files[p] = ""

            body = assemble_text(readme, files)
            if not body.strip():
                malformed += 1
                continue

            prompt = build_prompt(full_name, html_url, body)
            try:
                raw = llm_fn(prompt)
            except Exception as e:
                log(f"llm call failed {full_name}: {e}", "WARNING")
                malformed += 1
                continue

            extracted = parse_extraction(raw)
            if extracted is None:
                malformed += 1
                continue

            record = build_record(extracted, repo=repo, model=used_model)
            if not dry_run:
                append_record(records_path, record)
            accepted.append(record)
            existing_urls.add(html_url)
            if len(accepted) >= max_repos:
                break

    return {
        "queries": list(queries),
        "scanned": scanned,
        "new": len(accepted),
        "skipped": skipped,
        "malformed": malformed,
        "records": accepted,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", action="append", default=None,
                        help="repeatable; default uses 3 built-in queries")
    parser.add_argument("--min-stars", type=int, default=30)
    parser.add_argument("--max-repos", type=int, default=20)
    parser.add_argument("--since-pushed-days", type=int, default=365)
    parser.add_argument("--model", default=None,
                        help=f"override OLLAMA model (default {OLLAMA_MODEL_DEFAULT})")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--records-path", default=str(RECORDS_PATH))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-auth", action="store_true",
                        help="ignore github token in credentials.json")
    args = parser.parse_args()

    token = None if args.no_auth else load_github_token()
    auth_mode = "authenticated" if token else "unauthenticated (60 req/hr)"

    log(
        f"scrape_github_strategies start: queries={args.query or list(DEFAULT_QUERIES)} "
        f"max_repos={args.max_repos} min_stars={args.min_stars} "
        f"mode={auth_mode}",
        "INFO",
    )

    summary = scrape(
        queries=args.query,
        min_stars=args.min_stars,
        max_repos=args.max_repos,
        since_pushed_days=args.since_pushed_days,
        model=args.model,
        temperature=args.temperature,
        token=token,
        records_path=Path(args.records_path),
        dry_run=args.dry_run,
    )

    log(
        f"done: scanned={summary['scanned']} new={summary['new']} "
        f"skipped={summary['skipped']} malformed={summary['malformed']}",
        "SUCCESS" if summary["new"] > 0 else "WARNING",
    )
    return 0 if summary["new"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
