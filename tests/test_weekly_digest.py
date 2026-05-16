import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import weekly_digest as wd  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_closed_outcome(conn, *, strategy_id, symbol, entry_iso, exit_iso,
                         entry_price=100.0, exit_price=101.0,
                         bar_interval="1d"):
    sid = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=entry_iso, signal_type="long_entry",
        close=entry_price, bar_interval=bar_interval,
    )
    db.open_outcome(conn, signal_id=sid, entry_ts=entry_iso,
                    entry_price=entry_price)
    db.close_outcome(
        conn, signal_id=sid, exit_ts=exit_iso,
        exit_price=exit_price, exit_reason="long_exit_signal", bars_held=1,
    )


# ---------------------------------------------------------------------------
# _safe_stats
# ---------------------------------------------------------------------------

def test_safe_stats_empty():
    s = wd._safe_stats([])
    assert s["n"] == 0
    assert s["mean"] == 0.0
    assert s["win_rate"] == 0.0
    assert s["sum_ret"] == 0.0


def test_safe_stats_basic():
    s = wd._safe_stats([1.0, -0.5, 2.0, -1.0])
    assert s["n"] == 4
    assert s["mean"] == pytest.approx(0.375)
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["sum_ret"] == pytest.approx(1.5)
    assert s["best"] == pytest.approx(2.0)
    assert s["worst"] == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# aggregate_window
# ---------------------------------------------------------------------------

def test_aggregate_window_empty(isolated_db):
    conn = db.init_db()
    try:
        rollup = wd.aggregate_window(conn, asof=date(2026, 5, 17),
                                     window_days=7)
        assert rollup["window_start"] == "2026-05-11"
        assert rollup["window_end"] == "2026-05-17"
        assert rollup["fires"] == {}
        assert rollup["outcomes"]["n"] == 0
        assert rollup["by_strategy"] == []
        assert rollup["top_performer"] is None
        assert rollup["biggest_loser"] is None
        assert rollup["new_strategies"] == []
    finally:
        conn.close()


def test_aggregate_window_counts_fires_by_type(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
        db.upsert_strategy(conn, {"extra": {"strategy_id": "beta"}})
        db.record_signal(conn, strategy_id="alpha", symbol="A",
                         bar_ts="2026-05-12", signal_type="long_entry",
                         close=100.0, bar_interval="1d")
        db.record_signal(conn, strategy_id="alpha", symbol="A",
                         bar_ts="2026-05-13", signal_type="long_exit",
                         close=102.0, bar_interval="1d")
        db.record_signal(conn, strategy_id="beta", symbol="B",
                         bar_ts="2026-05-14", signal_type="long_entry",
                         close=50.0, bar_interval="1d")
        # Outside the window — should be excluded.
        db.record_signal(conn, strategy_id="alpha", symbol="A",
                         bar_ts="2026-05-01", signal_type="long_entry",
                         close=99.0, bar_interval="1d")
        rollup = wd.aggregate_window(conn, asof=date(2026, 5, 17),
                                     window_days=7)
        assert rollup["fires"] == {"long_entry": 2, "long_exit": 1}
    finally:
        conn.close()


def test_aggregate_window_aggregates_outcomes_in_window(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
        db.upsert_strategy(conn, {"extra": {"strategy_id": "beta"}})
        _seed_closed_outcome(conn, strategy_id="alpha", symbol="A",
                             entry_iso="2026-05-12", exit_iso="2026-05-13",
                             entry_price=100.0, exit_price=103.0)
        _seed_closed_outcome(conn, strategy_id="alpha", symbol="A",
                             entry_iso="2026-05-14", exit_iso="2026-05-15",
                             entry_price=100.0, exit_price=99.0)
        _seed_closed_outcome(conn, strategy_id="beta", symbol="B",
                             entry_iso="2026-05-13", exit_iso="2026-05-14",
                             entry_price=100.0, exit_price=101.0)
        rollup = wd.aggregate_window(conn, asof=date(2026, 5, 17),
                                     window_days=7)
        assert rollup["outcomes"]["n"] == 3
        # alpha sum = +3 - 1 = +2; beta sum = +1 → top=alpha
        assert rollup["top_performer"]["strategy_id"] == "alpha"
        # biggest_loser only set when worst sum_ret < 0 — neither strat lost here.
        assert rollup["biggest_loser"] is None
    finally:
        conn.close()


def test_aggregate_window_surfaces_biggest_loser(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
        db.upsert_strategy(conn, {"extra": {"strategy_id": "loser"}})
        _seed_closed_outcome(conn, strategy_id="winner", symbol="A",
                             entry_iso="2026-05-12", exit_iso="2026-05-13",
                             entry_price=100.0, exit_price=103.0)
        _seed_closed_outcome(conn, strategy_id="loser", symbol="B",
                             entry_iso="2026-05-13", exit_iso="2026-05-14",
                             entry_price=100.0, exit_price=97.0)
        rollup = wd.aggregate_window(conn, asof=date(2026, 5, 17),
                                     window_days=7)
        assert rollup["top_performer"]["strategy_id"] == "winner"
        assert rollup["biggest_loser"]["strategy_id"] == "loser"
        assert rollup["biggest_loser"]["sum_ret"] < 0
    finally:
        conn.close()


def test_aggregate_window_excludes_outcomes_outside_window(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
        # Outside the window (window = May 11–17).
        _seed_closed_outcome(conn, strategy_id="alpha", symbol="A",
                             entry_iso="2026-05-01", exit_iso="2026-05-02",
                             entry_price=100.0, exit_price=110.0)
        # Inside.
        _seed_closed_outcome(conn, strategy_id="alpha", symbol="A",
                             entry_iso="2026-05-12", exit_iso="2026-05-13",
                             entry_price=100.0, exit_price=101.0)
        rollup = wd.aggregate_window(conn, asof=date(2026, 5, 17),
                                     window_days=7)
        assert rollup["outcomes"]["n"] == 1
    finally:
        conn.close()


def test_aggregate_window_excludes_intraday_outcomes(isolated_db):
    """1d-intraday outcomes are NOT included (only EOD 1d closed trades)."""
    conn = db.init_db()
    try:
        db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
        _seed_closed_outcome(conn, strategy_id="alpha", symbol="A",
                             entry_iso="2026-05-12", exit_iso="2026-05-13",
                             entry_price=100.0, exit_price=101.0,
                             bar_interval="1d-intraday")
        rollup = wd.aggregate_window(conn, asof=date(2026, 5, 17),
                                     window_days=7)
        assert rollup["outcomes"]["n"] == 0
    finally:
        conn.close()


def test_aggregate_window_lists_new_strategies(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_strategy(conn, {
            "extra": {"strategy_id": "fresh"},
            "first_logged_iso": "2026-05-13T10:00:00",
        })
        db.upsert_strategy(conn, {
            "extra": {"strategy_id": "old"},
            "first_logged_iso": "2025-01-01T10:00:00",
        })
        rollup = wd.aggregate_window(conn, asof=date(2026, 5, 17),
                                     window_days=7)
        assert rollup["new_strategies"] == ["fresh"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------

def test_render_markdown_empty_window():
    rollup = {
        "window_start": "2026-05-11", "window_end": "2026-05-17",
        "fires": {}, "outcomes": wd._safe_stats([]),
        "by_strategy": [], "top_performer": None,
        "biggest_loser": None, "new_strategies": [],
    }
    md = wd.render_markdown(rollup)
    assert "Weekly Digest" in md
    assert "2026-05-11" in md
    assert "(none)" in md  # fires + new_strategies both empty


def test_render_markdown_includes_top_and_loser():
    rollup = {
        "window_start": "2026-05-11", "window_end": "2026-05-17",
        "fires": {"long_entry": 4, "long_exit": 3},
        "outcomes": {"n": 3, "mean": 0.5, "win_rate": 0.67,
                     "sum_ret": 1.5, "best": 2.0, "worst": -1.0},
        "by_strategy": [
            {"strategy_id": "alpha", "n": 2, "mean": 1.0,
             "win_rate": 1.0, "sum_ret": 2.0, "best": 1.5, "worst": 0.5},
            {"strategy_id": "loser", "n": 1, "mean": -0.5,
             "win_rate": 0.0, "sum_ret": -0.5, "best": -0.5, "worst": -0.5},
        ],
        "top_performer": {"strategy_id": "alpha", "n": 2, "mean": 1.0,
                          "win_rate": 1.0, "sum_ret": 2.0,
                          "best": 1.5, "worst": 0.5},
        "biggest_loser": {"strategy_id": "loser", "n": 1, "mean": -0.5,
                          "win_rate": 0.0, "sum_ret": -0.5,
                          "best": -0.5, "worst": -0.5},
        "new_strategies": ["delta"],
    }
    md = wd.render_markdown(rollup)
    assert "Top performer" in md and "alpha" in md
    assert "Biggest loser" in md and "loser" in md
    assert "delta" in md
    assert "+2.00%" in md or "+2.0%" in md  # sum formatting


# ---------------------------------------------------------------------------
# build_digest (end-to-end markdown)
# ---------------------------------------------------------------------------

def test_build_digest_runs_end_to_end(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_closed_outcome(conn, strategy_id="alpha", symbol="A",
                         entry_iso="2026-05-12", exit_iso="2026-05-13",
                         entry_price=100.0, exit_price=101.0)
    conn.close()
    digest = wd.build_digest(asof=date(2026, 5, 17), window_days=7)
    assert "Weekly Digest" in digest["markdown"]
    assert digest["rollup"]["outcomes"]["n"] == 1
    assert digest["rollup"]["top_performer"]["strategy_id"] == "alpha"


# ---------------------------------------------------------------------------
# post_to_notion
# ---------------------------------------------------------------------------

def test_post_to_notion_builds_correct_payload(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "page-123"}
        return resp

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(
        "monitoring.notion_writer._headers",
        lambda: {"Authorization": "Bearer test", "Notion-Version": "x",
                 "Content-Type": "application/json"},
    )
    md = "## Hello\n\nworld"
    out = wd.post_to_notion(
        window_start="2026-05-11", window_end="2026-05-17",
        markdown=md, database_id="db-abc",
    )
    assert out == {"id": "page-123"}
    body = captured["json"]
    assert body["parent"]["database_id"] == "db-abc"
    props = body["properties"]
    assert props["Source"]["select"]["name"] == "weekly-digest"
    assert props["Tags"]["multi_select"][0]["name"] == "Weekly Digest"
    assert props["Date"]["date"]["start"] == "2026-05-17"
    # Title includes both window edges.
    title_text = props["Report"]["title"][0]["text"]["content"]
    assert "2026-05-11" in title_text
    assert "2026-05-17" in title_text


def test_post_to_notion_raises_on_api_error(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 400
        resp.text = "bad request"
        return resp
    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(
        "monitoring.notion_writer._headers",
        lambda: {"Authorization": "Bearer test", "Notion-Version": "x",
                 "Content-Type": "application/json"},
    )
    with pytest.raises(RuntimeError):
        wd.post_to_notion(
            window_start="2026-05-11", window_end="2026-05-17",
            markdown="## x", database_id="db-abc",
        )
