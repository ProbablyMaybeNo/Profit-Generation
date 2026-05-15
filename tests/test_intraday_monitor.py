import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import intraday_monitor as im  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _daily_frame(end_date: date, n: int, prices) -> pd.DataFrame:
    """Build n daily OHLCV bars ending the day BEFORE end_date (so today is the synthesized bar)."""
    if not isinstance(prices, list):
        prices = [prices] * n
    assert len(prices) == n
    idx = pd.date_range(end=end_date - timedelta(days=1), periods=n, freq="D")
    return pd.DataFrame({
        "open":   prices,
        "high":   [p * 1.01 for p in prices],
        "low":    [p * 0.99 for p in prices],
        "close":  prices,
        "volume": [1_000_000] * n,
    }, index=idx)


def _minute_frame(open_p, high_p, low_p, close_p, vol_total, n_bars=10) -> pd.DataFrame:
    idx = pd.date_range(start="2026-05-14 09:30", periods=n_bars, freq="1min")
    closes = list(pd.Series([open_p, *([close_p] * (n_bars - 1))]))
    return pd.DataFrame({
        "open":   [open_p] * n_bars,
        "high":   [high_p] + [close_p] * (n_bars - 1),
        "low":    [low_p] + [close_p] * (n_bars - 1),
        "close":  closes,
        "volume": [vol_total / n_bars] * n_bars,
    }, index=idx)


def _make_loaders(daily_by_symbol, minute_by_symbol=None):
    minute_by_symbol = minute_by_symbol or {}
    def daily_loader(symbols, *, start, end, interval, source):
        if interval != "1d":
            return {}
        return {s: daily_by_symbol[s] for s in symbols if s in daily_by_symbol}
    def minute_loader(symbols, *, start, end, interval, source):
        if interval != "1m":
            return {}
        return {s: minute_by_symbol[s] for s in symbols if s in minute_by_symbol}
    return daily_loader, minute_loader


def test_synthesize_today_bar_aggregates_minutes():
    minute = _minute_frame(open_p=100.0, high_p=105.0, low_p=99.0,
                           close_p=103.0, vol_total=1000.0, n_bars=10)
    _, m_loader = _make_loaders({}, {"GDX": minute})
    bar = im.synthesize_today_bar("GDX", asof=datetime(2026, 5, 14, 14, 0),
                                  minute_loader=m_loader)
    assert bar is not None
    assert bar["open"] == 100.0
    assert bar["high"] == 105.0
    assert bar["low"] == 99.0
    assert bar["close"] == 103.0
    assert bar["volume"] == pytest.approx(1000.0)
    assert bar.name == pd.Timestamp(date(2026, 5, 14))


def test_synthesize_today_bar_returns_none_on_empty():
    _, m_loader = _make_loaders({}, {})
    assert im.synthesize_today_bar("ZZZ", asof=datetime.now(), minute_loader=m_loader) is None


def test_blended_drops_yfinance_dup():
    daily = _daily_frame(date(2026, 5, 14), 30, 100.0)
    today_dup_idx = pd.Timestamp(date(2026, 5, 14))
    daily.loc[today_dup_idx] = {"open": 99.0, "high": 99.5, "low": 98.5, "close": 99.0, "volume": 1.0}
    daily = daily.sort_index()
    minute = _minute_frame(open_p=101.0, high_p=102.0, low_p=100.5,
                           close_p=101.5, vol_total=500.0, n_bars=5)
    d_loader, m_loader = _make_loaders({"GDX": daily}, {"GDX": minute})
    blended = im.blended_daily_history("GDX", datetime(2026, 5, 14, 12, 0),
                                       daily_loader=d_loader, minute_loader=m_loader)
    today = blended.loc[today_dup_idx]
    assert today["open"] == 101.0
    assert today["close"] == 101.5
    assert (blended.index.duplicated()).sum() == 0


def test_blended_handles_missing_minutes():
    daily = _daily_frame(date(2026, 5, 14), 30, 100.0)
    d_loader, m_loader = _make_loaders({"GDX": daily}, {})
    blended = im.blended_daily_history("GDX", datetime(2026, 5, 14, 12, 0),
                                       daily_loader=d_loader, minute_loader=m_loader)
    assert blended is not None
    assert pd.Timestamp(date(2026, 5, 14)) not in blended.index


def _seed_one_strategy(active_on, fn_name="compute_5day_low"):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "test-strat",
                                        "compute_fn": fn_name}})
    db.set_strategy_active_on(conn, "test-strat", active_on, compute_fn=fn_name)
    conn.close()


def test_scan_once_records_long_entry_when_today_breaks_5day_low(isolated_db):
    _seed_one_strategy(["GDX"])
    # 30 daily bars all at $100 (so 5-day-low = ~99). Today closes at $97.
    daily = _daily_frame(date(2026, 5, 14), 30, 100.0)
    minute = _minute_frame(open_p=99.5, high_p=99.6, low_p=96.5,
                           close_p=97.0, vol_total=2_000_000, n_bars=10)
    d_loader, m_loader = _make_loaders({"GDX": daily}, {"GDX": minute})
    alerts = []
    counts = im.scan_once(asof=datetime(2026, 5, 14, 15, 30),
                          daily_loader=d_loader, minute_loader=m_loader,
                          alerter=lambda msg: alerts.append(msg))
    assert counts["fires"] == 1
    assert counts["evaluated"] >= 1
    assert any("FIRE" in a and "GDX" in a for a in alerts)
    conn = db.connect(isolated_db)
    rows = conn.execute(
        "SELECT * FROM signals WHERE bar_interval='1d-intraday' AND signal_type='long_entry'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "GDX"
    assert rows[0]["close"] == 97.0


def test_scan_once_idempotent_for_same_day(isolated_db):
    _seed_one_strategy(["GDX"])
    daily = _daily_frame(date(2026, 5, 14), 30, 100.0)
    minute = _minute_frame(99.5, 99.6, 96.5, 97.0, 2_000_000, 10)
    d_loader, m_loader = _make_loaders({"GDX": daily}, {"GDX": minute})
    alerts = []
    asof = datetime(2026, 5, 14, 15, 30)
    a = im.scan_once(asof=asof, daily_loader=d_loader, minute_loader=m_loader,
                    alerter=lambda m: alerts.append(m))
    b = im.scan_once(asof=asof, daily_loader=d_loader, minute_loader=m_loader,
                    alerter=lambda m: alerts.append(m))
    assert a["fires"] == 1
    assert b["fires"] == 0  # already recorded; UNIQUE prevents double-insert
    conn = db.connect(isolated_db)
    n = conn.execute("SELECT COUNT(*) FROM signals "
                     "WHERE bar_interval='1d-intraday'").fetchone()[0]
    conn.close()
    assert n == 1
    assert sum(1 for m in alerts if "FIRE" in m) == 1


def test_scan_once_skips_crypto(isolated_db):
    _seed_one_strategy(["BTC-USD"])
    d_loader, m_loader = _make_loaders({}, {})
    counts = im.scan_once(asof=datetime(2026, 5, 14, 15, 30),
                          daily_loader=d_loader, minute_loader=m_loader,
                          alerter=lambda m: None)
    assert counts["evaluated"] == 0
    assert counts["fires"] == 0


def test_scan_once_skips_unresolvable_compute_fn(isolated_db):
    _seed_one_strategy(["GDX"], fn_name="does_not_exist_xyz")
    d_loader, m_loader = _make_loaders({}, {})
    counts = im.scan_once(asof=datetime(2026, 5, 14, 15, 30),
                          daily_loader=d_loader, minute_loader=m_loader,
                          alerter=lambda m: None)
    assert counts["skipped_strategies"] == 1
    assert counts["evaluated"] == 0


def test_scan_once_handles_no_bars(isolated_db):
    _seed_one_strategy(["XLE"])
    d_loader, m_loader = _make_loaders({}, {})
    counts = im.scan_once(asof=datetime(2026, 5, 14, 15, 30),
                          daily_loader=d_loader, minute_loader=m_loader,
                          alerter=lambda m: None)
    assert counts["skipped_no_bars"] == 1
    assert counts["evaluated"] == 0


def test_scan_once_records_long_exit_when_close_above_prev_high(isolated_db):
    """5day_low exit fires when close > prev_bar_high."""
    _seed_one_strategy(["XHB"])
    # 30 daily bars at $100 (prev high ~ 101). Today closes at $103 → exit fires.
    daily = _daily_frame(date(2026, 5, 14), 30, 100.0)
    minute = _minute_frame(102.0, 103.5, 102.0, 103.0, 1_500_000, 10)
    d_loader, m_loader = _make_loaders({"XHB": daily}, {"XHB": minute})
    counts = im.scan_once(asof=datetime(2026, 5, 14, 15, 30),
                          daily_loader=d_loader, minute_loader=m_loader,
                          alerter=lambda m: None)
    assert counts["exits"] == 1
    conn = db.connect(isolated_db)
    rows = conn.execute(
        "SELECT * FROM signals WHERE bar_interval='1d-intraday' AND signal_type='long_exit'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "XHB"
