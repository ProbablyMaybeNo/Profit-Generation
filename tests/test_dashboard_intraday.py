"""
test_dashboard_intraday.py — 5.6.1 / 5.6.2: dashboard intraday surfaces.

Covers:
5.6.1:
  - /api/state surfaces intraday_signals_today key
  - empty DB → []
  - intraday signals (5m/15m/1h) on today appear in the list
  - EOD signals are excluded
  - signals from a different day are excluded
  - limit of 20 honoured
  - card markup present in index.html

5.6.2:
  - paper_trades_today rows carry bar_interval (defaults to '1d' on
    rows without a resolvable signal)
  - join surfaces 15m signals → INTRADAY tag in the renderer
  - renderer markup carries the INTRADAY/EOD distinction
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    yield test_db


@pytest.fixture()
def client(isolated_db):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def _seed_strategy(sid="strat"):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    return conn


# ---------------- 5.6.1: intraday signals card ----------------

def test_state_includes_intraday_signals_key(client):
    rv = client.get("/api/state")
    s = rv.get_json()
    assert "intraday_signals_today" in s
    assert s["intraday_signals_today"] == []


def test_intraday_signals_only_today_intraday(client, isolated_db):
    today = date.today().isoformat()
    conn = _seed_strategy("intra-strat")
    # Three intraday signals today (5m / 15m / 1h) — should all surface
    db.record_signal(conn, strategy_id="intra-strat", symbol="SPY",
                     bar_ts=f"{today}T14:30:00",
                     signal_type="long_entry", close=400.0, bar_interval="15m")
    db.record_signal(conn, strategy_id="intra-strat", symbol="QQQ",
                     bar_ts=f"{today}T10:05:00",
                     signal_type="long_entry", close=350.0, bar_interval="5m")
    db.record_signal(conn, strategy_id="intra-strat", symbol="IWM",
                     bar_ts=f"{today}T13:00:00",
                     signal_type="long_entry", close=200.0, bar_interval="1h")
    # EOD signal today — excluded
    db.record_signal(conn, strategy_id="intra-strat", symbol="SPY",
                     bar_ts=today, signal_type="long_entry",
                     close=400.0, bar_interval="1d")
    # Intraday signal YESTERDAY — excluded
    db.record_signal(conn, strategy_id="intra-strat", symbol="SPY",
                     bar_ts="2026-04-15T14:30:00",
                     signal_type="long_entry", close=400.0, bar_interval="15m")
    conn.close()

    rv = client.get("/api/state")
    rows = rv.get_json()["intraday_signals_today"]
    assert len(rows) == 3
    syms = sorted(r["symbol"] for r in rows)
    assert syms == ["IWM", "QQQ", "SPY"]
    assert all(r["bar_interval"] != "1d" for r in rows)


def test_intraday_signals_limit(client, isolated_db):
    today = date.today().isoformat()
    conn = _seed_strategy("intra-strat")
    # 25 signals today — limit 20 should clip.
    for i in range(25):
        db.record_signal(conn, strategy_id="intra-strat", symbol="SPY",
                          bar_ts=f"{today}T{14 + i // 10:02d}:{(i % 10) * 5:02d}:00",
                          signal_type="long_entry", close=400.0,
                          bar_interval="15m")
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["intraday_signals_today"]
    assert len(rows) == 20


def test_intraday_signals_card_present_in_index_html():
    idx = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert 'id="intraday-signals-card"' in idx
    assert 'id="intraday-signals"' in idx
    assert "renderIntradaySignals" in idx


# ---------------- 5.6.2: paper trades carry bar_interval ----------------

def _insert_paper_trade(conn, *, sig_id, symbol, side, order_id,
                         submitted_at):
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " submitted_at, filled_at, status) "
        "VALUES (?, ?, 'strat', ?, ?, ?, ?, ?, 'filled')",
        (order_id, sig_id, symbol, side, 1.0, submitted_at, submitted_at),
    )
    conn.commit()


def test_paper_trades_today_carries_bar_interval(client, isolated_db):
    today = date.today().isoformat()
    conn = _seed_strategy("strat")
    # Intraday signal → paper trade
    sig_intra = db.record_signal(
        conn, strategy_id="strat", symbol="SPY",
        bar_ts=f"{today}T14:30:00", signal_type="long_entry",
        close=400.0, bar_interval="15m",
    )
    _insert_paper_trade(conn, sig_id=sig_intra, symbol="SPY", side="buy",
                         order_id="o-intra", submitted_at=f"{today}T14:30:05")
    # EOD signal → paper trade
    sig_eod = db.record_signal(
        conn, strategy_id="strat", symbol="QQQ",
        bar_ts=today, signal_type="long_entry",
        close=350.0, bar_interval="1d",
    )
    _insert_paper_trade(conn, sig_id=sig_eod, symbol="QQQ", side="buy",
                         order_id="o-eod", submitted_at=f"{today}T16:30:05")
    conn.close()

    rv = client.get("/api/state")
    rows = rv.get_json()["paper_trades_today"]
    by_sym = {r["symbol"]: r for r in rows}
    assert "SPY" in by_sym and "QQQ" in by_sym
    assert by_sym["SPY"]["bar_interval"] == "15m"
    assert by_sym["QQQ"]["bar_interval"] == "1d"


def test_paper_trades_today_defaults_to_1d_on_missing_signal(client, isolated_db):
    """Paper trade without a resolvable signal_id falls back to '1d'."""
    today = date.today().isoformat()
    conn = db.init_db()
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " submitted_at, status) "
        "VALUES ('o-manual', NULL, 'manual', 'TSLA', 'buy', 1.0, ?, 'filled')",
        (f"{today}T10:00:00",),
    )
    conn.commit()
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["paper_trades_today"]
    assert len(rows) == 1
    assert rows[0]["bar_interval"] == "1d"


def test_paper_trades_render_intraday_tag_in_index_html():
    """Renderer must emit INTRADAY (interval) for non-1d rows."""
    idx = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "INTRADAY (" in idx
    assert "EOD (1d)" in idx
    assert "data-bar-interval" in idx
