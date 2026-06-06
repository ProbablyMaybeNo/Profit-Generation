import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import trend_scanner  # noqa: E402


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


# ---------------------------------------------------------------------------
# Helpers — synthetic bars with known breakout shape
# ---------------------------------------------------------------------------


def _flat_then_breakout_bars(n: int = 60, breakout_close: float = 150.0) -> pd.DataFrame:
    """Generate daily bars: 50 flat sessions at ~100, then 10 ascending,
    so a Donchian-20 channel breakout fires on the last bar."""
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    closes = np.concatenate([
        np.full(n - 10, 100.0),
        np.linspace(105.0, breakout_close, 10),
    ])
    highs = closes + 0.5
    lows = closes - 0.5
    return pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.full(n, 10_000_000),
    }, index=idx)


def _flat_bars(n: int = 60) -> pd.DataFrame:
    """Generate bars with no breakout — nothing should fire."""
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": [100.0] * n,
        "high": [100.5] * n,
        "low": [99.5] * n,
        "close": [100.0] * n,
        "volume": [10_000_000] * n,
    }, index=idx)


# ---------------------------------------------------------------------------
# Filter / class detection
# ---------------------------------------------------------------------------


def test_trend_strategies_filters_by_class():
    decls = [
        {"id": "trend-a", "strategy_class": "trend"},
        {"id": "mr-b", "strategy_class": "mean_reversion"},
        {"id": "trend-c", "strategy_class": "trend"},
        {"id": "no-class"},
    ]
    out = trend_scanner.trend_strategies(decls)
    ids = [e["id"] for e in out]
    assert ids == ["trend-a", "trend-c"]


# ---------------------------------------------------------------------------
# End-to-end scan against fixture data
# ---------------------------------------------------------------------------


def test_scan_records_fires_on_breakout(isolated_db):
    _seed_strategies(["trend-donchian-breakout-20"])
    conn = db.init_db()
    try:
        bars = {
            "BREAKOUT": _flat_then_breakout_bars(),
            "FLAT": _flat_bars(),
        }
        def loader(symbols, lookback): return {s: bars[s] for s in symbols if s in bars}

        decls = [{
            "id": "trend-donchian-breakout-20",
            "compute": "compute_donchian_breakout_20",
            "strategy_class": "trend",
            "active_on": ["SPY"],
        }]

        result = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_override=["BREAKOUT", "FLAT"],
            bar_loader=loader,
            conn=conn,
        )

        entries = [r for r in result if r["signal_type"] == "long_entry"]
        symbols_fired = {r["symbol"] for r in entries}
        assert "BREAKOUT" in symbols_fired
        assert "FLAT" not in symbols_fired

        # Persisted to signals table
        rows = conn.execute(
            "SELECT strategy_id, symbol, signal_type, bar_interval "
            "  FROM signals WHERE strategy_id='trend-donchian-breakout-20'"
        ).fetchall()
        assert any(r["symbol"] == "BREAKOUT"
                   and r["signal_type"] == "long_entry"
                   and r["bar_interval"] == "1d"
                   for r in rows)
    finally:
        conn.close()


def test_scan_bypasses_active_on_field(isolated_db):
    """Crucial: trend strategies have active_on=[SPY/QQQ/IWM] but the wide
    scan must include any symbol from the universe override."""
    _seed_strategies(["trend-donchian-breakout-20"])
    conn = db.init_db()
    try:
        bars = {"NEWSYM": _flat_then_breakout_bars()}
        def loader(symbols, lookback): return {s: bars[s] for s in symbols if s in bars}

        decls = [{
            "id": "trend-donchian-breakout-20",
            "compute": "compute_donchian_breakout_20",
            "strategy_class": "trend",
            "active_on": ["SPY", "QQQ", "IWM"],  # narrow
        }]

        result = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_override=["NEWSYM"],  # not in active_on
            bar_loader=loader,
            conn=conn,
        )

        symbols = {r["symbol"] for r in result if r["signal_type"] == "long_entry"}
        assert "NEWSYM" in symbols
    finally:
        conn.close()


def test_scan_idempotent_same_bar_double_run(isolated_db):
    _seed_strategies(["trend-donchian-breakout-20"])
    conn = db.init_db()
    try:
        bars = {"BREAKOUT": _flat_then_breakout_bars()}
        def loader(symbols, lookback): return bars

        decls = [{
            "id": "trend-donchian-breakout-20",
            "compute": "compute_donchian_breakout_20",
            "strategy_class": "trend",
            "active_on": ["SPY"],
        }]

        r1 = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_override=["BREAKOUT"],
            bar_loader=loader,
            conn=conn,
        )
        r2 = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_override=["BREAKOUT"],
            bar_loader=loader,
            conn=conn,
        )

        # First run inserts, second run sees dupe (signal_id=None)
        first_ids = [r["signal_id"] for r in r1 if r["signal_type"] == "long_entry"]
        second_ids = [r["signal_id"] for r in r2 if r["signal_type"] == "long_entry"]
        assert all(i is not None for i in first_ids)
        assert all(i is None for i in second_ids)

        # Only one row in signals table for this (strategy, symbol, bar_ts)
        count = conn.execute(
            "SELECT COUNT(*) FROM signals "
            " WHERE strategy_id='trend-donchian-breakout-20' "
            "   AND symbol='BREAKOUT' AND signal_type='long_entry'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_scan_ignores_non_trend_strategies(isolated_db):
    _seed_strategies(["trend-donchian-breakout-20"])
    conn = db.init_db()
    try:
        bars = {"X": _flat_then_breakout_bars()}
        def loader(symbols, lookback): return bars

        decls = [
            {"id": "mr-thing", "strategy_class": "mean_reversion",
             "compute": "compute_donchian_breakout_20", "active_on": ["SPY"]},
        ]

        result = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_override=["X"],
            bar_loader=loader,
            conn=conn,
        )
        assert result == []
    finally:
        conn.close()


def test_scan_skips_symbols_with_too_few_bars(isolated_db):
    _seed_strategies(["trend-donchian-breakout-20"])
    conn = db.init_db()
    try:
        bars = {"SHORT": _flat_then_breakout_bars(n=10)}  # < MIN_BARS_REQUIRED
        def loader(symbols, lookback): return bars

        decls = [{
            "id": "trend-donchian-breakout-20",
            "compute": "compute_donchian_breakout_20",
            "strategy_class": "trend",
            "active_on": ["SPY"],
        }]

        result = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_override=["SHORT"],
            bar_loader=loader,
            conn=conn,
        )
        assert result == []
    finally:
        conn.close()


def test_scan_continues_when_one_compute_raises(isolated_db):
    _seed_strategies(["trend-donchian-breakout-20"])
    conn = db.init_db()
    try:
        bars = {
            "GOOD": _flat_then_breakout_bars(),
            "BAD": pd.DataFrame({"close": ["not-a-number"] * 60,
                                  "high": ["x"] * 60, "low": ["x"] * 60,
                                  "open": ["x"] * 60, "volume": ["x"] * 60},
                                 index=pd.date_range("2026-01-01", periods=60, freq="B")),
        }
        def loader(symbols, lookback): return bars

        decls = [{
            "id": "trend-donchian-breakout-20",
            "compute": "compute_donchian_breakout_20",
            "strategy_class": "trend",
            "active_on": ["SPY"],
        }]

        result = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_override=["GOOD", "BAD"],
            bar_loader=loader,
            conn=conn,
        )
        # GOOD still fires, BAD silently skipped
        fired = {r["symbol"] for r in result if r["signal_type"] == "long_entry"}
        assert "GOOD" in fired
    finally:
        conn.close()


def test_scan_applies_universe_loader_and_liquidity_filter(isolated_db):
    _seed_strategies(["trend-donchian-breakout-20"])
    conn = db.init_db()
    try:
        loader_calls = []
        filter_calls = []

        def fake_universe_loader():
            loader_calls.append(True)
            return ["BREAKOUT", "TINY", "FLAT"]

        def fake_liquidity_filter(symbols, *, min_usd):
            filter_calls.append((tuple(symbols), min_usd))
            # Drop TINY as illiquid
            return [s for s in symbols if s != "TINY"]

        bars = {
            "BREAKOUT": _flat_then_breakout_bars(),
            "FLAT": _flat_bars(),
        }
        def bar_loader(symbols, lookback):
            assert "TINY" not in symbols  # liquidity filter must have run
            return {s: bars[s] for s in symbols if s in bars}

        decls = [{
            "id": "trend-donchian-breakout-20",
            "compute": "compute_donchian_breakout_20",
            "strategy_class": "trend",
            "active_on": ["SPY"],
        }]

        result = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_loader=fake_universe_loader,
            liquidity_filter=fake_liquidity_filter,
            bar_loader=bar_loader,
            conn=conn,
            min_usd=50_000_000.0,
        )
        assert loader_calls == [True]
        assert filter_calls[0][1] == 50_000_000.0
        fired = {r["symbol"] for r in result if r["signal_type"] == "long_entry"}
        assert "BREAKOUT" in fired and "TINY" not in fired


    finally:
        conn.close()


def test_scan_empty_universe_after_filter_returns_no_fires(isolated_db):
    conn = db.init_db()
    try:
        def empty_filter(symbols, *, min_usd): return []
        def loader(symbols, lookback): return {}

        decls = [{
            "id": "trend-donchian-breakout-20",
            "compute": "compute_donchian_breakout_20",
            "strategy_class": "trend",
            "active_on": ["SPY"],
        }]

        result = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_loader=lambda: ["A", "B", "C"],
            liquidity_filter=empty_filter,
            bar_loader=loader,
            conn=conn,
        )
        assert result == []
    finally:
        conn.close()


def test_scan_persists_bar_ts_as_date_only_string(isolated_db):
    """Regression: trend_scanner was persisting bar_ts as a full
    ISO timestamp (YYYY-MM-DDTHH:MM:SS) but auto_trader.process_signals
    matches 1d signals against `bar_ts = asof.isoformat()` which yields
    YYYY-MM-DD. The mismatch silently dropped every scanner fire on the
    floor. This test pins the date-only format so it can't regress."""
    _seed_strategies(["trend-donchian-breakout-20"])
    conn = db.init_db()
    try:
        bars = {"BREAKOUT": _flat_then_breakout_bars()}
        def loader(symbols, lookback): return bars
        decls = [{
            "id": "trend-donchian-breakout-20",
            "compute": "compute_donchian_breakout_20",
            "strategy_class": "trend",
            "active_on": ["SPY"],
        }]
        result = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_override=["BREAKOUT"],
            bar_loader=loader,
            conn=conn,
        )
        assert len(result) >= 1
        # Every fire's bar_ts must be exactly 10 chars (YYYY-MM-DD), no 'T'
        for r in result:
            assert "T" not in r["bar_ts"], f"bar_ts has time component: {r['bar_ts']!r}"
            assert len(r["bar_ts"]) == 10, f"bar_ts not date-only: {r['bar_ts']!r}"
        # Same for the persisted DB row
        rows = conn.execute(
            "SELECT bar_ts FROM signals WHERE strategy_id='trend-donchian-breakout-20'"
        ).fetchall()
        for row in rows:
            assert "T" not in row["bar_ts"]
            assert len(row["bar_ts"]) == 10
    finally:
        conn.close()


def test_scan_records_long_exit_when_signal_fires(isolated_db):
    """Sanity check: build bars that produce both an entry and a later exit.

    M4 (Sprint 3): an exit is only RECORDED when the strategy OWNS the symbol
    (holds an open buy). Seed that owned position so the legitimate exit lands —
    a positionless scan would (correctly) record no exit.
    """
    _seed_strategies(["trend-donchian-breakout-20"])
    conn = db.init_db()
    try:
        # M4 — the strategy must own ROUNDTRIP for its exit to be recordable.
        conn.execute(
            "INSERT INTO paper_trades "
            "(alpaca_order_id, strategy_id, symbol, side, qty, status, "
            " submitted_at) VALUES "
            "('b-rt', 'trend-donchian-breakout-20', 'ROUNDTRIP', 'buy', 10, "
            " 'filled', '2026-01-01T00:00:00')"
        )
        conn.commit()
        # 80 bars: flat -> spike -> crash so Donchian fires entry then exit
        idx = pd.date_range("2026-01-01", periods=80, freq="B")
        closes = np.concatenate([
            np.full(40, 100.0),
            np.linspace(105.0, 150.0, 20),
            np.linspace(150.0, 50.0, 20),
        ])
        bars = {
            "ROUNDTRIP": pd.DataFrame({
                "open": closes, "high": closes + 0.5, "low": closes - 0.5,
                "close": closes, "volume": [10_000_000] * 80,
            }, index=idx),
        }
        def loader(symbols, lookback): return bars

        decls = [{
            "id": "trend-donchian-breakout-20",
            "compute": "compute_donchian_breakout_20",
            "strategy_class": "trend",
            "active_on": ["SPY"],
        }]

        result = trend_scanner.scan_trend_universe(
            declarations=decls,
            universe_override=["ROUNDTRIP"],
            bar_loader=loader,
            conn=conn,
        )

        # On the LAST bar (crash bottom), we should record a long_exit
        types = {r["signal_type"] for r in result}
        assert "long_exit" in types
    finally:
        conn.close()
