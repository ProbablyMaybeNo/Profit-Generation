import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import news_fetcher  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


@pytest.fixture(autouse=True)
def _reset_rate_history():
    news_fetcher._call_history.clear()
    yield
    news_fetcher._call_history.clear()


@pytest.fixture(autouse=True)
def _stub_credentials(monkeypatch):
    monkeypatch.setattr(news_fetcher, "load_credentials", lambda key: {"api_key": "TEST_KEY_OK"})


def _mk_resp(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload or {"results": []}
    return resp


def _now_iso(offset_hours=0):
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_fetch_normalizes_polygon_payload(monkeypatch):
    raw_item = {
        "id": "poly-1",
        "title": "Big news",
        "article_url": "https://example/x",
        "published_utc": _now_iso(-1),
        "publisher": {"name": "Reuters"},
        "author": "Jane",
        "tickers": ["KRE", "XLF"],
        "keywords": ["banks"],
        "description": "desc",
        "insights": [{"ticker": "KRE", "sentiment": "negative"}],
    }
    monkeypatch.setattr(news_fetcher, "_http_get",
                        lambda url, params, timeout=15.0: _mk_resp(200, {"results": [raw_item]}))
    items = news_fetcher.fetch_news_for_symbol("KRE", use_cache=False)
    assert len(items) == 1
    item = items[0]
    assert item["polygon_id"] == "poly-1"
    assert item["symbol"] == "KRE"
    assert item["publisher"] == "Reuters"
    assert item["url"] == "https://example/x"
    assert item["tickers"] == ["KRE", "XLF"]
    assert item["insights"] == [{"ticker": "KRE", "sentiment": "negative"}]


def test_fetch_filters_old_items(monkeypatch):
    fresh = {"id": "fresh", "title": "fresh", "article_url": "u",
             "published_utc": _now_iso(-1), "publisher": {"name": "X"}}
    stale = {"id": "stale", "title": "stale", "article_url": "u",
             "published_utc": _now_iso(-200), "publisher": {"name": "X"}}
    monkeypatch.setattr(news_fetcher, "_http_get",
                        lambda url, params, timeout=15.0: _mk_resp(200, {"results": [fresh, stale]}))
    items = news_fetcher.fetch_news_for_symbol("SPY", max_age_hours=24, use_cache=False)
    assert [i["polygon_id"] for i in items] == ["fresh"]


def test_fetch_429_returns_empty(monkeypatch):
    monkeypatch.setattr(news_fetcher, "_http_get",
                        lambda url, params, timeout=15.0: _mk_resp(429))
    assert news_fetcher.fetch_news_for_symbol("GDX", use_cache=False) == []


def test_fetch_network_error_returns_empty(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(news_fetcher, "_http_get", boom)
    assert news_fetcher.fetch_news_for_symbol("GDX", use_cache=False) == []


def test_fetch_missing_credentials_returns_empty(monkeypatch):
    def raise_missing(_):
        raise FileNotFoundError("no creds")
    monkeypatch.setattr(news_fetcher, "load_credentials", raise_missing)
    assert news_fetcher.fetch_news_for_symbol("SPY", use_cache=False) == []


def test_fetch_placeholder_credentials_returns_empty(monkeypatch):
    monkeypatch.setattr(news_fetcher, "load_credentials",
                        lambda key: {"api_key": "PASTE_YOUR_POLYGON_KEY_HERE"})
    assert news_fetcher.fetch_news_for_symbol("SPY", use_cache=False) == []


def test_persist_news_items_dedupes(isolated_db, monkeypatch):
    raw = {"id": "p-1", "title": "T", "article_url": "u",
           "published_utc": _now_iso(-1), "publisher": {"name": "X"}}
    monkeypatch.setattr(news_fetcher, "_http_get",
                        lambda url, params, timeout=15.0: _mk_resp(200, {"results": [raw]}))
    items = news_fetcher.fetch_news_for_symbol("KRE", use_cache=False)
    n1 = news_fetcher.persist_news_items(items)
    n2 = news_fetcher.persist_news_items(items)
    assert n1 == 1
    assert n2 == 0
    conn = db.connect(isolated_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM news WHERE symbol='KRE'").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_fetch_and_persist_for_universe(isolated_db, monkeypatch):
    calls = []
    def fake_http(url, params, timeout=15.0):
        calls.append(params["ticker"])
        return _mk_resp(200, {"results": [{
            "id": f"id-{params['ticker']}", "title": f"News for {params['ticker']}",
            "article_url": "u", "published_utc": _now_iso(-1),
            "publisher": {"name": "P"},
        }]})
    monkeypatch.setattr(news_fetcher, "_http_get", fake_http)
    out = news_fetcher.fetch_and_persist_for_universe(
        ["KRE", "XME", "KRE"], limit=3, use_cache=False,
    )
    assert calls == ["KRE", "XME"]  # deduped
    assert set(out.keys()) == {"KRE", "XME"}
    assert out["KRE"][0]["title"] == "News for KRE"


def test_rate_limit_blocks_after_5_calls(monkeypatch):
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    base = 1_000_000.0
    fake_now = [base]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])

    for _ in range(5):
        news_fetcher._rate_limit_block()
    assert sleeps == []

    fake_now[0] = base + 10  # only 10s elapsed since first call
    news_fetcher._rate_limit_block()
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(50.05, rel=1e-3)


def test_rate_limit_no_sleep_if_window_passed(monkeypatch):
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    base = 2_000_000.0
    fake_now = [base]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])

    for _ in range(5):
        news_fetcher._rate_limit_block()
    fake_now[0] = base + 61
    news_fetcher._rate_limit_block()
    assert sleeps == []
