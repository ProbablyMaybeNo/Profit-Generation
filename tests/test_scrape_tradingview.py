import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import scrape_tradingview as st  # noqa: E402


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

def _listing_html(slugs):
    """Build a minimal listing page with N script anchors."""
    anchors = "\n".join(
        f'<a class="tv-widget-idea__title" href="/script/{slug}/">{slug}</a>'
        for slug in slugs
    )
    return f"""<!doctype html>
<html><head><title>Strategies</title></head>
<body>
  <div class="tv-widget-idea">
    {anchors}
    <a href="/markets/stocks-usa/">markets (not a script)</a>
    <a href="/script/INVALID%20slug/">malformed</a>
  </div>
</body></html>"""


def _detail_html(title, author, description, pine=""):
    payload = {
        "title": title,
        "author": author,
        "description": description,
        "source": pine,
    }
    embedded = json.dumps(payload)
    return f"""<!doctype html>
<html><head>
  <meta property="og:title" content="{title} by {author}">
  <meta property="og:description" content="{description}">
</head>
<body>
  <h1>{title}</h1>
  <script type="application/json">{embedded}</script>
  <script>window.__INITIAL_STATE__ = {embedded};</script>
</body></html>"""


# ---------------------------------------------------------------------------
# parse_listing / normalize_detail_url
# ---------------------------------------------------------------------------

def test_parse_listing_extracts_script_urls():
    html = _listing_html(["abc123-trend-follower", "xyz789-meanrev",
                          "qqq-breakout"])
    urls = st.parse_listing(html)
    assert len(urls) == 3
    assert all(u.startswith("https://www.tradingview.com/script/") for u in urls)
    assert all(u.endswith("/") for u in urls)


def test_parse_listing_dedupes_within_page():
    html = _listing_html(["same-slug", "same-slug", "other-slug"])
    urls = st.parse_listing(html)
    assert len(urls) == 2


def test_normalize_detail_url_rejects_non_script_paths():
    assert st.normalize_detail_url("/markets/stocks/") is None
    assert st.normalize_detail_url("https://example.com/script/abc/") is None
    assert st.normalize_detail_url("") is None
    assert st.normalize_detail_url("/script/INVALID slug/") is None


def test_normalize_detail_url_handles_absolute_and_relative():
    rel = st.normalize_detail_url("/script/abc-123/")
    abso = st.normalize_detail_url("https://www.tradingview.com/script/abc-123/")
    assert rel == abso == "https://www.tradingview.com/script/abc-123/"


# ---------------------------------------------------------------------------
# parse_detail
# ---------------------------------------------------------------------------

def test_parse_detail_extracts_metadata():
    html = _detail_html(
        title="RSI Pullback Strategy",
        author="user123",
        description="Long when RSI(2) < 10, exit when RSI(2) > 70.",
        pine="//@version=5\nstrategy('RSI Pullback')\nplot(rsi(close, 2))",
    )
    result = st.parse_detail(html, "https://www.tradingview.com/script/abc/")
    assert result["title"] == "RSI Pullback Strategy"
    assert result["author"] == "user123"
    assert "RSI(2)" in result["description"]
    assert "//@version=5" in result["pine_source"]


def test_parse_detail_handles_missing_pine_gracefully():
    html = _detail_html("Title", "user", "desc", pine="")
    result = st.parse_detail(html, "https://www.tradingview.com/script/abc/")
    assert result["pine_source"] == ""
    assert result["title"] == "Title"


def test_parse_detail_falls_back_to_og_description_when_payload_short():
    html = """<!doctype html><html><head>
      <meta property="og:title" content="Foo by bar">
      <meta property="og:description" content="OG fallback description here.">
    </head><body>
      <script>{"description":"short"}</script>
    </body></html>"""
    result = st.parse_detail(html, "https://www.tradingview.com/script/abc/")
    assert "OG fallback" in result["description"]


# ---------------------------------------------------------------------------
# build_record schema
# ---------------------------------------------------------------------------

def test_build_record_matches_untested_schema():
    detail = {
        "title": "Breakout",
        "author": "trader42",
        "description": "Long on 20-bar high break.",
        "pine_source": "//@version=5\nstrategy('Breakout')",
        "og_description": "",
    }
    url = "https://www.tradingview.com/script/abc123-breakout/"
    rec = st.build_record(detail, url)

    assert rec["url"] == url
    assert rec["title"] == "Breakout"
    assert rec["author"] == "trader42"
    assert rec["source"] == "tradingview.com/scripts"
    assert "tradingview" in rec["tags"]
    extra = rec["extra"]
    assert extra["strategy_id"] == "tv-abc123-breakout"
    assert extra["current_verdict"] == "UNTESTED"
    assert extra["tested"] is False
    assert extra["entry_rules"]
    assert extra["exit_rules"]
    assert extra["risk_management"]
    assert extra["pine_source"].startswith("//@version=5")
    assert extra["methodology_family"] == "tradingview-pine"
    assert extra["scraper"] == "scrape_tradingview"


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------

def test_load_existing_source_urls_handles_missing_file(tmp_path):
    assert st.load_existing_source_urls(tmp_path / "nope.jsonl") == set()


def test_load_existing_source_urls_reads_url_field(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text(
        json.dumps({"url": "https://www.tradingview.com/script/aaa/",
                    "extra": {}}) + "\n" +
        json.dumps({"url": "https://www.tradingview.com/script/bbb/",
                    "extra": {}}) + "\n" +
        "not json at all\n",  # skipped silently
        encoding="utf-8",
    )
    urls = st.load_existing_source_urls(p)
    assert urls == {
        "https://www.tradingview.com/script/aaa/",
        "https://www.tradingview.com/script/bbb/",
    }


# ---------------------------------------------------------------------------
# rate limiter
# ---------------------------------------------------------------------------

def test_rate_limiter_enforces_min_interval():
    sleeps = []
    now_val = [0.0]

    def fake_sleep(s):
        sleeps.append(s)
        now_val[0] += s

    def fake_now():
        return now_val[0]

    rl = st.RateLimiter(min_interval=1.0, sleep=fake_sleep, now=fake_now)
    rl.wait()  # first call: no sleep
    rl.wait()
    rl.wait()
    assert len(sleeps) == 2
    assert all(s >= 0.99 for s in sleeps)


def test_rate_limiter_no_sleep_when_enough_elapsed():
    sleeps = []
    now_val = [0.0]

    def fake_sleep(s):
        sleeps.append(s)
        now_val[0] += s

    def fake_now():
        return now_val[0]

    rl = st.RateLimiter(min_interval=1.0, sleep=fake_sleep, now=fake_now)
    rl.wait()
    now_val[0] += 5.0  # plenty of time passes
    rl.wait()
    assert sleeps == []


# ---------------------------------------------------------------------------
# scrape() end-to-end with injected fetchers
# ---------------------------------------------------------------------------

class _NoSleepRL(st.RateLimiter):
    def __init__(self):
        super().__init__(min_interval=0.0, sleep=lambda s: None,
                         now=lambda: 0.0)


def _fake_fetchers(slugs_per_page, detail_payloads):
    listing_calls = []
    detail_calls = []

    def listing_fetcher(page):
        listing_calls.append(page)
        slugs = slugs_per_page.get(page, [])
        return _listing_html(slugs)

    def detail_fetcher(url):
        detail_calls.append(url)
        payload = detail_payloads.get(url)
        if payload is None:
            raise RuntimeError(f"no fixture for {url}")
        if payload == "MALFORMED":
            return "<html><body>nothing useful</body></html>"
        return _detail_html(**payload)

    return listing_fetcher, detail_fetcher, listing_calls, detail_calls


def test_scrape_writes_at_least_min_records(tmp_path):
    records = tmp_path / "records.jsonl"
    slugs = [f"s{i}-strat" for i in range(12)]
    slugs_per_page = {1: slugs[:6], 2: slugs[6:]}
    detail_payloads = {
        f"https://www.tradingview.com/script/{s}/": {
            "title": f"Strat {s}",
            "author": "anon",
            "description": "Long on high break, exit on low break.",
            "pine": "//@version=5\nstrategy('s')",
        }
        for s in slugs
    }
    listing_fn, detail_fn, _, _ = _fake_fetchers(slugs_per_page, detail_payloads)

    summary = st.scrape(
        pages=2, min_records=10, records_path=records,
        rate_limiter=_NoSleepRL(),
        listing_fetcher=listing_fn, detail_fetcher=detail_fn,
    )
    assert summary["new"] >= 10
    assert records.exists()
    lines = [json.loads(l) for l in records.read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) == summary["new"]


def test_scrape_dedupes_against_existing_records(tmp_path):
    records = tmp_path / "records.jsonl"
    existing = {
        "url": "https://www.tradingview.com/script/already-here/",
        "extra": {},
    }
    records.write_text(json.dumps(existing) + "\n", encoding="utf-8")

    slugs_per_page = {1: ["already-here", "fresh-one"]}
    detail_payloads = {
        "https://www.tradingview.com/script/already-here/": {
            "title": "old", "author": "x", "description": "y", "pine": "",
        },
        "https://www.tradingview.com/script/fresh-one/": {
            "title": "new", "author": "x", "description": "y", "pine": "",
        },
    }
    listing_fn, detail_fn, _, detail_calls = _fake_fetchers(
        slugs_per_page, detail_payloads)

    summary = st.scrape(
        pages=1, min_records=1, records_path=records,
        rate_limiter=_NoSleepRL(),
        listing_fetcher=listing_fn, detail_fetcher=detail_fn,
    )

    assert summary["skipped"] == 1
    assert summary["new"] == 1
    # detail fetcher should NOT be called for the already-seen URL
    assert "https://www.tradingview.com/script/already-here/" not in detail_calls


def test_scrape_skips_malformed_detail_pages(tmp_path):
    records = tmp_path / "records.jsonl"
    slugs_per_page = {1: ["good", "bad"]}
    detail_payloads = {
        "https://www.tradingview.com/script/good/": {
            "title": "Good", "author": "u", "description": "d", "pine": "",
        },
        "https://www.tradingview.com/script/bad/": "MALFORMED",
    }
    listing_fn, detail_fn, _, _ = _fake_fetchers(slugs_per_page, detail_payloads)

    summary = st.scrape(
        pages=1, min_records=1, records_path=records,
        rate_limiter=_NoSleepRL(),
        listing_fetcher=listing_fn, detail_fetcher=detail_fn,
    )
    assert summary["malformed"] == 1
    assert summary["new"] == 1


def test_scrape_honors_rate_limiter_per_request(tmp_path):
    """Rate limiter wait() must be called before every HTTP fetch."""
    records = tmp_path / "records.jsonl"
    slugs_per_page = {1: ["a-strat", "b-strat"]}
    detail_payloads = {
        f"https://www.tradingview.com/script/{s}/": {
            "title": s, "author": "u", "description": "d", "pine": "",
        } for s in ("a-strat", "b-strat")
    }
    listing_fn, detail_fn, _, _ = _fake_fetchers(slugs_per_page, detail_payloads)

    waits = []

    class CountingRL(st.RateLimiter):
        def __init__(self):
            super().__init__(min_interval=0.0, sleep=lambda s: None,
                             now=lambda: 0.0)

        def wait(self):
            waits.append(1)

    st.scrape(
        pages=1, min_records=1, records_path=records,
        rate_limiter=CountingRL(),
        listing_fetcher=listing_fn, detail_fetcher=detail_fn,
    )
    # 1 listing fetch + 2 detail fetches = 3 waits
    assert len(waits) == 3


def test_scrape_dry_run_does_not_write(tmp_path):
    records = tmp_path / "records.jsonl"
    slugs_per_page = {1: ["only-one"]}
    detail_payloads = {
        "https://www.tradingview.com/script/only-one/": {
            "title": "X", "author": "u", "description": "d", "pine": "",
        }
    }
    listing_fn, detail_fn, _, _ = _fake_fetchers(slugs_per_page, detail_payloads)
    summary = st.scrape(
        pages=1, min_records=1, records_path=records,
        rate_limiter=_NoSleepRL(), dry_run=True,
        listing_fetcher=listing_fn, detail_fetcher=detail_fn,
    )
    assert summary["new"] == 1
    assert not records.exists()


def test_scrape_listing_failure_does_not_abort(tmp_path):
    records = tmp_path / "records.jsonl"

    def listing_fetcher(page):
        if page == 1:
            raise RuntimeError("rate limited")
        return _listing_html(["only-page-two"])

    def detail_fetcher(url):
        return _detail_html("Only page two", "u", "d", "")

    summary = st.scrape(
        pages=2, min_records=1, records_path=records,
        rate_limiter=_NoSleepRL(),
        listing_fetcher=listing_fetcher, detail_fetcher=detail_fetcher,
    )
    assert summary["new"] == 1
