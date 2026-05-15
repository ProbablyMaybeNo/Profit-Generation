import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import scrape_quantpedia as qp  # noqa: E402


def _good_item(sid="alpha"):
    return {
        "strategy_id": sid,
        "title": f"{sid} title",
        "entry_rules": "Long when close > 50-bar SMA.",
        "exit_rules": "Exit when close < 50-bar SMA.",
        "risk_management": "Stop at 2x ATR(20).",
    }


def _listing_html(slugs):
    anchors = "\n".join(
        f'<a href="/strategies/{slug}/">{slug}</a>' for slug in slugs
    )
    return f"""<!doctype html><html><body>
    {anchors}
    <a href="/about/">about (not a strategy)</a>
    <a href="https://other.com/strategies/abc/">offsite (rejected)</a>
    </body></html>"""


def _detail_html(title, body):
    return f"""<!doctype html><html><head>
    <meta property="og:title" content="{title}">
    <title>{title} | Quantpedia</title>
    </head><body>
    <article><h1>{title}</h1><p>{body}</p></article>
    </body></html>"""


# ---------------------------------------------------------------------------
# Listing parsing
# ---------------------------------------------------------------------------

def test_parse_listing_extracts_strategy_urls():
    html = _listing_html(["abc", "def", "ghi"])
    urls = qp.parse_listing(html)
    assert len(urls) == 3
    assert all(u.startswith("https://quantpedia.com/strategies/") for u in urls)


def test_parse_listing_rejects_non_strategy_paths():
    html = _listing_html(["abc"])
    urls = qp.parse_listing(html)
    assert all("/about/" not in u for u in urls)
    assert all("other.com" not in u for u in urls)


def test_normalize_detail_url_handles_absolute_and_relative():
    rel = qp.normalize_detail_url("/strategies/abc/")
    abso = qp.normalize_detail_url("https://quantpedia.com/strategies/abc/")
    assert rel == abso == "https://quantpedia.com/strategies/abc/"


def test_normalize_detail_url_rejects_offsite():
    assert qp.normalize_detail_url("https://other.com/strategies/abc/") is None


def test_normalize_detail_url_rejects_non_strategy():
    assert qp.normalize_detail_url("/about/") is None
    assert qp.normalize_detail_url("") is None


# ---------------------------------------------------------------------------
# Article body / title extraction
# ---------------------------------------------------------------------------

def test_extract_article_text_strips_scripts():
    html = """<html><body>
    <script>var x=1;</script>
    <article><p>Body content here.</p></article>
    </body></html>"""
    text = qp.extract_article_text(html)
    assert "Body content here" in text
    assert "var x" not in text


def test_extract_title_prefers_og_title():
    html = _detail_html("OG Title", "body")
    assert qp.extract_title(html) == "OG Title"


def test_extract_title_falls_back_to_title_tag():
    html = "<html><head><title>Page Title</title></head><body></body></html>"
    assert qp.extract_title(html) == "Page Title"


def test_extract_title_falls_back_to_h1():
    html = "<html><body><h1>Main H1</h1></body></html>"
    assert qp.extract_title(html) == "Main H1"


def test_extract_title_empty_when_nothing_present():
    assert qp.extract_title("<html><body><p>x</p></body></html>") == ""


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------

def test_parse_extraction_accepts_clean_json():
    raw = json.dumps(_good_item("a"))
    assert qp.parse_extraction(raw) is not None


def test_parse_extraction_returns_none_on_NONE():
    assert qp.parse_extraction("NONE") is None


def test_parse_extraction_rejects_missing_fields():
    assert qp.parse_extraction(json.dumps({"strategy_id": "a"})) is None


# ---------------------------------------------------------------------------
# Record schema
# ---------------------------------------------------------------------------

def test_build_record_matches_untested_schema():
    item = _good_item("alpha")
    rec = qp.build_record(
        item, source_url="https://quantpedia.com/strategies/abc/",
        page_title="ABC Strategy", model="qwen2.5-coder:14b",
    )
    assert rec["url"].endswith("/abc/")
    assert rec["source"] == "quantpedia.com"
    assert "quantpedia" in rec["tags"]
    extra = rec["extra"]
    assert extra["strategy_id"] == "qp-alpha"
    assert extra["current_verdict"] == "UNTESTED"
    assert extra["scraper"] == "scrape_quantpedia"
    assert extra["llm_model"] == "qwen2.5-coder:14b"
    assert extra["methodology_family"] == "quantpedia-extracted"


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def test_load_existing_source_urls_handles_missing(tmp_path):
    assert qp.load_existing_source_urls(tmp_path / "no.jsonl") == set()


def test_load_existing_source_urls_reads_url_field(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        json.dumps({"url": "https://quantpedia.com/strategies/a/",
                    "extra": {}}) + "\n",
        encoding="utf-8",
    )
    assert qp.load_existing_source_urls(p) == {
        "https://quantpedia.com/strategies/a/"}


# ---------------------------------------------------------------------------
# scrape() end-to-end with injected fetchers
# ---------------------------------------------------------------------------

class _NoSleepRL(qp.RateLimiter):
    def __init__(self):
        super().__init__(min_interval=0.0, sleep=lambda s: None,
                         now=lambda: 0.0)


def test_scrape_extracts_at_least_five_strategies(tmp_path):
    records = tmp_path / "records.jsonl"
    slugs = [f"s{i}" for i in range(7)]
    listing_html = _listing_html(slugs)
    detail_by_url = {
        f"https://quantpedia.com/strategies/{s}/": _detail_html(
            f"Strategy {s}", f"Body of {s}.") for s in slugs
    }

    def listing_fetcher(_page):
        return listing_html

    def detail_fetcher(url):
        return detail_by_url[url]

    def llm(prompt):
        # extract slug from prompt SOURCE: line for unique strategy_id
        line = [l for l in prompt.splitlines() if l.startswith("SOURCE:")][0]
        slug = line.rstrip("/").rsplit("/", 1)[-1]
        return json.dumps(_good_item(slug))

    summary = qp.scrape(
        pages=1, max_strategies=10, records_path=records,
        rate_limiter=_NoSleepRL(),
        listing_fetcher=listing_fetcher,
        detail_fetcher=detail_fetcher,
        ollama_caller=llm,
    )
    assert summary["new"] >= 5


def test_scrape_dedupes_against_existing_urls(tmp_path):
    records = tmp_path / "records.jsonl"
    existing_url = "https://quantpedia.com/strategies/old/"
    records.write_text(
        json.dumps({"url": existing_url, "extra": {}}) + "\n",
        encoding="utf-8",
    )
    listing = _listing_html(["old", "new"])
    detail_by_url = {
        existing_url: _detail_html("Old", "body"),
        "https://quantpedia.com/strategies/new/": _detail_html("New", "body"),
    }

    def llm(prompt):
        return json.dumps(_good_item("fresh"))

    summary = qp.scrape(
        pages=1, max_strategies=10, records_path=records,
        rate_limiter=_NoSleepRL(),
        listing_fetcher=lambda p: listing,
        detail_fetcher=lambda u: detail_by_url[u],
        ollama_caller=llm,
    )
    assert summary["skipped"] == 1
    assert summary["new"] == 1


def test_scrape_handles_NONE_llm_response(tmp_path):
    records = tmp_path / "records.jsonl"
    listing = _listing_html(["only"])
    summary = qp.scrape(
        pages=1, max_strategies=5, records_path=records,
        rate_limiter=_NoSleepRL(),
        listing_fetcher=lambda p: listing,
        detail_fetcher=lambda u: _detail_html("Only", "x"),
        ollama_caller=lambda p: "NONE",
    )
    assert summary["new"] == 0
    assert summary["malformed"] == 1


def test_scrape_handles_listing_failure_per_page(tmp_path):
    records = tmp_path / "records.jsonl"

    def listing_fetcher(page):
        if page == 1:
            raise RuntimeError("404")
        return _listing_html(["only"])

    summary = qp.scrape(
        pages=2, max_strategies=5, records_path=records,
        rate_limiter=_NoSleepRL(),
        listing_fetcher=listing_fetcher,
        detail_fetcher=lambda u: _detail_html("Only", "x"),
        ollama_caller=lambda p: json.dumps(_good_item("a")),
    )
    assert summary["new"] == 1


def test_scrape_caps_at_max_strategies(tmp_path):
    records = tmp_path / "records.jsonl"
    slugs = [f"s{i}" for i in range(10)]
    listing = _listing_html(slugs)
    detail_by_url = {
        f"https://quantpedia.com/strategies/{s}/": _detail_html(s, "body")
        for s in slugs
    }
    call_count = [0]

    def llm(prompt):
        call_count[0] += 1
        return json.dumps(_good_item(f"x{call_count[0]}"))

    summary = qp.scrape(
        pages=1, max_strategies=3, records_path=records,
        rate_limiter=_NoSleepRL(),
        listing_fetcher=lambda p: listing,
        detail_fetcher=lambda u: detail_by_url[u],
        ollama_caller=llm,
    )
    assert summary["new"] == 3


def test_scrape_dry_run_does_not_write(tmp_path):
    records = tmp_path / "records.jsonl"
    listing = _listing_html(["one"])
    summary = qp.scrape(
        pages=1, max_strategies=5, records_path=records,
        rate_limiter=_NoSleepRL(), dry_run=True,
        listing_fetcher=lambda p: listing,
        detail_fetcher=lambda u: _detail_html("One", "x"),
        ollama_caller=lambda p: json.dumps(_good_item("a")),
    )
    assert summary["new"] == 1
    assert not records.exists()


# ---------------------------------------------------------------------------
# Ollama plumbing
# ---------------------------------------------------------------------------

def _mock_ollama_response(text: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"response": text}
    return r


def test_call_ollama_round_trip(monkeypatch):
    raw = json.dumps(_good_item("a"))
    monkeypatch.setattr(qp, "_ollama_post",
                        lambda url, payload, timeout: _mock_ollama_response(raw))
    assert qp.call_ollama("p") == raw


def test_call_ollama_raises_on_non_200(monkeypatch):
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "boom"
    monkeypatch.setattr(qp, "_ollama_post",
                        lambda url, payload, timeout: bad)
    with pytest.raises(RuntimeError, match="ollama 500"):
        qp.call_ollama("p")
