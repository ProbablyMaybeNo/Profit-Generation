"""
scrape_quantocracy.py — Pull strategy ideas from the Quantocracy RSS feed.

Pipeline:
  1. Fetch the RSS feed at https://quantocracy.com/feed/ (configurable).
  2. Parse each <item>: title, link, description, pubDate.
  3. For each linked post, fetch the article HTML and extract its main
     text (BeautifulSoup, strip script/style/nav).
  4. Run the article body through Ollama with a single-strategy
     extraction prompt; parse JSON, drop NONE / malformed.
  5. Dedupe by source URL against records.jsonl. Append UNTESTED records.

Acceptance: ≥ 5 strategies per run (when the feed has ≥ 5 substantive
posts and the LLM finds extractable rules).

CLI:
  py -3.13 scripts/scrape_quantocracy.py
  py -3.13 scripts/scrape_quantocracy.py --max-posts 20 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
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

DEFAULT_RSS_URL = "https://quantocracy.com/feed/"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL_DEFAULT = os.environ.get(
    "OLLAMA_STRATEGY_MODEL",
    os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b"),
)
OLLAMA_TIMEOUT_SEC = int(os.environ.get("OLLAMA_TIMEOUT_SEC", "300"))

USER_AGENT = "profit-generation-quantocracy-scraper"
MIN_REQ_INTERVAL_SEC = 1.0
MAX_BODY_BYTES = 24_000

REQUIRED_FIELDS = ("strategy_id", "title", "entry_rules", "exit_rules",
                   "risk_management")


PROMPT_TEMPLATE = textwrap.dedent("""\
    You are a quantitative-trading strategist. The text below is the
    body of a quant-research blog post. If it describes ONE concrete
    daily-bar trading strategy with codeable rules, output a SINGLE
    JSON object with these keys:

      "strategy_id"     — short kebab-case id (unique, lowercase, <= 50 chars)
      "title"           — human title (<= 80 chars)
      "entry_rules"     — entry rules with concrete numbers, no look-ahead
      "exit_rules"      — exit rules with concrete numbers
      "risk_management" — stop-loss / sizing / hedging notes

    If the post describes multiple strategies, pick the ONE most clearly
    specified. If no concrete codeable strategy is present, output the
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
# rate limiter
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
# HTTP — indirection seams
# ---------------------------------------------------------------------------

def _http_get_text(url: str, *, timeout: float = 30.0) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                        timeout=timeout)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------

def parse_rss(xml_text: str) -> List[Dict]:
    """Return a list of {title, link, description, pub_date} dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: List[Dict] = []
    # RSS 2.0: <rss><channel><item>...
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not link:
            continue
        out.append({"title": title, "link": link,
                    "description": desc, "pub_date": pub})
    return out


# ---------------------------------------------------------------------------
# Article body extraction
# ---------------------------------------------------------------------------

def extract_article_text(html: str) -> str:
    """Return the main readable text of an article page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    # Prefer <article>, fall back to <main>, then body.
    container = soup.find("article") or soup.find("main") or soup.body or soup
    text = container.get_text(separator="\n", strip=True)
    # collapse excess blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:MAX_BODY_BYTES]


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


def build_record(item: Dict, *, source_url: str, post_title: str,
                 model: str) -> Dict:
    today = date.today().isoformat()
    strategy_id = f"qc-{item['strategy_id'].strip().lower()}"
    title = item["title"].strip() or post_title
    entry = item["entry_rules"].strip()
    exit_ = item["exit_rules"].strip()
    risk = item["risk_management"].strip()
    description = f"Entry: {entry} | Exit: {exit_}"

    extra: Dict = {
        "agent_summary": (
            f"Quantocracy post `{post_title}`: extracted strategy `{title}`. "
            f"{description[:300]}"
        ),
        "description_full_readable": (
            f"Post: {post_title}\nEntry: {entry}\nExit: {exit_}\nRisk: {risk}"
        ),
        "strategy_id": strategy_id,
        "methodology_family": "quantocracy-extracted",
        "instruments": [],
        "timeframes": {"execution": "1d"},
        "core_concepts": [],
        "entry_rules": entry,
        "exit_rules": exit_,
        "risk_management": risk,
        "tested": False,
        "test_runs": [],
        "current_verdict": "UNTESTED",
        "verdict_summary": "Quantocracy-extracted candidate, not yet validated",
        "failure_modes": [],
        "improvement_hypotheses": [],
        "code_paths": {},
        "data_artifacts": [],
        "first_logged_iso": today,
        "last_updated_iso": today,
        "scraper": "scrape_quantocracy",
        "llm_model": model,
        "post_title": post_title,
    }

    return {
        "url": source_url,
        "title": title,
        "author": "quantocracy.com",
        "description": description[:500],
        "source": "quantocracy.com",
        "date_scraped": today,
        "tags": ["UNTESTED", "quantocracy", "llm-extracted"],
        "extra": extra,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape(
    *,
    rss_url: str = DEFAULT_RSS_URL,
    max_posts: int = 20,
    model: Optional[str] = None,
    temperature: float = 0.3,
    records_path: Path = RECORDS_PATH,
    dry_run: bool = False,
    rate_limiter: Optional[RateLimiter] = None,
    rss_fetcher: Optional[Callable[[str], str]] = None,
    article_fetcher: Optional[Callable[[str], str]] = None,
    ollama_caller: Optional[Callable[[str], str]] = None,
) -> Dict:
    if max_posts <= 0:
        raise ValueError("max_posts must be positive")
    used_model = model or OLLAMA_MODEL_DEFAULT
    rl = rate_limiter or RateLimiter()
    rss_fn = rss_fetcher or (lambda url: (rl.wait() or _http_get_text(url)))
    art_fn = article_fetcher or (lambda url: (rl.wait() or _http_get_text(url)))
    llm_fn = ollama_caller or (lambda prompt: call_ollama(
        prompt, model=used_model, temperature=temperature,
    ))

    try:
        xml_text = rss_fn(rss_url)
    except Exception as e:
        log(f"RSS fetch failed {rss_url}: {e}", "WARNING")
        return {"posts": 0, "new": 0, "skipped": 0, "malformed": 0,
                "records": []}

    items = parse_rss(xml_text)[:max_posts]
    existing_urls = load_existing_source_urls(records_path)

    skipped = 0
    malformed = 0
    accepted: List[Dict] = []

    for it in items:
        link = it["link"]
        if link in existing_urls:
            skipped += 1
            continue
        try:
            html = art_fn(link)
        except Exception as e:
            log(f"article fetch failed {link}: {e}", "WARNING")
            malformed += 1
            continue
        body = extract_article_text(html)
        if not body.strip():
            malformed += 1
            continue
        prompt = build_prompt(source_url=link, title=it["title"], body=body)
        try:
            raw = llm_fn(prompt)
        except Exception as e:
            log(f"llm call failed {link}: {e}", "WARNING")
            malformed += 1
            continue
        extracted = parse_extraction(raw)
        if extracted is None:
            malformed += 1
            continue
        rec = build_record(
            extracted, source_url=link,
            post_title=it["title"], model=used_model,
        )
        if not dry_run:
            append_record(records_path, rec)
        accepted.append(rec)
        existing_urls.add(link)

    return {
        "posts": len(items),
        "new": len(accepted),
        "skipped": skipped,
        "malformed": malformed,
        "records": accepted,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rss-url", default=DEFAULT_RSS_URL)
    parser.add_argument("--max-posts", type=int, default=20)
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--records-path", default=str(RECORDS_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log(
        f"scrape_quantocracy start: rss={args.rss_url} "
        f"max_posts={args.max_posts}",
        "INFO",
    )
    summary = scrape(
        rss_url=args.rss_url,
        max_posts=args.max_posts,
        model=args.model,
        temperature=args.temperature,
        records_path=Path(args.records_path),
        dry_run=args.dry_run,
    )
    log(
        f"done: posts={summary['posts']} new={summary['new']} "
        f"skipped={summary['skipped']} malformed={summary['malformed']}",
        "SUCCESS" if summary["new"] >= 5 else "WARNING",
    )
    return 0 if summary["new"] >= 5 else 1


if __name__ == "__main__":
    sys.exit(main())
