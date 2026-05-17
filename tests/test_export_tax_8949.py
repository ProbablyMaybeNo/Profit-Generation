"""Tests for scripts.export_tax_8949 — milestone 3.5.4."""

import csv
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from scripts import export_tax_8949 as ex  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_round_trip(
    conn, *, strategy_id, symbol, qty, buy_price, sell_price,
    buy_iso, sell_iso, signal_close=100.0,
):
    """One buy + one sell pair on the same signal_id."""
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    sid = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=buy_iso[:10], signal_type="long_entry",
        close=signal_close, bar_interval="1d",
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": f"buy-{sid}",
        "signal_id": sid, "strategy_id": strategy_id, "symbol": symbol,
        "side": "buy", "qty": qty, "order_type": "market",
        "submitted_at": f"{buy_iso}T14:30Z",
        "filled_at": f"{buy_iso}T14:30:05Z",
        "fill_price": buy_price, "status": "filled",
    })
    db.record_paper_trade(conn, {
        "alpaca_order_id": f"sell-{sid}",
        "signal_id": sid, "strategy_id": strategy_id, "symbol": symbol,
        "side": "sell", "qty": qty, "order_type": "market",
        "submitted_at": f"{sell_iso}T20:00Z",
        "filled_at": f"{sell_iso}T20:00:05Z",
        "fill_price": sell_price, "status": "filled",
    })
    return sid


# ---------- closed_round_trips ----------

def test_no_round_trips_when_db_empty(isolated_db):
    conn = db.init_db()
    assert ex.closed_round_trips(conn) == []


def test_round_trip_round_trips_buy_and_sell(isolated_db):
    conn = db.init_db()
    _seed_round_trip(conn, strategy_id="s1", symbol="GDX",
                      qty=10, buy_price=70.0, sell_price=72.5,
                      buy_iso="2026-03-01", sell_iso="2026-03-05")
    rts = ex.closed_round_trips(conn)
    assert len(rts) == 1
    rt = rts[0]
    assert rt["symbol"] == "GDX"
    assert rt["qty"] == 10
    assert rt["cost_basis"] == pytest.approx(700.0)
    assert rt["proceeds"] == pytest.approx(725.0)
    assert rt["gain_loss"] == pytest.approx(25.0)
    assert rt["hold_days"] == 4


def test_open_position_excluded_from_round_trips(isolated_db):
    """A buy without a matching sell must NOT show up in the export."""
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    sid = db.record_signal(conn, strategy_id="s1", symbol="GDX",
                            bar_ts="2026-03-01", signal_type="long_entry",
                            close=70.0, bar_interval="1d")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "buy-only",
        "signal_id": sid, "strategy_id": "s1", "symbol": "GDX",
        "side": "buy", "qty": 10, "order_type": "market",
        "submitted_at": "2026-03-01T14:30Z",
        "filled_at": "2026-03-01T14:30:05Z",
        "fill_price": 70.0, "status": "filled",
    })
    assert ex.closed_round_trips(conn) == []


def test_rejected_fills_excluded(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    sid = db.record_signal(conn, strategy_id="s1", symbol="GDX",
                            bar_ts="2026-03-01", signal_type="long_entry",
                            close=70.0, bar_interval="1d")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "buy-rej",
        "signal_id": sid, "strategy_id": "s1", "symbol": "GDX",
        "side": "buy", "qty": 10, "order_type": "market",
        "submitted_at": "2026-03-01T14:30Z",
        "fill_price": 70.0, "status": "rejected",
    })
    assert ex.closed_round_trips(conn) == []


def test_year_filter_only_includes_matching_sell_year(isolated_db):
    conn = db.init_db()
    _seed_round_trip(conn, strategy_id="s1", symbol="A",
                      qty=10, buy_price=100, sell_price=105,
                      buy_iso="2025-12-30", sell_iso="2026-01-10")
    _seed_round_trip(conn, strategy_id="s1", symbol="B",
                      qty=10, buy_price=100, sell_price=105,
                      buy_iso="2025-12-30", sell_iso="2025-12-31")
    rts_2026 = ex.closed_round_trips(conn, year=2026)
    rts_2025 = ex.closed_round_trips(conn, year=2025)
    assert {r["symbol"] for r in rts_2026} == {"A"}
    assert {r["symbol"] for r in rts_2025} == {"B"}


# ---------- split_short_long ----------

def test_split_short_term_under_365_days(isolated_db):
    conn = db.init_db()
    _seed_round_trip(conn, strategy_id="s1", symbol="GDX",
                      qty=10, buy_price=70, sell_price=72,
                      buy_iso="2026-01-01", sell_iso="2026-06-30")
    rts = ex.closed_round_trips(conn)
    split = ex.split_short_long(rts)
    assert len(split["short_term"]) == 1
    assert len(split["long_term"]) == 0


def test_split_long_term_at_or_over_365_days(isolated_db):
    conn = db.init_db()
    _seed_round_trip(conn, strategy_id="s1", symbol="GDX",
                      qty=10, buy_price=70, sell_price=72,
                      buy_iso="2025-01-01", sell_iso="2026-01-01")
    rts = ex.closed_round_trips(conn)
    split = ex.split_short_long(rts)
    # Exactly 365 days held → long-term per IRS day-count rule.
    assert split["long_term"][0]["hold_days"] == 365
    assert len(split["short_term"]) == 0
    assert len(split["long_term"]) == 1


def test_split_boundary_exact_364_days_is_short_term(isolated_db):
    conn = db.init_db()
    _seed_round_trip(conn, strategy_id="s1", symbol="A",
                      qty=10, buy_price=100, sell_price=110,
                      buy_iso="2025-01-02", sell_iso="2026-01-01")
    rts = ex.closed_round_trips(conn)
    split = ex.split_short_long(rts)
    assert split["short_term"][0]["hold_days"] == 364
    assert split["long_term"] == []


# ---------- export round-trip ----------

def test_export_writes_csvs_and_returns_summary(isolated_db, tmp_path):
    conn = db.init_db()
    _seed_round_trip(conn, strategy_id="s1", symbol="GDX",
                      qty=10, buy_price=70, sell_price=72,
                      buy_iso="2026-03-01", sell_iso="2026-03-15")
    _seed_round_trip(conn, strategy_id="s1", symbol="KRE",
                      qty=5, buy_price=50, sell_price=48,
                      buy_iso="2025-01-01", sell_iso="2026-04-01")
    out_dir = tmp_path / "tax"
    summary = ex.export(year=2026, out_dir=out_dir, conn=conn)
    assert summary["counts"]["short_term"] == 1
    assert summary["counts"]["long_term"] == 1
    # Short-term: +20 (GDX), long-term: -10 (KRE). Net: +10.
    assert summary["totals"]["short_term_gain_loss"] == pytest.approx(20.0)
    assert summary["totals"]["long_term_gain_loss"] == pytest.approx(-10.0)
    assert summary["totals"]["net"] == pytest.approx(10.0)
    # CSV files written + parseable.
    st_path = Path(summary["short_term_csv"])
    lt_path = Path(summary["long_term_csv"])
    assert st_path.exists() and lt_path.exists()
    with st_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    r = rows[0]
    assert r["description"] == "10 GDX"
    assert r["date_acquired"] == "03/01/2026"
    assert r["date_sold"] == "03/15/2026"
    assert r["cost_basis"] == "700.00"
    assert r["proceeds"] == "720.00"
    assert r["gain_loss"] == "20.00"


def test_export_columns_match_irs_8949(isolated_db, tmp_path):
    """Acceptance: column shape matches IRS Form 8949."""
    conn = db.init_db()
    _seed_round_trip(conn, strategy_id="s1", symbol="X",
                      qty=1, buy_price=100, sell_price=110,
                      buy_iso="2026-03-01", sell_iso="2026-03-02")
    out_dir = tmp_path / "tax"
    ex.export(year=2026, out_dir=out_dir, conn=conn)
    st_path = out_dir / "form_8949_short_term_2026.csv"
    with st_path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
    assert header == ex.FORM_8949_COLUMNS


def test_export_empty_when_no_round_trips(isolated_db, tmp_path):
    out_dir = tmp_path / "tax"
    conn = db.init_db()
    summary = ex.export(year=2026, out_dir=out_dir, conn=conn)
    assert summary["counts"]["total"] == 0
    # Both CSVs created (with just the header row) so downstream tooling
    # doesn't crash on a missing file.
    for p in (out_dir / "form_8949_short_term_2026.csv",
               out_dir / "form_8949_long_term_2026.csv"):
        assert p.exists()
        with p.open(encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 1  # header only


def test_export_roundtrip_math_loss_case(isolated_db, tmp_path):
    """Regression: loss must serialize with leading minus + 2 decimals."""
    conn = db.init_db()
    _seed_round_trip(conn, strategy_id="s1", symbol="X",
                      qty=10, buy_price=100, sell_price=95,
                      buy_iso="2026-03-01", sell_iso="2026-03-10")
    out_dir = tmp_path / "tax"
    summary = ex.export(year=2026, out_dir=out_dir, conn=conn)
    assert summary["totals"]["short_term_gain_loss"] == pytest.approx(-50.0)
    with Path(summary["short_term_csv"]).open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["gain_loss"] == "-50.00"


# ---------- CLI smoke ----------

def test_cli_writes_to_chosen_dir(isolated_db, tmp_path, capsys):
    conn = db.init_db()
    _seed_round_trip(conn, strategy_id="s1", symbol="GDX",
                      qty=1, buy_price=10, sell_price=11,
                      buy_iso="2026-01-02", sell_iso="2026-01-03")
    conn.close()
    out_dir = tmp_path / "out"
    ex.main(["--year", "2026", "--out", str(out_dir)])
    assert (out_dir / "form_8949_short_term_2026.csv").exists()
    captured = capsys.readouterr().out
    assert '"net": 1.0' in captured or '"net": 1' in captured
