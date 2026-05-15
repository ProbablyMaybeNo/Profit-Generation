"""
scrape_quantpedia.py — Pull strategy ideas from Quantpedia's curated
free strategy index.

Quantpedia publishes a paginated list of trading strategies at
https://quantpedia.com/screener/ — each links to a strategy detail
page with a structured description. Most fields needed for our schema
are present there (entry / exit / risk).

Pipeline:
  1. Fetch listing pages of the screener (paginated via ?paged=N).
  2. Extract strategy detail URLs (/strategies/<slug>/).
  3. For each detail URL, fetch HTML and extract its main article text.
  4. Run the article body through Ollama with a single-strategy
     extraction prompt (same shape as scrape_quantocracy.py).
  5. Dedupe by source URL against records.jsonl. Append UNTESTED records.

Acceptance: ≥ 5 strategies per run.

CLI:
  py -3.13 scripts/scrape_quantpedia.py
  py -3.13 scripts/scrape_quantpedia.py --pages 3 --max-strategies 15 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import time
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402

RECORDS_PATH = (
    ROOT / "data" / "scrapes"
    / "tradingview-in-daytrading-strategies-2026-04-26"
    / "records.jsonl"
)

LISTING_BASE = "https://quantpedia.com/screener/"
DETAIL_HOST = "https://quantpedia.com"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL_DEFAULT = os.environ.get(
    "OLLAMA_STRATEGY_MODEL",
    os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b"),
)
OLLAMA_TIMEOUT_SEC = int(os.environ.get("OLLAMA_TIMEOUT_SEC", "300"))

USER_AGENT = "profit-generation-quantpedia-scraper"
MIN_REQ_INTERVAL_SEC = 1.0
MAX_BODY_BYTES = 24_000

REQUIRED_FIELDS = ("strategy_id", "title", "entry_rules", "exit_rules",
                   "risk_management")


PROMPT_TEMPLATE = textwrap.dedent("""\
    You are a quantitative-trading strategist. The text below is the
    full description of ONE published trading strategy (from
    Quantpedia). Output a SINGLE JSON object with these keys:

      "strategy_id"     — short kebab-case id (unique, lowercase, <= 50 chars)
      "title"           — human title (<= 80 chars)
      "entry_rules"     — entry rules with concrete numbers, no look-ahead
      "exit_rules"      — exit rules with concrete numbers
      "risk_management" — stop-loss / sizing / hedging notes

    Distill multi-paragraph prose into precise codeable rules. If the
    timeframe is monthly or weekly, ADAPT the rules to daily bars (use
    the equivalent rolling windows in trading days, e.g. 20 days ≈ 1
    month). If you cannot find concrete codeable rules, output the
    literal NONE.

    Output ONLY the JSON object (starting with `{{`) or NONE. NO
    markdown fences. NO commentary.

    SOURCE: {source_url}
    TITLE: {title}

    -------- BODY --------
    {body}
    -------- END --------
    """)


# ---------------------------------------------------------------------------
# rate limiter (shared shape with sister scrapers)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, min_interval: float = MIN_REQ_INTERVAL_SEC,
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
# HTTP — indirection seam
# ---------------------------------------------------------------------------

def _http_get_text(url: str, *, timeout: float = 30.0) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                        timeout=timeout)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

_DETAIL_HREF_RE = re.compile(r"^/strategies/[A-Za-z0-9][A-Za-z0-9_\-/]+/?$")


def normalize_detail_url(href: str) -> Optional[str]:
    if not href:
        return None
    href = href.split("#")[0].split("?")[0]
    if href.startswith("http"):
        if "quantpedia.com" not in href:
            return None
        path = re.sub(r"^https?://[^/]+", "", href)
    else:
        path = href
    if not path.startswith("/strategies/"):
        return None
    path = path.rstrip("/") + "/"
    if not _DETAIL_HREF_RE.match(path.rstrip("/")):
        return None
    return DETAIL_HOST + path


def parse_listing(html: str) -> List[str]:
    """Extract unique strategy detail URLs from a listing page."""
    soup = BeautifulSoup(html, "html.parser")
    urls: List[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        u = normalize_detail_url(a.get("href"))
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def extract_article_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    container = (soup.find("article") or soup.find("main")
                 or soup.body or soup)
    text = container.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:MAX_BODY_BYTES]


def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return ""


# ---------------------------------------------------------------------------
# Ollama plumbing
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
        "options": {"temperature": temperature, "num_predict": 1200},
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


def build_prompt(*, source_url: str, title: str, body: str) -> str:
    return PROMPT_TEMPLATE.format(
        source_url=source_url,
        title=title or "(untitled)",
        body=body[:MAX_BODY_BYTES],
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


def build_record(item: Dict, *, source_url: str, page_title: str,
                 model: str) -> Dict:
    today = date.today().isoformat()
    strategy_id = f"qp-{item['strategy_id'].strip().lower()}"
    title = item["title"].strip() or page_title
    entry = item["entry_rules"].strip()
    exit_ = item["exit_rules"].strip()
    risk = item["risk_management"].strip()
    description = f"Entry: {entry} | Exit: {exit_}"

    extra: Dict = {
        "agent_summary": (
            f"Quantpedia strategy `{title}` extracted from `{page_title}`. "
            f"{description[:300]}"
        ),
        "description_full_readable": (
            f"Source: {source_url}\nEntry: {entry}\nExit: {exit_}\nRisk: {risk}"
        ),
        "strategy_id": strategy_id,
        "methodology_family": "quantpedia-extracted",
        "instruments": [],
        "timeframes": {"execution": "1d"},
        "core_concepts": [],
        "entry_rules": entry,
        "exit_rules": exit_,
        "risk_management": risk,
        "tested": False,
        "test_runs": [],
        "current_verdict": "UNTESTED",
        "verdict_summary": "Quantpedia-extracted candidate, not yet validated",
        "failure_modes": [],
        "improvement_hypotheses": [],
        "code_paths": {},
        "data_artifacts": [],
        "first_logged_iso": today,
        "last_updated_iso": today,
        "scraper": "scrape_quantpedia",
        "llm_model": model,
        "page_title": page_title,
    }

    return {
        "url": source_url,
        "title": title,
        "author": "quantpedia.com",
        "description": description[:500],
        "source": "quantpedia.com",
        "date_scraped": today,
        "tags": ["UNTESTED", "quantpedia", "llm-extracted"],
        "extra": extra,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape(
    *,
    pages: int = 2,
    max_strategies: int = 15,
    model: Optional[str] = None,
    temperature: float = 0.3,
    records_path: Path = RECORDS_PATH,
    dry_run: bool = False,
    rate_limiter: Optional[RateLimiter] = None,
    listing_fetcher: Optional[Callable[[int], str]] = None,
    detail_fetcher: Optional[Callable[[str], str]] = None,
    ollama_caller: Optional[Callable[[str], str]] = None,
) -> Dict:
    if max_strategies <= 0:
        raise ValueError("max_strategies must be positive")
    used_model = model or OLLAMA_MODEL_DEFAULT
    rl = rate_limiter or RateLimiter()

    def default_listing(page: int) -> str:
        rl.wait()
        if page <= 1:
            return _http_get_text(LISTING_BASE)
        return _http_get_text(f"{LISTING_BASE}?paged={page}")

    def default_detail(url: str) -> str:
        rl.wait()
        return _http_get_text(url)

    list_fn = listing_fetcher or default_listing
    det_fn = detail_fetcher or default_detail
    llm_fn = ollama_caller or (lambda prompt: call_ollama(
        prompt, model=used_model, temperature=temperature,
    ))

    discovered: List[str] = []
    seen_local: set[str] = set()
    for page in range(1, pages + 1):
        try:
            html = list_fn(page)
        except Exception as e:
            log(f"listing page {page} failed: {e}", "WARNING")
            continue
        for u in parse_listing(html):
            if u in seen_local:
                continue
            seen_local.add(u)
            discovered.append(u)

    existing_urls = load_existing_source_urls(records_path)

    skipped = 0
    malformed = 0
    accepted: List[Dict] = []

    for url in discovered:
        if len(accepted) >= max_strategies:
            break
        if url in existing_urls:
            skipped += 1
            continue
        try:
            html = det_fn(url)
        except Exception as e:
            log(f"detail fetch failed {url}: {e}", "WARNING")
            malformed += 1
            continue
        body = extract_article_text(html)
        page_title = extract_title(html)
        if not body.strip():
            malformed += 1
            continue
        prompt = build_prompt(source_url=url, title=page_title, body=body)
        try:
            raw = llm_fn(prompt)
        except Exception as e:
            log(f"llm call failed {url}: {e}", "WARNING")
            malformed += 1
            continue
        extracted = parse_extraction(raw)
        if extracted is None:
            malformed += 1
            continue
        rec = build_record(extracted, source_url=url,
                           page_title=page_title, model=used_model)
        if not dry_run:
            append_record(records_path, rec)
        accepted.append(rec)
        existing_urls.add(url)

    return {
        "discovered": len(discovered),
        "new": len(accepted),
        "skipped": skipped,
        "malformed": malformed,
        "records": accepted,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--max-strategies", type=int, default=15)
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--records-path", default=str(RECORDS_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log(
        f"scrape_quantpedia start: pages={args.pages} "
        f"max_strategies={args.max_strategies}",
        "INFO",
    )
    summary = scrape(
        pages=args.pages,
        max_strategies=args.max_strategies,
        model=args.model,
        temperature=args.temperature,
        records_path=Path(args.records_path),
        dry_run=args.dry_run,
    )
    log(
        f"done: discovered={summary['discovered']} new={summary['new']} "
        f"skipped={summary['skipped']} malformed={summary['malformed']}",
        "SUCCESS" if summary["new"] >= 5 else "WARNING",
    )
    return 0 if summary["new"] >= 5 else 1


if __name__ == "__main__":
    sys.exit(main())
