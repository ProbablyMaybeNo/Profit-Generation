import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import liquidity  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


# ---------------------------------------------------------------------------
# DB schema + helpers
# ---------------------------------------------------------------------------


def test_liquidity_snapshots_table_created(isolated_db):
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            " WHERE type='table' AND name='liquidity_snapshots'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_upsert_liquidity_snapshot_inserts_and_updates(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_liquidity_snapshot(
            conn, symbol="aapl", as_of_date="2026-05-19",
            avg_dollar_volume_20d=1_234_567_890.0, last_close=192.50,
        )
        rows = conn.execute(
            "SELECT symbol, as_of_date, avg_dollar_volume_20d, last_close "
            "  FROM liquidity_snapshots"
        ).fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["symbol"] == "AAPL"
        assert r["as_of_date"] == "2026-05-19"
        assert r["avg_dollar_volume_20d"] == pytest.approx(1_234_567_890.0)
        assert r["last_close"] == pytest.approx(192.50)

        # Update by re-inserting same symbol
        db.upsert_liquidity_snapshot(
            conn, symbol="AAPL", as_of_date="2026-05-20",
            avg_dollar_volume_20d=2_000_000_000.0, last_close=195.0,
        )
        r2 = conn.execute(
            "SELECT as_of_date, avg_dollar_volume_20d, last_close "
            "  FROM liquidity_snapshots WHERE symbol='AAPL'"
        ).fetchone()
        assert r2["as_of_date"] == "2026-05-20"
        assert r2["avg_dollar_volume_20d"] == pytest.approx(2_000_000_000.0)
        assert r2["last_close"] == pytest.approx(195.0)
    finally:
        conn.close()


def test_get_liquidity_snapshots_returns_dict(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_liquidity_snapshot(
            conn, symbol="AAPL", as_of_date="2026-05-19",
            avg_dollar_volume_20d=10e9,
        )
        db.upsert_liquidity_snapshot(
            conn, symbol="XYZ", as_of_date="2026-05-19",
            avg_dollar_volume_20d=1e6,
        )
        snaps = db.get_liquidity_snapshots(conn, ["AAPL", "MISSING"])
        assert set(snaps.keys()) == {"AAPL"}
        assert snaps["AAPL"]["avg_dollar_volume_20d"] == pytest.approx(10e9)

        all_snaps = db.get_liquidity_snapshots(conn, None)
        assert set(all_snaps.keys()) == {"AAPL", "XYZ"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Filter math
# ---------------------------------------------------------------------------


def test_filter_by_dollar_volume_basic(isolated_db):
    conn = db.init_db()
    try:
        today = date(2026, 5, 19).isoformat()
        db.upsert_liquidity_snapshot(
            conn, symbol="AAPL", as_of_date=today,
            avg_dollar_volume_20d=10_000_000_000.0,
        )
        db.upsert_liquidity_snapshot(
            conn, symbol="MSFT", as_of_date=today,
            avg_dollar_volume_20d=8_000_000_000.0,
        )
        db.upsert_liquidity_snapshot(
            conn, symbol="TINY", as_of_date=today,
            avg_dollar_volume_20d=1_000_000.0,  # below threshold
        )

        out = liquidity.filter_by_dollar_volume(
            ["AAPL", "MSFT", "TINY", "MISSING"],
            min_usd=50_000_000.0,
            conn=conn,
            as_of=date(2026, 5, 19),
        )
        assert out == ["AAPL", "MSFT"]
    finally:
        conn.close()


def test_filter_by_dollar_volume_missing_excluded(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_liquidity_snapshot(
            conn, symbol="AAPL", as_of_date=date(2026, 5, 19).isoformat(),
            avg_dollar_volume_20d=10e9,
        )
        out = liquidity.filter_by_dollar_volume(
            ["AAPL", "NEVERSEEN"], conn=conn, as_of=date(2026, 5, 19),
        )
        assert out == ["AAPL"]
    finally:
        conn.close()


def test_filter_by_dollar_volume_stale_excluded(isolated_db):
    conn = db.init_db()
    try:
        old = date(2026, 1, 1).isoformat()
        db.upsert_liquidity_snapshot(
            conn, symbol="AAPL", as_of_date=old,
            avg_dollar_volume_20d=10e9,
        )
        out = liquidity.filter_by_dollar_volume(
            ["AAPL"],
            conn=conn,
            as_of=date(2026, 5, 19),
            max_staleness_days=7,
        )
        assert out == []
    finally:
        conn.close()


def test_filter_by_dollar_volume_dedupes_and_uppercases(isolated_db):
    conn = db.init_db()
    try:
        today = date(2026, 5, 19).isoformat()
        db.upsert_liquidity_snapshot(
            conn, symbol="AAPL", as_of_date=today,
            avg_dollar_volume_20d=10e9,
        )
        out = liquidity.filter_by_dollar_volume(
            ["aapl", "AAPL", "aapl"], conn=conn, as_of=date(2026, 5, 19),
        )
        assert out == ["AAPL"]
    finally:
        conn.close()


def test_filter_by_dollar_volume_empty_input(isolated_db):
    assert liquidity.filter_by_dollar_volume([]) == []


# ---------------------------------------------------------------------------
# ADV math
# ---------------------------------------------------------------------------


def _make_bars(closes, volumes):
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": volumes,
    })


def test_compute_avg_dollar_volume_basic():
    closes = [100.0] * 20
    volumes = [1_000_000] * 20
    df = _make_bars(closes, volumes)
    adv = liquidity.compute_avg_dollar_volume(df, window=20)
    assert adv == pytest.approx(100.0 * 1_000_000)


def test_compute_avg_dollar_volume_uses_last_n_bars():
    closes = [50.0] * 30 + [100.0] * 20  # last 20 are higher
    volumes = [1_000_000] * 30 + [1_000_000] * 20
    df = _make_bars(closes, volumes)
    adv = liquidity.compute_avg_dollar_volume(df, window=20)
    assert adv == pytest.approx(100.0 * 1_000_000)


def test_compute_avg_dollar_volume_insufficient_bars():
    df = _make_bars([100.0] * 10, [1_000_000] * 10)
    assert liquidity.compute_avg_dollar_volume(df, window=20) is None


def test_compute_avg_dollar_volume_handles_none():
    assert liquidity.compute_avg_dollar_volume(None) is None


# ---------------------------------------------------------------------------
# populate_liquidity_snapshots
# ---------------------------------------------------------------------------


def test_populate_liquidity_snapshots_writes_rows(isolated_db):
    conn = db.init_db()
    try:
        bars = {
            "AAPL": _make_bars([100.0] * 25, [10_000_000] * 25),
            "TINY": _make_bars([1.0] * 25, [100] * 25),
        }
        def fake_loader(syms, start, end, interval):
            return {s: bars[s] for s in syms if s in bars}

        result = liquidity.populate_liquidity_snapshots(
            ["AAPL", "TINY"],
            bar_loader=fake_loader,
            conn=conn,
            as_of=date(2026, 5, 19),
        )
        assert "AAPL" in result and "TINY" in result
        assert result["AAPL"][0] == pytest.approx(1_000_000_000.0)
        assert result["TINY"][0] == pytest.approx(100.0)

        snaps = db.get_liquidity_snapshots(conn, ["AAPL", "TINY"])
        assert snaps["AAPL"]["avg_dollar_volume_20d"] == pytest.approx(1_000_000_000.0)
        assert snaps["AAPL"]["last_close"] == pytest.approx(100.0)
    finally:
        conn.close()


def test_populate_liquidity_snapshots_skips_short_bars(isolated_db):
    conn = db.init_db()
    try:
        # Only 10 bars — below the 20-day window
        bars = {"AAPL": _make_bars([100.0] * 10, [10_000_000] * 10)}
        def loader(syms, start, end, interval): return bars

        result = liquidity.populate_liquidity_snapshots(
            ["AAPL"], bar_loader=loader, conn=conn, as_of=date(2026, 5, 19),
        )
        assert result == {}
        snaps = db.get_liquidity_snapshots(conn, ["AAPL"])
        assert snaps == {}
    finally:
        conn.close()


def test_populate_liquidity_snapshots_loader_failure_returns_empty(isolated_db):
    conn = db.init_db()
    try:
        def loader(syms, start, end, interval):
            raise RuntimeError("network down")

        result = liquidity.populate_liquidity_snapshots(
            ["AAPL"], bar_loader=loader, conn=conn,
        )
        assert result == {}
    finally:
        conn.close()


def test_populate_then_filter_end_to_end(isolated_db):
    conn = db.init_db()
    try:
        bars = {
            "AAPL": _make_bars([200.0] * 25, [10_000_000] * 25),   # $2B/day
            "MSFT": _make_bars([400.0] * 25, [5_000_000] * 25),    # $2B/day
            "TINY": _make_bars([5.0] * 25, [50_000] * 25),         # $250k/day
        }
        def loader(syms, start, end, interval):
            return {s: bars[s] for s in syms}

        liquidity.populate_liquidity_snapshots(
            ["AAPL", "MSFT", "TINY"], bar_loader=loader, conn=conn,
            as_of=date(2026, 5, 19),
        )
        passed = liquidity.filter_by_dollar_volume(
            ["AAPL", "MSFT", "TINY"],
            min_usd=50_000_000,
            conn=conn,
            as_of=date(2026, 5, 19),
        )
        assert passed == ["AAPL", "MSFT"]
    finally:
        conn.close()
