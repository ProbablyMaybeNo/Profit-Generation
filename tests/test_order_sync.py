import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import order_sync  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    conn = db.init_db(test_db)
    yield conn
    conn.close()


def _seed_trade(conn, *, order_id, status="accepted", side="buy",
                fill_price=None, symbol="GDX", qty=3):
    db.record_paper_trade(conn, {
        "alpaca_order_id": order_id,
        "signal_id": None,
        "strategy_id": "winner",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "order_type": "market",
        "submitted_at": "2026-05-26T21:03:49+00:00",
        "fill_price": fill_price,
        "status": status,
        "notes": "seed",
    })


def _mk_order(*, status, filled_avg_price=None, filled_at=None):
    o = MagicMock()
    o.status = status
    o.filled_avg_price = filled_avg_price
    o.filled_at = filled_at
    return o


def _mk_client(order_map):
    client = MagicMock()
    client.get_order_by_id.side_effect = lambda oid: order_map[oid]
    return client


def test_fill_backfills_status_price_and_time(isolated_db):
    conn = isolated_db
    _seed_trade(conn, order_id="o1", status="accepted")
    client = _mk_client({
        "o1": _mk_order(status="filled", filled_avg_price=86.5,
                        filled_at="2026-05-26T21:03:55+00:00"),
    })
    res = order_sync.sync_order_fills(conn, client)
    assert res == {"checked": 1, "updated": 1, "filled": 1, "errors": 0}
    row = conn.execute(
        "SELECT status, fill_price, filled_at FROM paper_trades "
        "WHERE alpaca_order_id='o1'").fetchone()
    assert row["status"] == "filled"
    assert row["fill_price"] == 86.5
    assert row["filled_at"] == "2026-05-26T21:03:55+00:00"


def test_terminal_rows_are_not_rechecked(isolated_db):
    conn = isolated_db
    _seed_trade(conn, order_id="done", status="filled", fill_price=10.0)
    client = _mk_client({})
    res = order_sync.sync_order_fills(conn, client)
    assert res["checked"] == 0
    client.get_order_by_id.assert_not_called()


def test_no_forward_progress_is_not_rewritten(isolated_db):
    conn = isolated_db
    _seed_trade(conn, order_id="o2", status="accepted")
    client = _mk_client({"o2": _mk_order(status="accepted")})
    res = order_sync.sync_order_fills(conn, client)
    assert res["checked"] == 1
    assert res["updated"] == 0
    assert res["filled"] == 0


def test_partial_then_fill_advances(isolated_db):
    conn = isolated_db
    _seed_trade(conn, order_id="o3", status="accepted")
    client = _mk_client({
        "o3": _mk_order(status="partially_filled", filled_avg_price=70.0,
                        filled_at="2026-05-26T21:04:00+00:00"),
    })
    res = order_sync.sync_order_fills(conn, client)
    assert res["updated"] == 1
    assert res["filled"] == 0
    row = conn.execute(
        "SELECT status, fill_price FROM paper_trades "
        "WHERE alpaca_order_id='o3'").fetchone()
    assert row["status"] == "partially_filled"
    assert row["fill_price"] == 70.0


def test_broker_error_is_counted_not_raised(isolated_db):
    conn = isolated_db
    _seed_trade(conn, order_id="bad", status="accepted")
    client = MagicMock()
    client.get_order_by_id.side_effect = RuntimeError("api down")
    res = order_sync.sync_order_fills(conn, client)
    assert res == {"checked": 1, "updated": 0, "filled": 0, "errors": 1}
    row = conn.execute(
        "SELECT status FROM paper_trades WHERE alpaca_order_id='bad'").fetchone()
    assert row["status"] == "accepted"
