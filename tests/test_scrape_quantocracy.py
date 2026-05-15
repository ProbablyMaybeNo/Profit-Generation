import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import scrape_quantocracy as qc  # noqa: E402


def _good_item(sid="alpha"):
    return {
        "strategy_id": sid,
        "title": f"{sid} title",
        "entry_rules": "Long when close > 50-bar SMA.",
        "exit_rules": "Exit when close < 50-bar SMA.",
        "risk_management": "Stop at 2x ATR(20).",
    }


def _rss_xml(items):
    item_xml = "\n".join(
        f"<item><title>{it['title']}</title><link>{it['link']}</link>"
        f"<description>{it['description']}</description>"
        f"<pubDate>{it.get('pubDate', '')}</pubDate></item>"
        for it in items
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Quantocracy</title>
<link>https://quantocracy.com/</link>
{item_xml}
</channel></rss>"""


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------

def test_parse_rss_extracts_items():
    xml = _rss_xml([
        {"title": "Post 1", "link": "https://example.com/p1",
         "description": "Body 1"},
        {"title": "Post 2", "link": "https://example.com/p2",
         "description": "Body 2"},
    ])
    items = qc.parse_rss(xml)
    assert len(items) == 2
    assert items[0]["title"] == "Post 1"
    assert items[0]["link"] == "https://example.com/p1"


def test_parse_rss_skips_items_without_link():
    xml = """<?xml version='1.0'?><rss><channel>
    <item><title>no link</title><link></link></item>
    <item><title>has link</title><link>https://x.com/p</link></item>
    </channel></rss>"""
    items = qc.parse_rss(xml)
    assert len(items) == 1
    assert items[0]["title"] == "has link"


def test_parse_rss_returns_empty_on_garbage():
    assert qc.parse_rss("not xml at all") == []


# ---------------------------------------------------------------------------
# Article body extraction
# ---------------------------------------------------------------------------

def test_extract_article_text_strips_scripts_and_nav():
    html = """<html><body>
    <nav>menu</nav>
    <script>var x = 1;</script>
    <article><p>Real content here.</p><p>More content.</p></article>
    <footer>copyright</footer>
    </body></html>"""
    text = qc.extract_article_text(html)
    assert "Real content here" in text
    assert "More content" in text
    assert "var x" not in text
    assert "menu" not in text
    assert "copyright" not in text


def test_extract_article_text_falls_back_to_body():
    html = "<html><body><p>just a body paragraph.</p></body></html>"
    text = qc.extract_article_text(html)
    assert "just a body" in text


def test_extract_article_text_truncates_to_max_bytes():
    big = "<html><body><article><p>" + "A" * 100_000 + "</p></article></body></html>"
    text = qc.extract_article_text(big)
    assert len(text) <= qc.MAX_BODY_BYTES


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------

def test_parse_extraction_accepts_clean_json():
    raw = json.dumps(_good_item("a"))
    assert qc.parse_extraction(raw) is not None


def test_parse_extraction_returns_none_on_NONE():
    assert qc.parse_extraction("NONE") is None


def test_parse_extraction_strips_markdown_fence():
    raw = "```json\n" + json.dumps(_good_item("a")) + "\n```"
    assert qc.parse_extraction(raw) is not None


def test_parse_extraction_rejects_missing_fields():
    assert qc.parse_extraction(json.dumps({"strategy_id": "a"})) is None


# ---------------------------------------------------------------------------
# Record schema
# ---------------------------------------------------------------------------

def test_build_record_matches_untested_schema():
    item = _good_item("mean-rev")
    rec = qc.build_record(item, source_url="https://blog.com/p",
                          post_title="Mean Reversion 101",
                          model="qwen2.5-coder:14b")
    assert rec["url"] == "https://blog.com/p"
    assert rec["source"] == "quantocracy.com"
    assert "quantocracy" in rec["tags"]
    extra = rec["extra"]
    assert extra["strategy_id"] == "qc-mean-rev"
    assert extra["current_verdict"] == "UNTESTED"
    assert extra["tested"] is False
    assert extra["scraper"] == "scrape_quantocracy"
    assert extra["llm_model"] == "qwen2.5-coder:14b"
    assert extra["methodology_family"] == "quantocracy-extracted"


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def test_load_existing_source_urls_handles_missing(tmp_path):
    assert qc.load_existing_source_urls(tmp_path / "no.jsonl") == set()


def test_load_existing_source_urls_reads_url_field(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        json.dumps({"url": "https://blog.com/a", "extra": {}}) + "\n" +
        json.dumps({"url": "https://blog.com/b", "extra": {}}) + "\n",
        encoding="utf-8",
    )
    urls = qc.load_existing_source_urls(p)
    assert urls == {"https://blog.com/a", "https://blog.com/b"}


# ---------------------------------------------------------------------------
# scrape() end-to-end with injected fetchers
# ---------------------------------------------------------------------------

class _NoSleepRL(qc.RateLimiter):
    def __init__(self):
        super().__init__(min_interval=0.0, sleep=lambda s: None,
                         now=lambda: 0.0)


def test_scrape_extracts_at_least_five_strategies(tmp_path):
    records = tmp_path / "records.jsonl"
    rss_items = [
        {"title": f"Post {i}",
         "link": f"https://blog.com/p{i}",
         "description": "summary"}
        for i in range(6)
    ]
    rss_xml = _rss_xml(rss_items)

    def rss_fetcher(_url):
        return rss_xml

    def article_fetcher(url):
        return ("<html><body><article>"
                f"<p>Strategy body for {url}.</p>"
                "</article></body></html>")

    def llm_caller(prompt):
        # Pick a unique strategy_id per URL so dedupe inside the run holds.
        m = prompt.split("SOURCE: ")[1].split("\n")[0]
        sid = m.rsplit("/", 1)[-1]
        return json.dumps(_good_item(sid))

    summary = qc.scrape(
        rss_url="https://example.com/feed/",
        max_posts=10, records_path=records,
        rate_limiter=_NoSleepRL(),
        rss_fetcher=rss_fetcher,
        article_fetcher=article_fetcher,
        ollama_caller=llm_caller,
    )
    assert summary["new"] >= 5


def test_scrape_dedupes_against_existing_urls(tmp_path):
    records = tmp_path / "records.jsonl"
    records.write_text(
        json.dumps({"url": "https://blog.com/old", "extra": {}}) + "\n",
        encoding="utf-8",
    )
    rss_xml = _rss_xml([
        {"title": "Old", "link": "https://blog.com/old", "description": ""},
        {"title": "New", "link": "https://blog.com/new", "description": ""},
    ])

    def article_fetcher(url):
        return f"<html><body><article><p>body for {url}</p></article></body></html>"

    def llm_caller(prompt):
        return json.dumps(_good_item("fresh"))

    summary = qc.scrape(
        rss_url="x", max_posts=10, records_path=records,
        rate_limiter=_NoSleepRL(),
        rss_fetcher=lambda u: rss_xml,
        article_fetcher=article_fetcher,
        ollama_caller=llm_caller,
    )
    assert summary["skipped"] == 1
    assert summary["new"] == 1


def test_scrape_handles_NONE_llm_response_as_malformed(tmp_path):
    records = tmp_path / "records.jsonl"
    rss_xml = _rss_xml([{"title": "t", "link": "https://blog.com/a",
                         "description": ""}])
    summary = qc.scrape(
        rss_url="x", max_posts=5, records_path=records,
        rate_limiter=_NoSleepRL(),
        rss_fetcher=lambda u: rss_xml,
        article_fetcher=lambda u: "<html><body><article><p>x</p></article></body></html>",
        ollama_caller=lambda p: "NONE",
    )
    assert summary["new"] == 0
    assert summary["malformed"] == 1


def test_scrape_returns_empty_when_rss_fetch_fails(tmp_path):
    records = tmp_path / "records.jsonl"

    def boom(_url):
        raise RuntimeError("rss down")

    summary = qc.scrape(
        rss_url="x", max_posts=5, records_path=records,
        rate_limiter=_NoSleepRL(),
        rss_fetcher=boom,
        article_fetcher=lambda u: "x",
        ollama_caller=lambda p: "x",
    )
    assert summary["new"] == 0
    assert summary["posts"] == 0


def test_scrape_dry_run_does_not_write(tmp_path):
    records = tmp_path / "records.jsonl"
    rss_xml = _rss_xml([{"title": "t", "link": "https://blog.com/a",
                         "description": ""}])
    summary = qc.scrape(
        rss_url="x", max_posts=5, records_path=records,
        rate_limiter=_NoSleepRL(),
        dry_run=True,
        rss_fetcher=lambda u: rss_xml,
        article_fetcher=lambda u: "<html><body><article><p>x</p></article></body></html>",
        ollama_caller=lambda p: json.dumps(_good_item("a")),
    )
    assert summary["new"] == 1
    assert not records.exists()


def test_scrape_handles_llm_error_per_post(tmp_path):
    records = tmp_path / "records.jsonl"
    rss_xml = _rss_xml([
        {"title": "good", "link": "https://blog.com/good", "description": ""},
        {"title": "bad", "link": "https://blog.com/bad", "description": ""},
    ])

    def llm(prompt):
        if "bad" in prompt:
            raise RuntimeError("boom")
        return json.dumps(_good_item("g"))

    summary = qc.scrape(
        rss_url="x", max_posts=5, records_path=records,
        rate_limiter=_NoSleepRL(),
        rss_fetcher=lambda u: rss_xml,
        article_fetcher=lambda u: "<html><body><article><p>x</p></article></body></html>",
        ollama_caller=llm,
    )
    assert summary["new"] == 1
    assert summary["malformed"] == 1


def test_scrape_caps_at_max_posts(tmp_path):
    records = tmp_path / "records.jsonl"
    rss_xml = _rss_xml([
        {"title": f"p{i}", "link": f"https://blog.com/p{i}",
         "description": ""}
        for i in range(20)
    ])
    call_count = [0]

    def llm(prompt):
        call_count[0] += 1
        return json.dumps(_good_item(f"x{call_count[0]}"))

    summary = qc.scrape(
        rss_url="x", max_posts=3, records_path=records,
        rate_limiter=_NoSleepRL(),
        rss_fetcher=lambda u: rss_xml,
        article_fetcher=lambda u: "<html><body><article><p>x</p></article></body></html>",
        ollama_caller=llm,
    )
    assert summary["posts"] == 3
    assert summary["new"] == 3


# ---------------------------------------------------------------------------
# Ollama plumbing (mocked)
# ---------------------------------------------------------------------------

def _mock_ollama_response(text: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"response": text}
    return r


def test_call_ollama_round_trip(monkeypatch):
    raw = json.dumps(_good_item("a"))
    monkeypatch.setattr(qc, "_ollama_post",
                        lambda url, payload, timeout: _mock_ollama_response(raw))
    assert qc.call_ollama("p") == raw


def test_call_ollama_raises_on_non_200(monkeypatch):
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "boom"
    monkeypatch.setattr(qc, "_ollama_post",
                        lambda url, payload, timeout: bad)
    with pytest.raises(RuntimeError, match="ollama 500"):
        qc.call_ollama("p")
