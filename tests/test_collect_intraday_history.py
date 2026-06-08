import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import collect_intraday_history as cih  # noqa: E402


def _frame(n=10, base=100.0, start="2026-01-02 09:30"):
    idx = pd.date_range(start=start, periods=n, freq="5min")
    closes = [base + i for i in range(n)]
    return pd.DataFrame({
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000 + i for i in range(n)],
    }, index=idx)


def test_coverage_for_counts_bars_and_range():
    df = _frame(n=5)
    cov = cih.coverage_for(df)
    assert cov["bars"] == 5
    assert cov["start"].startswith("2026-01-02 09:30")
    assert cov["end"].startswith("2026-01-02 09:50")


def test_coverage_for_empty_is_zero_not_crash():
    assert cih.coverage_for(None) == {"bars": 0, "start": None, "end": None}
    assert cih.coverage_for(pd.DataFrame()) == {"bars": 0, "start": None,
                                                "end": None}


def test_coverage_report_includes_missing_symbols_as_zero():
    data = {"AMD": _frame(n=3)}
    rows = cih.coverage_report(data, ["AMD", "TSLA"])
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["AMD"]["bars"] == 3
    # TSLA absent from data -> explicit zero row, not omitted
    assert by_sym["TSLA"]["bars"] == 0
    assert by_sym["TSLA"]["start"] is None


def test_to_et_naive_converts_utc_index_and_orders():
    idx = pd.to_datetime([
        "2026-01-02 14:35:00+00:00",
        "2026-01-02 14:30:00+00:00",
    ])
    df = pd.DataFrame({
        "open": [1.0, 2.0], "high": [1, 2], "low": [1, 2],
        "close": [1.0, 2.0], "volume": [10, 20],
    }, index=idx)
    out = cih._to_et_naive(df)
    # tz stripped, sorted ascending, 14:30 UTC -> 09:30 ET
    assert out.index.tz is None
    assert list(out.index.hour) == [9, 9]
    assert list(out.index.minute) == [30, 35]
    assert list(out["open"]) == [2.0, 1.0]  # reordered by ts


def test_split_multi_keys_by_symbol():
    idx = pd.MultiIndex.from_product(
        [["AMD", "TSLA"],
         pd.to_datetime(["2026-01-02 14:30:00+00:00",
                         "2026-01-02 14:35:00+00:00"])],
        names=["symbol", "timestamp"],
    )
    df = pd.DataFrame({
        "open": [1, 2, 3, 4], "high": [1, 2, 3, 4], "low": [1, 2, 3, 4],
        "close": [1, 2, 3, 4], "volume": [1, 2, 3, 4],
    }, index=idx)
    out = cih._split_multi(df)
    assert set(out) == {"AMD", "TSLA"}
    assert len(out["AMD"]) == 2
    assert out["AMD"].index.tz is None


def test_split_multi_empty():
    assert cih._split_multi(pd.DataFrame()) == {}


def test_collect_interval_uses_injected_fetcher_and_drops_empty():
    captured = {}

    def fake_fetcher(symbols, interval, start, end):
        captured["symbols"] = symbols
        captured["interval"] = interval
        captured["span_days"] = (end - start).days
        return {"AMD": _frame(n=4), "TSLA": pd.DataFrame()}

    out = cih.collect_interval(
        ["AMD", "TSLA"], "5m", months=9,
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
        fetcher=fake_fetcher,
    )
    assert set(out) == {"AMD"}  # empty TSLA dropped
    assert captured["interval"] == "5m"
    assert 9 * 31 - 2 <= captured["span_days"] <= 9 * 31 + 1


def test_collect_interval_rejects_bad_interval():
    with pytest.raises(ValueError):
        cih.collect_interval(["AMD"], "3m", fetcher=lambda *a: {})


def test_write_cache_roundtrip(tmp_path):
    data = {"AMD": _frame(n=6)}
    path = cih.write_cache(data, "5m", root=tmp_path)
    assert path.exists()
    import pickle
    with open(path, "rb") as fh:
        loaded = pickle.load(fh)
    assert set(loaded) == {"AMD"}
    pd.testing.assert_frame_equal(loaded["AMD"], data["AMD"])


def test_out_path_template():
    p = cih.out_path("15m", root=Path("/x"))
    assert p.name == "intraday_history_15m.pkl"
    assert p.parent.name == "data"
