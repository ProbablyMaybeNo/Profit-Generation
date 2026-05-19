"""
test_intraday_mr_tracked.py — 5.3.1: promote mean_reversion_intraday
to TRACKED_STRATEGIES.

Covers:
  - INTRADAY_MR_DECLARATIONS shape (bar_interval, active_on, flags)
  - declaration is included in TRACKED_STRATEGIES
  - compute_fn resolves through monitoring.strategy_fires._resolve_compute_fn
  - intraday_fires.intraday_strategies surfaces the 15m entry
  - signal generation against a crafted 15m bar fixture
  - seed_strategies promotes the new entry into the strategies table
"""

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import config as mcfg  # noqa: E402
from monitoring import intraday_fires as ifires  # noqa: E402
from monitoring import strategy_fires as sfires  # noqa: E402


def test_intraday_mr_declaration_shape():
    decls = mcfg.INTRADAY_MR_DECLARATIONS
    assert len(decls) == 1
    decl = decls[0]
    assert decl["id"] == "intraday-mr-3bar-low-15m"
    assert decl["compute"] == "compute_3bar_low_intraday"
    assert decl["bar_interval"] == "15m"
    assert decl["active_on"] == ["SPY", "QQQ", "IWM"]
    assert decl["grace_period"] is True
    assert decl["pyramidable"] is False
    assert decl["strategy_class"] == "mean_reversion"


def test_intraday_mr_in_tracked_strategies():
    ids = [e["id"] for e in mcfg.TRACKED_STRATEGIES]
    assert "intraday-mr-3bar-low-15m" in ids


def test_compute_fn_resolves():
    fn = sfires._resolve_compute_fn("compute_3bar_low_intraday")
    assert callable(fn)


def test_intraday_fires_surfaces_15m_entry():
    """The new 15m entry must show up in intraday_strategies()."""
    surfaced = ifires.intraday_strategies(mcfg.TRACKED_STRATEGIES)
    intraday_ids = [e["id"] for e in surfaced]
    assert "intraday-mr-3bar-low-15m" in intraday_ids
    # Everything surfaced must be non-1d.
    for e in surfaced:
        assert e.get("bar_interval", "1d") != "1d"


def _crafted_3bar_low_15m():
    """Frame where the LAST 15m bar breaks the prior-3-bar low.
    compute_3bar_low_intraday should fire long_entry on the last bar.
    """
    closes = [100.0, 100.5, 101.0, 100.7, 100.3, 100.1, 99.9, 99.8, 95.0]
    df = pd.DataFrame({
        "open":   closes,
        "high":   [p + 0.4 for p in closes],
        "low":    [p - 0.4 for p in closes],
        "close":  closes,
        "volume": [1_000_000] * len(closes),
    })
    df.index = pd.date_range("2026-05-14 09:30", periods=len(closes), freq="15min")
    return df


def test_signal_fires_on_crafted_15m_bar():
    fn = sfires._resolve_compute_fn("compute_3bar_low_intraday")
    df = _crafted_3bar_low_15m()
    out = fn(df)
    assert "long_entry" in out.columns
    assert bool(out["long_entry"].iloc[-1]) is True


def test_intraday_fires_commits_signal_via_full_pipeline(tmp_path, monkeypatch):
    """End-to-end: declaration + bars + fire-check → signals row committed."""
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)

    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {
        "strategy_id": "intraday-mr-3bar-low-15m",
    }})

    crafted = _crafted_3bar_low_15m()

    def fake_loader(symbols, interval, lookback, now=None):
        return {s: crafted for s in symbols}

    decls = [d for d in mcfg.TRACKED_STRATEGIES
             if d["id"] == "intraday-mr-3bar-low-15m"]
    from datetime import datetime
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 11, 0),
        declarations=decls,
        bar_loader=fake_loader,
        conn=conn,
        min_bars=5,
    )
    assert len(fires) >= 1
    entries = [f for f in fires if f["signal_type"] == "long_entry"]
    assert entries, f"expected long_entry in fires: {fires}"
    assert entries[0]["bar_interval"] == "15m"
    assert entries[0]["symbol"] in {"SPY", "QQQ", "IWM"}


def test_seed_strategies_promotes_intraday_mr(tmp_path, monkeypatch):
    """seed_strategies upserts TRACKED_STRATEGIES entries that don't come
    from records.jsonl (trend, intraday-mr)."""
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)

    from scripts import seed_strategies as ss
    monkeypatch.setattr(ss, "_load_records", lambda: [])
    rc = ss.main()
    assert rc == 0

    conn = db.init_db()
    row = conn.execute(
        "SELECT strategy_id, compute_fn, active_on_json "
        "FROM strategies WHERE strategy_id=?",
        ("intraday-mr-3bar-low-15m",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == "compute_3bar_low_intraday"
    import json
    assert sorted(json.loads(row[2])) == sorted(["SPY", "QQQ", "IWM"])
