import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402
from monitoring import news_sentiment_overlay as nso  # noqa: E402
from scripts import news_sentiment_overlay as nso_cli  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    yield test_db


@pytest.fixture()
def client(isolated_db):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


_BAR_TS_COUNTER = [0]


def _seed_trade(conn, *, strategy_id, symbol, entry_ts, return_pct,
                bar_interval="1d"):
    _BAR_TS_COUNTER[0] += 1
    bar_ts = f"2020-01-{(_BAR_TS_COUNTER[0] % 28) + 1:02d}T{_BAR_TS_COUNTER[0]:05d}"
    sid = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts, signal_type="long_entry",
        close=100.0, bar_interval=bar_interval,
    )
    db.open_outcome(conn, signal_id=sid, entry_ts=entry_ts,
                    entry_price=100.0)
    db.close_outcome(
        conn, signal_id=sid,
        exit_ts=entry_ts, exit_price=100.0 * (1 + return_pct / 100),
        exit_reason="long_exit_signal", bars_held=1,
    )


def _seed_news(conn, *, polygon_id, symbol, published_utc, insights):
    """Insert a news row whose sentiment payload is `insights` (a list
    of {ticker, sentiment, ...} dicts) serialized as JSON."""
    db.insert_news(conn, {
        "polygon_id": polygon_id,
        "fetched_at": "2026-05-15T00:00:00",
        "published_utc": published_utc,
        "symbol": symbol,
        "title": f"news {polygon_id}",
        "url": f"https://example.com/{polygon_id}",
        "insights": insights,
    })


# ---------------------------------------------------------------------------
# extract_sentiment_labels
# ---------------------------------------------------------------------------

def test_extract_sentiment_labels_matches_symbol():
    raw = json.dumps([
        {"ticker": "GDX", "sentiment": "positive"},
        {"ticker": "SPY", "sentiment": "negative"},
    ])
    assert nso.extract_sentiment_labels(raw, "GDX") == ["positive"]


def test_extract_sentiment_labels_case_insensitive():
    raw = json.dumps([{"ticker": "gdx", "sentiment": "POSITIVE"}])
    assert nso.extract_sentiment_labels(raw, "GDX") == ["positive"]


def test_extract_sentiment_labels_skips_unknown_label():
    raw = json.dumps([{"ticker": "GDX", "sentiment": "mixed"}])
    assert nso.extract_sentiment_labels(raw, "GDX") == []


def test_extract_sentiment_labels_malformed_json():
    assert nso.extract_sentiment_labels("not json", "GDX") == []


def test_extract_sentiment_labels_none():
    assert nso.extract_sentiment_labels(None, "GDX") == []


def test_extract_sentiment_labels_dict_not_list():
    # Polygon sometimes returns a single dict instead of a list — we
    # only accept lists.
    raw = json.dumps({"ticker": "GDX", "sentiment": "positive"})
    assert nso.extract_sentiment_labels(raw, "GDX") == []


def test_extract_sentiment_labels_multiple_matches():
    raw = json.dumps([
        {"ticker": "GDX", "sentiment": "positive"},
        {"ticker": "GDX", "sentiment": "neutral"},
    ])
    assert nso.extract_sentiment_labels(raw, "GDX") == ["positive", "neutral"]


# ---------------------------------------------------------------------------
# dominant_label
# ---------------------------------------------------------------------------

def test_dominant_label_clear_majority():
    assert nso.dominant_label(["positive", "positive", "negative"]) == "positive"


def test_dominant_label_pos_neg_tie_falls_to_neutral():
    assert nso.dominant_label(["positive", "negative"]) == "neutral"


def test_dominant_label_neutral_in_tie_keeps_neutral():
    assert nso.dominant_label(["positive", "neutral"]) == "neutral"


def test_dominant_label_empty():
    assert nso.dominant_label([]) is None


def test_dominant_label_single():
    assert nso.dominant_label(["negative"]) == "negative"


# ---------------------------------------------------------------------------
# bucket_for_trade
# ---------------------------------------------------------------------------

def test_bucket_no_news_returns_no_news():
    bucket = nso.bucket_for_trade("GDX", date(2026, 5, 15), [])
    assert bucket == "no_news"


def test_bucket_within_window_picks_dominant():
    raw = json.dumps([{"ticker": "GDX", "sentiment": "positive"}])
    news = [{"date": date(2026, 5, 15), "sentiment_raw": raw}]
    assert nso.bucket_for_trade("GDX", date(2026, 5, 15), news) == "positive"


def test_bucket_outside_window_excluded():
    raw = json.dumps([{"ticker": "GDX", "sentiment": "positive"}])
    # 5 days before entry; default window is ±1 day → excluded.
    news = [{"date": date(2026, 5, 10), "sentiment_raw": raw}]
    assert nso.bucket_for_trade("GDX", date(2026, 5, 15), news) == "no_news"


def test_bucket_boundary_day_included():
    raw = json.dumps([{"ticker": "GDX", "sentiment": "negative"}])
    news = [{"date": date(2026, 5, 14), "sentiment_raw": raw}]
    assert nso.bucket_for_trade("GDX", date(2026, 5, 15), news) == "negative"


def test_bucket_only_irrelevant_tickers_yields_no_news():
    raw = json.dumps([{"ticker": "SPY", "sentiment": "positive"}])
    news = [{"date": date(2026, 5, 15), "sentiment_raw": raw}]
    assert nso.bucket_for_trade("GDX", date(2026, 5, 15), news) == "no_news"


def test_bucket_window_can_be_widened():
    raw = json.dumps([{"ticker": "GDX", "sentiment": "positive"}])
    news = [{"date": date(2026, 5, 10), "sentiment_raw": raw}]
    assert nso.bucket_for_trade(
        "GDX", date(2026, 5, 15), news, window_days=10,
    ) == "positive"


# ---------------------------------------------------------------------------
# slice_outcomes_by_sentiment
# ---------------------------------------------------------------------------

def test_slice_empty_outcomes_yields_empty():
    out = nso.slice_outcomes_by_sentiment([], {})
    assert out == []


def test_slice_buckets_by_sentiment():
    raw_pos = json.dumps([{"ticker": "GDX", "sentiment": "positive"}])
    raw_neg = json.dumps([{"ticker": "GDX", "sentiment": "negative"}])
    news_by_sym = {
        "GDX": [
            {"date": date(2026, 5, 1), "sentiment_raw": raw_pos},
            {"date": date(2026, 5, 5), "sentiment_raw": raw_neg},
        ],
    }
    outcomes = [
        {"strategy_id": "s1", "symbol": "GDX",
         "entry_ts": "2026-05-01", "return_pct": 2.0},
        {"strategy_id": "s1", "symbol": "GDX",
         "entry_ts": "2026-05-05", "return_pct": -3.0},
        {"strategy_id": "s1", "symbol": "GDX",
         "entry_ts": "2026-05-20", "return_pct": 1.0},  # no news
    ]
    rows = nso.slice_outcomes_by_sentiment(outcomes, news_by_sym)
    by_bucket = {r["sentiment"]: r for r in rows}
    assert by_bucket["positive"]["n"] == 1
    assert by_bucket["positive"]["mean"] == pytest.approx(2.0)
    assert by_bucket["negative"]["n"] == 1
    assert by_bucket["negative"]["mean"] == pytest.approx(-3.0)
    assert by_bucket["no_news"]["n"] == 1


def test_slice_preserves_bucket_order():
    raw_pos = json.dumps([{"ticker": "GDX", "sentiment": "positive"}])
    raw_neg = json.dumps([{"ticker": "GDX", "sentiment": "negative"}])
    # Trades placed far enough apart that each entry's ±1d window only
    # catches one news item.
    news_by_sym = {
        "GDX": [
            {"date": date(2026, 5, 1), "sentiment_raw": raw_pos},
            {"date": date(2026, 5, 10), "sentiment_raw": raw_neg},
        ],
    }
    outcomes = [
        {"strategy_id": "s1", "symbol": "GDX",
         "entry_ts": "2026-05-10", "return_pct": -1.0},
        {"strategy_id": "s1", "symbol": "GDX",
         "entry_ts": "2026-05-01", "return_pct": 1.0},
    ]
    rows = nso.slice_outcomes_by_sentiment(outcomes, news_by_sym)
    # positive first, negative second (BUCKETS order, not entry order).
    assert [r["sentiment"] for r in rows] == ["positive", "negative"]


def test_slice_handles_unparseable_entry_ts():
    outcomes = [
        {"strategy_id": "s1", "symbol": "GDX",
         "entry_ts": None, "return_pct": 1.0},
    ]
    assert nso.slice_outcomes_by_sentiment(outcomes, {}) == []


# ---------------------------------------------------------------------------
# compute_overlay (integration)
# ---------------------------------------------------------------------------

def test_compute_overlay_empty(isolated_db):
    conn = db.init_db()
    out = nso.compute_overlay(conn)
    assert out["rows"] == []
    assert out["n_trades_total"] == 0
    assert out["n_news_total"] == 0
    assert out["news_unavailable"] is True
    conn.close()


def test_compute_overlay_pipeline(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_news(
        conn, polygon_id="n1", symbol="GDX",
        published_utc="2026-05-01T12:00:00Z",
        insights=[{"ticker": "GDX", "sentiment": "positive"}],
    )
    _seed_news(
        conn, polygon_id="n2", symbol="GDX",
        published_utc="2026-05-05T12:00:00Z",
        insights=[{"ticker": "GDX", "sentiment": "negative"}],
    )
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-01", return_pct=2.0)
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-05", return_pct=-3.0)
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-20", return_pct=1.0)
    out = nso.compute_overlay(conn)
    assert out["n_trades_total"] == 3
    assert out["n_news_total"] == 2
    assert out["news_unavailable"] is False
    buckets = {r["sentiment"]: r for r in out["rows"]}
    assert buckets["positive"]["mean"] == pytest.approx(2.0)
    assert buckets["negative"]["mean"] == pytest.approx(-3.0)
    assert buckets["no_news"]["n"] == 1
    conn.close()


def test_compute_overlay_respects_window(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    # News 3 days before entry — outside default ±1d window, inside ±5d.
    _seed_news(
        conn, polygon_id="n1", symbol="GDX",
        published_utc="2026-05-01T12:00:00Z",
        insights=[{"ticker": "GDX", "sentiment": "positive"}],
    )
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-04", return_pct=1.5)
    out_narrow = nso.compute_overlay(conn, window_days=1)
    buckets_narrow = {r["sentiment"]: r for r in out_narrow["rows"]}
    assert "positive" not in buckets_narrow
    assert buckets_narrow["no_news"]["n"] == 1
    out_wide = nso.compute_overlay(conn, window_days=5)
    buckets_wide = {r["sentiment"]: r for r in out_wide["rows"]}
    assert buckets_wide["positive"]["n"] == 1
    conn.close()


def test_compute_overlay_excludes_non_1d(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-01", return_pct=1.0,
                bar_interval="1d-intraday")
    out = nso.compute_overlay(conn)
    assert out["rows"] == []
    conn.close()


# ---------------------------------------------------------------------------
# CLI / snapshot persistence
# ---------------------------------------------------------------------------

def test_cli_writes_snapshot(isolated_db, tmp_path, monkeypatch):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_news(
        conn, polygon_id="n1", symbol="GDX",
        published_utc="2026-05-01T12:00:00Z",
        insights=[{"ticker": "GDX", "sentiment": "positive"}],
    )
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-01", return_pct=2.0)
    conn.close()
    out_file = tmp_path / "overlay.json"
    monkeypatch.setattr(sys, "argv",
                        ["news_sentiment_overlay.py", "--out", str(out_file)])
    rc = nso_cli.main()
    assert rc == 0
    body = json.loads(out_file.read_text(encoding="utf-8"))
    assert body["rows"][0]["sentiment"] == "positive"
    assert "generated_at" in body


def test_cli_no_write_skips_file(isolated_db, tmp_path, monkeypatch):
    out_file = tmp_path / "overlay.json"
    monkeypatch.setattr(
        sys, "argv",
        ["news_sentiment_overlay.py", "--out", str(out_file), "--no-write"],
    )
    rc = nso_cli.main()
    assert rc == 0
    assert not out_file.exists()


def test_cli_default_out_path_uses_today():
    p = nso_cli.default_out_path()
    assert p.name == f"news_sentiment_overlay_{date.today().isoformat()}.json"
    assert p.parent.name == "logs"


def test_render_table_empty():
    assert "no closed outcomes" in nso_cli.render_table([])


def test_render_table_groups_by_strategy():
    rows = [
        {"strategy_id": "s1", "sentiment": "positive", "n": 2,
         "mean": 1.5, "win_rate": 1.0, "median": 1.5, "stdev": 0.0,
         "min": 1.0, "max": 2.0},
        {"strategy_id": "s1", "sentiment": "negative", "n": 1,
         "mean": -2.0, "win_rate": 0.0, "median": -2.0, "stdev": 0.0,
         "min": -2.0, "max": -2.0},
    ]
    text = nso_cli.render_table(rows)
    assert "s1" in text
    assert "positive" in text
    assert "negative" in text


# ---------------------------------------------------------------------------
# /api/news_sentiment_overlay endpoint
# ---------------------------------------------------------------------------

def test_endpoint_empty(client):
    body = client.get("/api/news_sentiment_overlay").get_json()
    assert body["rows"] == []
    assert body["news_unavailable"] is True


def test_endpoint_returns_rows(client, isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    _seed_news(
        conn, polygon_id="n1", symbol="GDX",
        published_utc="2026-05-01T12:00:00Z",
        insights=[{"ticker": "GDX", "sentiment": "positive"}],
    )
    _seed_trade(conn, strategy_id="s1", symbol="GDX",
                entry_ts="2026-05-01", return_pct=2.0)
    conn.close()
    body = client.get("/api/news_sentiment_overlay").get_json()
    assert body["rows"][0]["sentiment"] == "positive"
    assert body["rows"][0]["mean"] == pytest.approx(2.0)


def test_index_html_includes_sentiment_card(client):
    text = client.get("/").get_data(as_text=True)
    assert 'id="news-sentiment-overlay"' in text
    assert "news sentiment overlay" in text.lower()
