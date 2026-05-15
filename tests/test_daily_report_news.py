"""Focused tests for the news-weighted importance + tags refactor."""

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring.daily_report import (  # noqa: E402
    DailyReport, finalize_report,
    _compute_importance, _derive_tags, _news_metrics,
)


def _mk_report(*, fires=None, news=None, snapshot_rows=None, notable_movers=None):
    return DailyReport(
        report_date=date(2026, 5, 14),
        market_regime="choppy",
        snapshot_rows=snapshot_rows or [],
        fires=fires or [],
        notable_movers=notable_movers or [],
        symbols_watched=[r["symbol"] for r in (snapshot_rows or [])],
        news_by_symbol=news or {},
    )


def _news_item(sym, sentiment="negative"):
    return {
        "title": "x", "published_utc": "2026-05-14T12:00:00Z", "url": "u",
        "publisher": "p", "symbol": sym,
        "insights": [{"ticker": sym, "sentiment": sentiment}],
    }


def test_news_metrics_counts_negative_per_fired_symbol():
    report = _mk_report(
        fires=[{"strategy_id": "x", "symbol": "KRE", "close": 67.7, "bar_date": "2026-05-14"}],
        news={"KRE": [_news_item("KRE", "negative"), _news_item("KRE", "negative")],
              "SPY": [_news_item("SPY", "negative")]},
    )
    m = _news_metrics(report)
    assert m["negative_on_fires"] == 1  # only KRE is fired; SPY isn't
    assert m["total_news"] == 3


def test_importance_bumps_with_negative_news_on_fires():
    base = _mk_report(
        fires=[{"strategy_id": "x", "symbol": "KRE", "close": 67.7, "bar_date": "2026-05-14"}]
    )
    no_news_imp = _compute_importance(base, _news_metrics(base))
    with_neg = _mk_report(
        fires=[{"strategy_id": "x", "symbol": "KRE", "close": 67.7, "bar_date": "2026-05-14"}],
        news={"KRE": [_news_item("KRE", "negative")]},
    )
    bumped = _compute_importance(with_neg, _news_metrics(with_neg))
    assert bumped == no_news_imp + 1


def test_importance_caps_at_5():
    report = _mk_report(
        fires=[{"strategy_id": "x", "symbol": f"S{i}", "close": 10.0,
                "bar_date": "2026-05-14"} for i in range(7)],
        notable_movers=[{"symbol": "S0", "ret_1d_pct": -7.0, "asset_class": "etf"}],
        news={"S0": [_news_item("S0", "negative")]},
    )
    report.has_notable_pattern = True
    finalize_report(report)
    assert report.importance == 5


def test_importance_unaffected_by_positive_or_neutral_news():
    report = _mk_report(
        fires=[{"strategy_id": "x", "symbol": "KRE", "close": 67.7, "bar_date": "2026-05-14"}],
        news={"KRE": [_news_item("KRE", "positive"), _news_item("KRE", "neutral")]},
    )
    base = _mk_report(
        fires=[{"strategy_id": "x", "symbol": "KRE", "close": 67.7, "bar_date": "2026-05-14"}]
    )
    assert (_compute_importance(report, _news_metrics(report))
            == _compute_importance(base, _news_metrics(base)))


def test_importance_ignores_negative_news_on_non_fired_symbol():
    report = _mk_report(
        fires=[{"strategy_id": "x", "symbol": "KRE", "close": 67.7, "bar_date": "2026-05-14"}],
        news={"SPY": [_news_item("SPY", "negative")]},  # SPY not fired
    )
    base_imp = _compute_importance(_mk_report(fires=report.fires), {})
    assert _compute_importance(report, _news_metrics(report)) == base_imp


def test_against_news_tag_added():
    report = _mk_report(
        fires=[{"strategy_id": "x", "symbol": "KRE", "close": 67.7, "bar_date": "2026-05-14"}],
        news={"KRE": [_news_item("KRE", "negative")]},
    )
    finalize_report(report)
    assert "against-news" in report.tags


def test_news_heavy_tag_added_above_threshold():
    items = {f"S{i}": [_news_item(f"S{i}", "neutral")] * 4 for i in range(5)}
    report = _mk_report(news=items)
    finalize_report(report)
    assert "news-heavy" in report.tags


def test_no_news_means_no_news_tags():
    report = _mk_report(
        fires=[{"strategy_id": "x", "symbol": "KRE", "close": 67.7, "bar_date": "2026-05-14"}]
    )
    finalize_report(report)
    assert "against-news" not in report.tags
    assert "news-heavy" not in report.tags


def test_finalize_idempotent():
    report = _mk_report(
        fires=[{"strategy_id": "x", "symbol": "KRE", "close": 67.7, "bar_date": "2026-05-14"}],
        news={"KRE": [_news_item("KRE", "negative")]},
    )
    finalize_report(report)
    imp_a, tags_a = report.importance, sorted(report.tags)
    finalize_report(report)
    assert report.importance == imp_a
    assert sorted(report.tags) == tags_a
