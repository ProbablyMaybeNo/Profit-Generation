"""
scrape_tradingview.py — Scrape public Pine *strategies* from TradingView's
public scripts library and append UNTESTED records to records.jsonl.

Pipeline:
  1. Fetch listing pages of https://www.tradingview.com/scripts/?script_type=strategies
     (paginated via /page-N/).
  2. Extract script detail URLs (/script/<id>-<slug>/).
  3. For each detail URL, fetch the page and parse:
       title, author, og:description, embedded JSON description (TV markup),
       optional Pine source code (when present in the public page payload).
  4. Dedupe vs records.jsonl by source URL.
  5. Append a record per new strategy in the UNTESTED schema codegen_strategy
     expects. Strategy IDs are stable: `tv-<script_key>`.

CLI:
  py -3.13 scripts/scrape_tradingview.py
  py -3.13 scripts/scrape_tradingview.py --pages 3 --min-records 10
  py -3.13 scripts/scrape_tradingview.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.cache import cached  # noqa: E402
from config.utils import log  # noqa: E402

LISTING_BASE = "https://www.tradingview.com/scripts/"
DETAIL_BASE = "https://www.tradingview.com"

RECORDS_PATH = (
    ROOT / "data" / "scrapes"
    / "tradingview-in-daytrading-strategies-2026-04-26"
    / "records.jsonl"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

# Hard floor on request spacing per the milestone (max 1 req/sec).
MIN_REQ_INTERVAL_SEC = 1.0


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Minimum-spacing rate limiter, default 1 req/sec."""

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
# HTTP — wrapped so tests / cache can override
# ---------------------------------------------------------------------------

def _http_get(url: str, *, session: Optional[requests.Session] = None,
              timeout: float = 30.0) -> str:
    """Indirection seam. Tests monkeypatch this."""
    s = session or requests
    resp = s.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


@cached(ttl=6 * 3600, namespace="tradingview.listing")
def fetch_listing_html(page: int = 1) -> str:
    """Fetch listing HTML for a strategies page. Cached for 6h."""
    if page <= 1:
        url = f"{LISTING_BASE}?script_type=strategies"
    else:
        url = f"{LISTING_BASE}page-{page}/?script_type=strategies"
    return _http_get(url)


@cached(ttl=24 * 3600, namespace="tradingview.detail")
def fetch_detail_html(url: str) -> str:
    """Fetch a /script/<id>/ detail page. Cached for 24h."""
    return _http_get(url)


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

_SCRIPT_HREF_RE = re.compile(r"^/script/[A-Za-z0-9][A-Za-z0-9_\-]+/?$")


def normalize_detail_url(href: str) -> Optional[str]:
    """Return canonical https://www.tradingview.com/script/<id>-<slug>/ URL."""
    if not href:
        return None
    href = href.split("#")[0].split("?")[0]
    if href.startswith("http"):
        if "tradingview.com" not in href:
            return None
        path = re.sub(r"^https?://[^/]+", "", href)
    else:
        path = href
    if not path.startswith("/script/"):
        return None
    path = path.rstrip("/") + "/"
    # Basic sanity check: /script/<token>/
    if not _SCRIPT_HREF_RE.match(path.rstrip("/")):
        return None
    return DETAIL_BASE + path


def parse_listing(html: str) -> List[str]:
    """Extract unique detail URLs from a listing page."""
    soup = BeautifulSoup(html, "html.parser")
    urls: List[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        u = normalize_detail_url(a.get("href"))
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def script_key_from_url(url: str) -> str:
    m = re.search(r"/script/([^/]+)/?", url)
    return (m.group(1) if m else url).lower()


_DESC_KEY_RE = re.compile(r'"description"\s*:\s*"')


def _longest_json_string_value(html: str, key_re: re.Pattern[str]) -> Optional[str]:
    """Find the longest JSON-decoded string for any "<key>": "<value>" occurrence."""
    best: Optional[str] = None
    dec = json.JSONDecoder()
    for m in key_re.finditer(html):
        i = html.find('"', m.end() - 1)
        if i < 0:
            continue
        try:
            val, _ = dec.raw_decode(html, i)
        except json.JSONDecodeError:
            continue
        if isinstance(val, str) and len(val) > len(best or ""):
            best = val
    return best


def _tv_markup_to_text(text: str) -> str:
    if not text:
        return ""
    t = text
    t = re.sub(r"\[b\](.*?)\[/b\]", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"\[i\](.*?)\[/i\]", r"\1", t, flags=re.DOTALL)
    t = t.replace("[list]", "").replace("[/list]", "")
    t = re.sub(r"\[\*\]", "\n- ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


# Look for Pine source code blocks. TradingView ships them inside a JSON
# string under keys like "source" / "scriptSource" in the page payload.
_PINE_KEY_RES = [
    re.compile(r'"source"\s*:\s*"'),
    re.compile(r'"scriptSource"\s*:\s*"'),
    re.compile(r'"pineSource"\s*:\s*"'),
]


def parse_detail(html: str, url: str) -> Dict[str, str]:
    """Parse a detail page. Returns dict with title/author/description/pine_source."""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    author = ""

    og_title = soup.find("meta", attrs={"property": "og:title"})
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    og_title_content = (og_title.get("content") if og_title else "") or ""
    og_desc_content = (og_desc.get("content") if og_desc else "") or ""

    if og_title_content and " by " in og_title_content:
        head, tail = og_title_content.rsplit(" by ", 1)
        title = head.strip()
        author = tail.strip()
    elif og_title_content:
        title = og_title_content.strip()

    if not author:
        for u in re.findall(r'"username"\s*:\s*"([^"]+)"', html):
            if u and u != "Guest" and re.match(r"^[A-Za-z0-9_]+$", u):
                author = u
                break

    raw_desc = _longest_json_string_value(html, _DESC_KEY_RE) or ""
    readable = _collapse_ws(_tv_markup_to_text(raw_desc))
    og_plain = _collapse_ws(og_desc_content.replace("\\n", "\n"))
    description = readable if len(readable) > len(og_plain) else og_plain

    pine_source = ""
    for keyre in _PINE_KEY_RES:
        candidate = _longest_json_string_value(html, keyre)
        if candidate and ("//@version" in candidate or "study(" in candidate or
                          "strategy(" in candidate or "indicator(" in candidate):
            pine_source = candidate
            break

    return {
        "title": title,
        "author": author,
        "description": description,
        "pine_source": pine_source,
        "og_description": og_plain,
    }


# ---------------------------------------------------------------------------
# records.jsonl I/O
# ---------------------------------------------------------------------------

def load_existing_source_urls(records_path: Path) -> set[str]:
    """Return the set of source URLs already present in records.jsonl."""
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


def build_record(detail: Dict[str, str], url: str) -> Dict:
    """Construct an UNTESTED record matching codegen_strategy schema."""
    today = date.today().isoformat()
    sk = script_key_from_url(url)
    strategy_id = f"tv-{sk}"
    title = detail.get("title") or sk
    description = detail.get("description") or detail.get("og_description") or ""
    author = detail.get("author") or "unknown"
    pine = detail.get("pine_source") or ""

    entry_rules = description or f"Pine strategy `{title}` from TradingView."
    exit_rules = "as defined by the strategy's Pine source (see extra.pine_source)"
    risk_management = "as defined by the strategy's Pine source"

    extra: Dict = {
        "agent_summary": (
            f"TradingView Pine strategy `{title}` by {author}. "
            f"Description: {description[:400]}"
        ),
        "description_full_readable": description,
        "strategy_id": strategy_id,
        "methodology_family": "tradingview-pine",
        "instruments": [],
        "timeframes": {"execution": "1d"},
        "core_concepts": [],
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "risk_management": risk_management,
        "tested": False,
        "test_runs": [],
        "current_verdict": "UNTESTED",
        "verdict_summary": "scraped from TradingView, not yet validated",
        "failure_modes": [],
        "improvement_hypotheses": [],
        "code_paths": {},
        "data_artifacts": [],
        "first_logged_iso": today,
        "last_updated_iso": today,
        "pine_source": pine,
        "scraper": "scrape_tradingview",
    }

    return {
        "url": url,
        "title": title,
        "author": author,
        "description": description[:500],
        "source": "tradingview.com/scripts",
        "date_scraped": today,
        "tags": ["UNTESTED", "tradingview", "pine"],
        "extra": extra,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape(
    *,
    pages: int = 2,
    min_records: int = 10,
    records_path: Path = RECORDS_PATH,
    rate_limiter: Optional[RateLimiter] = None,
    listing_fetcher: Optional[Callable[[int], str]] = None,
    detail_fetcher: Optional[Callable[[str], str]] = None,
    dry_run: bool = False,
) -> Dict:
    """
    Scrape TradingView and append UNTESTED records.

    Returns: {pages_scraped, found, new, skipped, malformed, records: [...]}
    """
    rl = rate_limiter or RateLimiter()
    list_fn = listing_fetcher or fetch_listing_html
    det_fn = detail_fetcher or fetch_detail_html

    seen_urls = load_existing_source_urls(records_path)
    found_urls: List[str] = []
    seen_local: set[str] = set()

    for page in range(1, pages + 1):
        rl.wait()
        try:
            html = list_fn(page)
        except Exception as e:
            log(f"listing page {page} failed: {e}", "WARNING")
            continue
        for u in parse_listing(html):
            if u in seen_local:
                continue
            seen_local.add(u)
            found_urls.append(u)

    new_records: List[Dict] = []
    skipped = 0
    malformed = 0

    for url in found_urls:
        if url in seen_urls:
            skipped += 1
            continue
        if min_records and len(new_records) >= max(min_records * 3, 30):
            # Safety cap so a single run doesn't blow up a stale cache.
            break
        rl.wait()
        try:
            html = det_fn(url)
        except Exception as e:
            log(f"detail fetch failed {url}: {e}", "WARNING")
            malformed += 1
            continue
        try:
            detail = parse_detail(html, url)
        except Exception as e:
            log(f"detail parse failed {url}: {e}", "WARNING")
            malformed += 1
            continue
        if not detail.get("title"):
            malformed += 1
            continue
        record = build_record(detail, url)
        if not dry_run:
            append_record(records_path, record)
        new_records.append(record)
        seen_urls.add(url)

    return {
        "pages_scraped": pages,
        "found": len(found_urls),
        "new": len(new_records),
        "skipped": skipped,
        "malformed": malformed,
        "records": new_records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=2,
                        help="number of listing pages to crawl (default 2)")
    parser.add_argument("--min-records", type=int, default=10,
                        help="acceptance floor (default 10)")
    parser.add_argument("--records-path", type=str, default=str(RECORDS_PATH),
                        help="path to records.jsonl")
    parser.add_argument("--dry-run", action="store_true",
                        help="do not write to records.jsonl")
    args = parser.parse_args()

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"scrape_tradingview start at {started}", "INFO")

    summary = scrape(
        pages=args.pages,
        min_records=args.min_records,
        records_path=Path(args.records_path),
        dry_run=args.dry_run,
    )

    log(
        f"done: pages={summary['pages_scraped']} found={summary['found']} "
        f"new={summary['new']} skipped={summary['skipped']} "
        f"malformed={summary['malformed']}",
        "SUCCESS" if summary["new"] >= args.min_records else "WARNING",
    )
    if summary["new"] < args.min_records:
        log(
            f"acceptance floor {args.min_records} not met (new={summary['new']}). "
            "Pine listing may be cached or rate-limited.",
            "WARNING",
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
