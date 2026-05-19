"""
test_intraday_fires.py — 5.1.2: intraday strategy fire-check.
"""

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import intraday_fires as ifires  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_strategies(strategy_ids):
    conn = db.init_db()
    for sid in strategy_ids:
        db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    conn.close()


def _bars_5day_low_break(n: int, interval_min: int = 15) -> pd.DataFrame:
    """Build N intraday bars where the LAST bar breaks the 5-bar low.

    All bars at $100 except the last at $97 (below rolling-5 low of $100).
    """
    idx = pd.date_range(start="2026-05-14 09:30", periods=n,
                        freq=f"{interval_min}min")
    closes = [100.0] * (n - 1) + [97.0]
    highs = [c + 0.5 for c in closes]
    lows  = [c - 0.5 for c in closes]
    return pd.DataFrame({
        "open": closes, "high": highs, "low": lows,
        "close": closes, "volume": [10_000.0] * n,
    }, index=idx)


def _bars_no_fire(n: int = 60, interval_min: int = 15) -> pd.DataFrame:
    idx = pd.date_range(start="2026-05-14 09:30", periods=n,
                        freq=f"{interval_min}min")
    return pd.DataFrame({
        "open":   [100.0] * n,
        "high":   [100.5] * n,
        "low":    [99.5] * n,
        "close":  [100.0] * n,
        "volume": [10_000.0] * n,
    }, index=idx)


def _decl(strategy_id, compute, interval, symbols, *,
          active_in_window=None):
    out = {
        "id": strategy_id, "compute": compute,
        "bar_interval": interval, "active_on": symbols,
    }
    if active_in_window is not None:
        out["active_in_window"] = active_in_window
    return out


def test_intraday_strategies_filters_out_eod():
    decls = [
        {"id": "eod-a", "bar_interval": "1d", "active_on": ["SPY"]},
        {"id": "intraday-15m", "bar_interval": "15m", "active_on": ["SPY"]},
        {"id": "eod-default", "active_on": ["SPY"]},  # no bar_interval => 1d
        {"id": "intraday-5m",  "bar_interval": "5m",  "active_on": ["QQQ"]},
    ]
    result = ifires.intraday_strategies(decls)
    assert {e["id"] for e in result} == {"intraday-15m", "intraday-5m"}


def test_records_intraday_long_entry(isolated_db):
    _seed_strategies(["mr-intra-15m"])
    decls = [_decl("mr-intra-15m", "compute_5day_low", "15m", ["SPY"])]
    bars = _bars_5day_low_break(60, 15)
    def loader(symbols, interval, lookback, *, now):
        assert interval == "15m"
        return {s: bars for s in symbols if s == "SPY"}
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 14, 0),
        declarations=decls, bar_loader=loader,
    )
    entries = [f for f in fires if f["signal_type"] == "long_entry"]
    assert len(entries) == 1
    assert entries[0]["strategy_id"] == "mr-intra-15m"
    assert entries[0]["symbol"] == "SPY"
    assert entries[0]["bar_interval"] == "15m"
    assert entries[0]["close"] == 97.0
    assert entries[0]["signal_id"] is not None

    conn = db.connect(isolated_db)
    rows = conn.execute(
        "SELECT * FROM signals WHERE bar_interval='15m' AND signal_type='long_entry'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "SPY"
    assert rows[0]["close"] == 97.0
    assert rows[0]["strategy_id"] == "mr-intra-15m"


def test_idempotent_on_repeat_scan(isolated_db):
    _seed_strategies(["mr-intra-15m"])
    decls = [_decl("mr-intra-15m", "compute_5day_low", "15m", ["SPY"])]
    bars = _bars_5day_low_break(60, 15)
    def loader(symbols, interval, lookback, *, now):
        return {s: bars for s in symbols}
    asof = datetime(2026, 5, 14, 14, 0)
    a = ifires.check_intraday_fires(asof=asof, declarations=decls, bar_loader=loader)
    b = ifires.check_intraday_fires(asof=asof, declarations=decls, bar_loader=loader)
    a_entries = [f for f in a if f["signal_type"] == "long_entry" and f["signal_id"]]
    b_entries = [f for f in b if f["signal_type"] == "long_entry" and f["signal_id"]]
    assert len(a_entries) == 1
    assert len(b_entries) == 0  # UNIQUE constraint prevented dupe
    conn = db.connect(isolated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE bar_interval='15m'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_skips_crypto_symbols(isolated_db):
    decls = [_decl("mr-intra-15m", "compute_5day_low", "15m",
                   ["BTC-USD", "ETH-USD"])]
    called = []
    def loader(symbols, interval, lookback, *, now):
        called.append(symbols)
        return {}
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 14, 0),
        declarations=decls, bar_loader=loader,
    )
    assert fires == []
    # Loader called with no symbols (all filtered as crypto) — or not called.
    if called:
        assert called[0] == []


def test_no_fire_when_compute_returns_no_signal(isolated_db):
    decls = [_decl("mr-intra-15m", "compute_5day_low", "15m", ["SPY"])]
    flat = _bars_no_fire(60, 15)
    def loader(symbols, interval, lookback, *, now):
        return {s: flat for s in symbols}
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 14, 0),
        declarations=decls, bar_loader=loader,
    )
    assert all(f["signal_type"] != "long_entry" for f in fires)


def test_unresolvable_compute_fn_skipped(isolated_db):
    decls = [_decl("xyz", "compute_does_not_exist", "15m", ["SPY"])]
    def loader(symbols, interval, lookback, *, now):
        return {}
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 14, 0),
        declarations=decls, bar_loader=loader,
    )
    assert fires == []


def test_active_in_window_inside_window(isolated_db):
    _seed_strategies(["orb-5m"])
    decls = [_decl("orb-5m", "compute_5day_low", "5m", ["SPY"],
                   active_in_window="09:35-10:30")]
    bars = _bars_5day_low_break(60, 5)
    def loader(symbols, interval, lookback, *, now):
        return {s: bars for s in symbols}
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 10, 0),
        declarations=decls, bar_loader=loader,
    )
    entries = [f for f in fires if f["signal_type"] == "long_entry"]
    assert len(entries) == 1


def test_active_in_window_outside_window(isolated_db):
    decls = [_decl("orb-5m", "compute_5day_low", "5m", ["SPY"],
                   active_in_window="09:35-10:30")]
    bars = _bars_5day_low_break(60, 5)
    def loader(symbols, interval, lookback, *, now):
        return {s: bars for s in symbols}
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 14, 0),
        declarations=decls, bar_loader=loader,
    )
    assert fires == []


def test_iterates_multiple_intervals(isolated_db):
    _seed_strategies(["mr-intra-15m", "mr-intra-5m"])
    decls = [
        _decl("mr-intra-15m", "compute_5day_low", "15m", ["SPY"]),
        _decl("mr-intra-5m",  "compute_5day_low", "5m",  ["QQQ"]),
    ]
    bars_15 = _bars_5day_low_break(60, 15)
    bars_5  = _bars_5day_low_break(60, 5)
    intervals_seen = []
    def loader(symbols, interval, lookback, *, now):
        intervals_seen.append(interval)
        return {s: (bars_15 if interval == "15m" else bars_5) for s in symbols}
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 14, 0),
        declarations=decls, bar_loader=loader,
    )
    entries = [f for f in fires if f["signal_type"] == "long_entry"]
    assert {e["bar_interval"] for e in entries} == {"15m", "5m"}
    assert {e["strategy_id"] for e in entries} == {"mr-intra-15m", "mr-intra-5m"}
    assert sorted(intervals_seen) == ["15m", "5m"]


def test_signal_extra_json_recorded(isolated_db):
    _seed_strategies(["mr-intra-15m"])
    decls = [_decl("mr-intra-15m", "compute_5day_low", "15m", ["SPY"])]
    bars = _bars_5day_low_break(60, 15)
    def loader(symbols, interval, lookback, *, now):
        return {s: bars for s in symbols}
    ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 14, 0),
        declarations=decls, bar_loader=loader,
    )
    conn = db.connect(isolated_db)
    row = conn.execute(
        "SELECT extra_json FROM signals WHERE bar_interval='15m' "
        "AND signal_type='long_entry'"
    ).fetchone()
    conn.close()
    import json
    extra = json.loads(row["extra_json"])
    assert extra["bar_interval"] == "15m"
    assert extra["source"] == "intraday_fires"


def test_in_window_helper():
    f = ifires._in_window
    assert f(datetime(2026, 5, 14, 10, 0),  "09:35-10:30") is True
    assert f(datetime(2026, 5, 14, 9, 34),  "09:35-10:30") is False
    assert f(datetime(2026, 5, 14, 10, 30), "09:35-10:30") is True
    assert f(datetime(2026, 5, 14, 10, 31), "09:35-10:30") is False
    assert f(datetime(2026, 5, 14, 14, 0),  None) is True
    assert f(datetime(2026, 5, 14, 14, 0),  "bogus") is True
