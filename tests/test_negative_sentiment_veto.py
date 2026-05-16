import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_news_row(conn, *, polygon_id, symbol, published_utc, sentiments):
    """Seed a news row whose insights payload assigns each sentiment label
    to this `symbol`. `sentiments` is a list like ["negative"] or
    ["negative", "neutral"]."""
    insights = [{"ticker": symbol, "sentiment": s} for s in sentiments]
    db.insert_news(conn, {
        "polygon_id": polygon_id,
        "fetched_at": "2026-05-15T00:00:00",
        "published_utc": published_utc,
        "symbol": symbol,
        "title": f"n-{polygon_id}",
        "url": f"https://example.com/{polygon_id}",
        "insights": insights,
    })


def _seed_eligible_outcomes(conn, strategy_id, n=5):
    for i in range(n):
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="HIST",
            bar_ts=f"2026-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2026-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2026-01-{i+2:02d}",
            exit_price=101.0, exit_reason="long_exit_signal", bars_held=1,
        )


def _winner_settings(extra=None):
    out = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 3, "min_mean_ret_pct": -100.0, "min_sharpe_ish": -100.0,
        "max_position_usd": 1000, "skip_intraday_signals": True,
        "cool_down_losers": 0,
        "earnings_veto_days": 0,
        "veto_negative_sentiment": True,
        "negative_sentiment_threshold": 2,
        "negative_sentiment_window_hours": 24,
    }
    if extra:
        out.update(extra)
    return out


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Coercers
# ---------------------------------------------------------------------------

def test_coerce_threshold_defaults():
    assert (at._coerce_negative_sentiment_threshold(None)
            == at.DEFAULT_NEGATIVE_SENTIMENT_THRESHOLD)
    assert (at._coerce_negative_sentiment_threshold("bad")
            == at.DEFAULT_NEGATIVE_SENTIMENT_THRESHOLD)


def test_coerce_threshold_clamps_low():
    assert at._coerce_negative_sentiment_threshold(0) == 1
    assert at._coerce_negative_sentiment_threshold(-3) == 1


def test_coerce_window_defaults():
    assert (at._coerce_negative_sentiment_window_hours(None)
            == at.DEFAULT_NEGATIVE_SENTIMENT_WINDOW_HOURS)


def test_coerce_window_clamps_low():
    assert at._coerce_negative_sentiment_window_hours(0) == 1


# ---------------------------------------------------------------------------
# _count_negative_news_for_symbol
# ---------------------------------------------------------------------------

def test_count_negative_zero_when_no_news(isolated_db):
    conn = db.init_db()
    try:
        n = at._count_negative_news_for_symbol(
            conn, "KRE",
            asof_dt=datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
            window_hours=24,
        )
        assert n == 0
    finally:
        conn.close()


def test_count_negative_counts_distinct_rows(isolated_db):
    conn = db.init_db()
    try:
        asof = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        _seed_news_row(conn, polygon_id="n1", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["negative"])
        _seed_news_row(conn, polygon_id="n2", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=5)),
                       sentiments=["negative", "neutral"])
        _seed_news_row(conn, polygon_id="n3", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=10)),
                       sentiments=["positive"])
        n = at._count_negative_news_for_symbol(
            conn, "KRE", asof_dt=asof, window_hours=24,
        )
        assert n == 2
    finally:
        conn.close()


def test_count_negative_window_excludes_old_rows(isolated_db):
    conn = db.init_db()
    try:
        asof = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        _seed_news_row(conn, polygon_id="n1", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["negative"])
        _seed_news_row(conn, polygon_id="n2", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=48)),
                       sentiments=["negative"])
        n = at._count_negative_news_for_symbol(
            conn, "KRE", asof_dt=asof, window_hours=24,
        )
        assert n == 1  # the 48h-old item is outside the window
    finally:
        conn.close()


def test_count_negative_only_for_this_symbol(isolated_db):
    conn = db.init_db()
    try:
        asof = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        # negative on KRE
        _seed_news_row(conn, polygon_id="n1", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["negative"])
        # negative on SPY — should NOT count for KRE
        _seed_news_row(conn, polygon_id="n2", symbol="SPY",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["negative"])
        assert at._count_negative_news_for_symbol(
            conn, "KRE", asof_dt=asof, window_hours=24) == 1
        assert at._count_negative_news_for_symbol(
            conn, "SPY", asof_dt=asof, window_hours=24) == 1
    finally:
        conn.close()


def test_count_negative_ignores_positive_only_rows(isolated_db):
    conn = db.init_db()
    try:
        asof = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        _seed_news_row(conn, polygon_id="n1", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["positive"])
        _seed_news_row(conn, polygon_id="n2", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=4)),
                       sentiments=["neutral"])
        assert at._count_negative_news_for_symbol(
            conn, "KRE", asof_dt=asof, window_hours=24) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _negative_sentiment_veto
# ---------------------------------------------------------------------------

def test_veto_disabled_when_setting_false(isolated_db):
    conn = db.init_db()
    try:
        asof = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        for i in range(3):
            _seed_news_row(conn, polygon_id=f"n{i}", symbol="KRE",
                           published_utc=_iso(asof - timedelta(hours=2)),
                           sentiments=["negative"])
        settings = _winner_settings({"veto_negative_sentiment": False})
        assert at._negative_sentiment_veto(
            conn, "KRE", settings, asof=date(2026, 5, 12)) is None
    finally:
        conn.close()


def test_veto_trips_at_threshold(isolated_db):
    conn = db.init_db()
    try:
        asof = datetime(2026, 5, 12, 23, 59, tzinfo=timezone.utc)
        for i in range(2):
            _seed_news_row(conn, polygon_id=f"n{i}", symbol="KRE",
                           published_utc=_iso(asof - timedelta(hours=2)),
                           sentiments=["negative"])
        out = at._negative_sentiment_veto(
            conn, "KRE", _winner_settings(), asof=date(2026, 5, 12),
        )
        assert out is not None
        assert out["negative_count"] == 2
        assert out["threshold"] == 2
        assert out["window_hours"] == 24
    finally:
        conn.close()


def test_veto_does_not_trip_below_threshold(isolated_db):
    conn = db.init_db()
    try:
        asof = datetime(2026, 5, 12, 23, 59, tzinfo=timezone.utc)
        _seed_news_row(conn, polygon_id="n1", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["negative"])
        assert at._negative_sentiment_veto(
            conn, "KRE", _winner_settings(), asof=date(2026, 5, 12)) is None
    finally:
        conn.close()


def test_veto_honors_custom_threshold(isolated_db):
    conn = db.init_db()
    try:
        asof = datetime(2026, 5, 12, 23, 59, tzinfo=timezone.utc)
        for i in range(3):
            _seed_news_row(conn, polygon_id=f"n{i}", symbol="KRE",
                           published_utc=_iso(asof - timedelta(hours=2)),
                           sentiments=["negative"])
        # Threshold 5 → not enough.
        settings = _winner_settings({"negative_sentiment_threshold": 5})
        assert at._negative_sentiment_veto(
            conn, "KRE", settings, asof=date(2026, 5, 12)) is None
        # Threshold 3 → trips.
        settings = _winner_settings({"negative_sentiment_threshold": 3})
        out = at._negative_sentiment_veto(
            conn, "KRE", settings, asof=date(2026, 5, 12),
        )
        assert out is not None
        assert out["negative_count"] == 3
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Integration via process_signals
# ---------------------------------------------------------------------------

def test_process_signals_blocks_entry_when_sentiment_veto_trips(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_eligible_outcomes(conn, "alpha")
    asof = datetime(2026, 5, 12, 23, 59, tzinfo=timezone.utc)
    for i in range(2):
        _seed_news_row(conn, polygon_id=f"n{i}", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["negative"])
    db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["symbol"] == "KRE"]
    assert actions
    assert actions[0]["action"] == "SKIP_NEGATIVE_SENTIMENT"
    assert actions[0]["negative_count"] == 2
    conn.close()


def test_process_signals_allows_entry_with_setting_off(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_eligible_outcomes(conn, "alpha")
    asof = datetime(2026, 5, 12, 23, 59, tzinfo=timezone.utc)
    for i in range(3):
        _seed_news_row(conn, polygon_id=f"n{i}", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["negative"])
    db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    settings = _winner_settings({"veto_negative_sentiment": False})
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=settings,
    )
    actions = [a for a in res["actions"] if a["symbol"] == "KRE"]
    assert all(a["action"] != "SKIP_NEGATIVE_SENTIMENT" for a in actions)
    conn.close()


def test_process_signals_allows_exit_during_sentiment_veto(isolated_db):
    """Exits ALWAYS proceed — negative-sentiment veto only blocks entries."""
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_eligible_outcomes(conn, "alpha")
    asof = datetime(2026, 5, 12, 23, 59, tzinfo=timezone.utc)
    for i in range(3):
        _seed_news_row(conn, polygon_id=f"n{i}", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["negative"])
    sig_entry = db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-11", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": "entry-1", "signal_id": sig_entry,
        "strategy_id": "alpha", "symbol": "KRE",
        "side": "buy", "qty": 10, "order_type": "market",
        "submitted_at": "2026-05-11", "status": "filled",
        "fill_price": 70.0,
    })
    db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-12", signal_type="long_exit",
        close=72.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["symbol"] == "KRE"]
    assert any(a["action"] == "DRY_SELL" for a in actions)
    conn.close()


def test_process_signals_ignores_positive_only_news(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "alpha"}})
    _seed_eligible_outcomes(conn, "alpha")
    asof = datetime(2026, 5, 12, 23, 59, tzinfo=timezone.utc)
    for i in range(3):
        _seed_news_row(conn, polygon_id=f"n{i}", symbol="KRE",
                       published_utc=_iso(asof - timedelta(hours=2)),
                       sentiments=["positive"])
    db.record_signal(
        conn, strategy_id="alpha", symbol="KRE",
        bar_ts="2026-05-12", signal_type="long_entry",
        close=70.0, bar_interval="1d",
    )
    res = at.process_signals(
        conn, asof=date(2026, 5, 12), settings=_winner_settings(),
    )
    actions = [a for a in res["actions"] if a["symbol"] == "KRE"]
    assert all(a["action"] != "SKIP_NEGATIVE_SENTIMENT" for a in actions)
    conn.close()
