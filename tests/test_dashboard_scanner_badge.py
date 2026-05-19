"""
test_dashboard_scanner_badge.py — milestone 5.5.6.2.

Verifies that paper trades whose source signal was produced by the
wide-universe trend scanner carry an `is_scanner` flag from the API
and get visually tagged with a 'scanner' badge by the Monitor renderer.

Mixed-source fixture covers:
  - scanner-sourced paper trade → is_scanner = True
  - active_on-sourced paper trade → is_scanner = False
  - manual paper trade (no signal_id) → is_scanner = False
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


def _paper(conn, *, sig_id, sid, symbol, side, order_id, submitted_at):
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " submitted_at, filled_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'filled')",
        (order_id, sig_id, sid, symbol, side, 1.0,
         submitted_at, submitted_at),
    )
    conn.commit()


def test_paper_trades_carry_is_scanner_flag(client, isolated_db):
    today = date.today().isoformat()
    conn = _seed_strategy("strat")
    scanner_sig = db.record_signal(
        conn, strategy_id="strat", symbol="AAPL",
        bar_ts=today, signal_type="long_entry", close=180.0,
        bar_interval="1d",
        extra={"source": "trend_scanner", "wide_universe": True},
    )
    _paper(conn, sig_id=scanner_sig, sid="strat", symbol="AAPL",
           side="buy", order_id="o-scan",
           submitted_at=f"{today}T16:30:00")

    narrow_sig = db.record_signal(
        conn, strategy_id="strat", symbol="QQQ",
        bar_ts=today, signal_type="long_entry", close=350.0,
        bar_interval="1d",
    )
    _paper(conn, sig_id=narrow_sig, sid="strat", symbol="QQQ",
           side="buy", order_id="o-narrow",
           submitted_at=f"{today}T16:30:05")
    conn.close()

    rv = client.get("/api/state")
    rows = rv.get_json()["paper_trades_today"]
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["AAPL"]["is_scanner"] is True
    assert by_sym["QQQ"]["is_scanner"] is False


def test_paper_trade_without_signal_defaults_to_not_scanner(client,
                                                              isolated_db):
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
    assert rows[0]["is_scanner"] is False


def test_paper_trade_wide_universe_only_also_tagged(client, isolated_db):
    """`wide_universe: true` without `source: trend_scanner` should also
    tag as scanner — belt-and-suspenders for legacy / future producers."""
    today = date.today().isoformat()
    conn = _seed_strategy("strat")
    sig = db.record_signal(
        conn, strategy_id="strat", symbol="NVDA",
        bar_ts=today, signal_type="long_entry", close=500.0,
        bar_interval="1d", extra={"wide_universe": True},
    )
    _paper(conn, sig_id=sig, sid="strat", symbol="NVDA",
           side="buy", order_id="o-wu", submitted_at=f"{today}T16:30:00")
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["paper_trades_today"]
    assert rows[0]["is_scanner"] is True


def test_paper_trade_malformed_extra_json_does_not_crash(client,
                                                          isolated_db):
    """Defensive: bad JSON in signal extra → is_scanner = False, no
    500."""
    today = date.today().isoformat()
    conn = _seed_strategy("strat")
    # Insert a signal with a malformed extra_json directly.
    conn.execute(
        "INSERT INTO signals "
        "(ts, bar_ts, bar_interval, strategy_id, symbol, signal_type, "
        " close, extra_json) "
        "VALUES (?, ?, '1d', 'strat', 'BAD', 'long_entry', 100.0, ?)",
        (f"{today}T10:00:00", today, "{not valid json"),
    )
    sig_id = conn.execute(
        "SELECT id FROM signals WHERE symbol='BAD'"
    ).fetchone()["id"]
    _paper(conn, sig_id=sig_id, sid="strat", symbol="BAD",
           side="buy", order_id="o-bad",
           submitted_at=f"{today}T10:00:01")
    conn.close()
    rv = client.get("/api/state")
    assert rv.status_code == 200
    rows = rv.get_json()["paper_trades_today"]
    assert rows[0]["is_scanner"] is False


# ---------------- markup present in Monitor index.html ----------------

def test_scanner_badge_present_in_index_html():
    idx = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "scanner-badge" in idx
    assert "is_scanner" in idx
    assert "data-is-scanner" in idx
