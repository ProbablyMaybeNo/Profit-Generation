import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import reconcile_positions as rp  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_buy(conn, *, strategy_id, symbol, qty, submitted_at,
              status="filled", side="buy", signal_id=None):
    if signal_id is None:
        db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
        signal_id = db.record_signal(
            conn, strategy_id=strategy_id, symbol=symbol,
            bar_ts=submitted_at[:10], signal_type="long_entry",
            close=100.0, bar_interval="1d",
        )
    db.record_paper_trade(conn, {
        "alpaca_order_id": f"alp-{signal_id}-{side}",
        "signal_id": signal_id,
        "strategy_id": strategy_id, "symbol": symbol,
        "side": side, "qty": qty, "order_type": "market",
        "submitted_at": submitted_at, "status": status,
        "fill_price": 100.0,
    })
    return signal_id


# ---- db_open_positions ---------------------------------------------------

def test_db_open_positions_empty(isolated_db):
    conn = db.init_db()
    assert rp.db_open_positions(conn) == {}


def test_db_open_positions_single_buy(isolated_db):
    conn = db.init_db()
    _seed_buy(conn, strategy_id="s1", symbol="GDX", qty=10,
              submitted_at="2026-05-14T13:30:00Z")
    pos = rp.db_open_positions(conn)
    assert pos == {"GDX": {"qty": 10.0, "strategies": ["s1"]}}


def test_db_open_positions_buy_then_sell_excluded(isolated_db):
    conn = db.init_db()
    _seed_buy(conn, strategy_id="s1", symbol="GDX", qty=10,
              submitted_at="2026-05-14T13:30:00Z")
    sid = db.record_signal(conn, strategy_id="s1", symbol="GDX",
                            bar_ts="2026-05-15", signal_type="long_exit",
                            close=110.0, bar_interval="1d")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "alp-sell",
        "signal_id": sid, "strategy_id": "s1", "symbol": "GDX",
        "side": "sell", "qty": 10, "order_type": "market",
        "submitted_at": "2026-05-15T13:30:00Z", "status": "filled",
        "fill_price": 110.0,
    })
    assert rp.db_open_positions(conn) == {}


def test_db_open_positions_rejected_buy_excluded(isolated_db):
    conn = db.init_db()
    _seed_buy(conn, strategy_id="s1", symbol="GDX", qty=10,
              submitted_at="2026-05-14T13:30:00Z", status="rejected")
    assert rp.db_open_positions(conn) == {}


def test_db_open_positions_canceled_sell_does_not_close(isolated_db):
    conn = db.init_db()
    _seed_buy(conn, strategy_id="s1", symbol="GDX", qty=10,
              submitted_at="2026-05-14T13:30:00Z")
    sid = db.record_signal(conn, strategy_id="s1", symbol="GDX",
                            bar_ts="2026-05-15", signal_type="long_exit",
                            close=110.0, bar_interval="1d")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "alp-cancel",
        "signal_id": sid, "strategy_id": "s1", "symbol": "GDX",
        "side": "sell", "qty": 10, "order_type": "market",
        "submitted_at": "2026-05-15T13:30:00Z", "status": "canceled",
    })
    pos = rp.db_open_positions(conn)
    assert "GDX" in pos
    assert pos["GDX"]["qty"] == 10.0


def test_db_open_positions_aggregates_strategies_per_symbol(isolated_db):
    conn = db.init_db()
    _seed_buy(conn, strategy_id="s1", symbol="GDX", qty=10,
              submitted_at="2026-05-14T13:30:00Z")
    _seed_buy(conn, strategy_id="s2", symbol="GDX", qty=5,
              submitted_at="2026-05-14T14:30:00Z")
    pos = rp.db_open_positions(conn)
    assert pos["GDX"]["qty"] == 15.0
    assert set(pos["GDX"]["strategies"]) == {"s1", "s2"}


# ---- alpaca_open_positions ------------------------------------------------

def _mk_pos(symbol, qty, avg=100.0):
    p = MagicMock()
    p.symbol = symbol
    p.qty = str(qty)
    p.avg_entry_price = str(avg)
    return p


def test_alpaca_open_positions_normalises_attr_form():
    client = MagicMock()
    client.get_all_positions = MagicMock(return_value=[
        _mk_pos("GDX", 10), _mk_pos("KRE", 4, avg=70.0),
    ])
    out = rp.alpaca_open_positions(client)
    assert out == {
        "GDX": {"qty": 10.0, "avg_entry_price": 100.0},
        "KRE": {"qty": 4.0, "avg_entry_price": 70.0},
    }


def test_alpaca_open_positions_skips_zero_qty():
    client = MagicMock()
    client.get_all_positions = MagicMock(return_value=[_mk_pos("X", 0)])
    assert rp.alpaca_open_positions(client) == {}


def test_alpaca_open_positions_falls_back_to_list_positions():
    client = MagicMock(spec=["list_positions"])
    client.list_positions = MagicMock(return_value=[_mk_pos("GDX", 5)])
    out = rp.alpaca_open_positions(client)
    assert "GDX" in out


def test_alpaca_open_positions_raises_without_either_method():
    client = MagicMock(spec=[])
    with pytest.raises(RuntimeError):
        rp.alpaca_open_positions(client)


# ---- compute_drift --------------------------------------------------------

def test_compute_drift_no_drift():
    db_pos = {"GDX": {"qty": 10.0, "strategies": ["s1"]}}
    al_pos = {"GDX": {"qty": 10.0, "avg_entry_price": 100.0}}
    r = rp.compute_drift(db_pos, al_pos)
    assert r["drift_count"] == 0
    assert r["agree_count"] == 1
    assert r["only_in_alpaca"] == []
    assert r["only_in_db"] == []
    assert r["qty_mismatch"] == []


def test_compute_drift_only_in_alpaca():
    db_pos = {}
    al_pos = {"GDX": {"qty": 10.0, "avg_entry_price": 100.0}}
    r = rp.compute_drift(db_pos, al_pos)
    assert r["drift_count"] == 1
    assert r["only_in_alpaca"] == [{"symbol": "GDX", "qty": 10.0}]


def test_compute_drift_only_in_db():
    db_pos = {"GDX": {"qty": 10.0, "strategies": ["s1"]}}
    al_pos = {}
    r = rp.compute_drift(db_pos, al_pos)
    assert r["drift_count"] == 1
    assert r["only_in_db"][0]["symbol"] == "GDX"
    assert r["only_in_db"][0]["qty"] == 10.0
    assert r["only_in_db"][0]["strategies"] == ["s1"]


def test_compute_drift_qty_mismatch():
    db_pos = {"GDX": {"qty": 10.0, "strategies": ["s1"]}}
    al_pos = {"GDX": {"qty": 14.0, "avg_entry_price": 100.0}}
    r = rp.compute_drift(db_pos, al_pos)
    assert r["drift_count"] == 1
    m = r["qty_mismatch"][0]
    assert m["symbol"] == "GDX"
    assert m["db_qty"] == 10.0
    assert m["alpaca_qty"] == 14.0
    assert m["delta"] == 4.0


def test_compute_drift_mixed():
    db_pos = {
        "GDX": {"qty": 10.0, "strategies": ["s1"]},  # qty mismatch
        "KRE": {"qty": 5.0, "strategies": ["s2"]},   # only in db
        "XLF": {"qty": 3.0, "strategies": ["s3"]},   # agree
    }
    al_pos = {
        "GDX": {"qty": 12.0, "avg_entry_price": 100.0},
        "XLF": {"qty": 3.0, "avg_entry_price": 40.0},
        "ARKK": {"qty": 7.0, "avg_entry_price": 50.0},  # only in alpaca
    }
    r = rp.compute_drift(db_pos, al_pos)
    assert r["agree_count"] == 1
    assert r["drift_count"] == 3
    assert [x["symbol"] for x in r["only_in_alpaca"]] == ["ARKK"]
    assert [x["symbol"] for x in r["only_in_db"]] == ["KRE"]
    assert [x["symbol"] for x in r["qty_mismatch"]] == ["GDX"]


# ---- format_section / format_telegram_alert ------------------------------

def test_format_section_no_drift():
    section = rp.format_section({
        "agree_count": 3, "drift_count": 0,
        "only_in_alpaca": [], "only_in_db": [], "qty_mismatch": [],
        "as_of": "2026-05-16T21:00:00+00:00",
    })
    assert "No drift" in section
    assert "3 symbol(s) match" in section


def test_format_section_with_drift():
    section = rp.format_section({
        "agree_count": 1, "drift_count": 2,
        "only_in_alpaca": [{"symbol": "ARKK", "qty": 7.0}],
        "only_in_db": [{"symbol": "KRE", "qty": 5.0, "strategies": ["s2"]}],
        "qty_mismatch": [],
        "as_of": "2026-05-16T21:00:00+00:00",
    })
    assert "2 drift(s) detected" in section
    assert "ARKK" in section
    assert "KRE" in section
    assert "s2" in section


def test_format_telegram_alert_empty_when_no_drift():
    assert rp.format_telegram_alert({
        "agree_count": 3, "drift_count": 0,
        "only_in_alpaca": [], "only_in_db": [], "qty_mismatch": [],
    }) == ""


def test_format_telegram_alert_has_each_category():
    text = rp.format_telegram_alert({
        "agree_count": 0, "drift_count": 3,
        "only_in_alpaca": [{"symbol": "ARKK", "qty": 7.0}],
        "only_in_db": [{"symbol": "KRE", "qty": 5.0, "strategies": ["s2"]}],
        "qty_mismatch": [{"symbol": "GDX", "db_qty": 10.0,
                           "alpaca_qty": 14.0, "delta": 4.0}],
    })
    assert "drift detected" in text
    assert "ARKK" in text
    assert "KRE" in text
    assert "GDX" in text


# ---- snapshot persistence -------------------------------------------------

def test_save_and_load_snapshot(tmp_path):
    p = tmp_path / "last.json"
    payload = {"drift_count": 0, "agree_count": 3, "only_in_alpaca": [],
                "only_in_db": [], "qty_mismatch": [], "as_of": "x"}
    rp._save_snapshot(payload, path=p)
    loaded = rp.load_snapshot(path=p)
    assert loaded == payload


def test_load_snapshot_missing_returns_none(tmp_path):
    assert rp.load_snapshot(path=tmp_path / "nope.json") is None


def test_load_snapshot_malformed_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("garbage", encoding="utf-8")
    assert rp.load_snapshot(path=p) is None


# ---- reconcile() end-to-end ----------------------------------------------

def test_reconcile_no_drift_no_alert(isolated_db, tmp_path):
    conn = db.init_db()
    _seed_buy(conn, strategy_id="s1", symbol="GDX", qty=10,
              submitted_at="2026-05-14T13:30:00Z")
    sent = []
    save_path = tmp_path / "rec.json"
    result = rp.reconcile(
        conn=conn,
        alpaca_positions_fn=lambda: {"GDX": {"qty": 10.0, "avg_entry_price": 100.0}},
        send_fn=lambda t: sent.append(t) or True,
        save_path=save_path,
        now_fn=lambda: "2026-05-16T21:00:00+00:00",
    )
    assert result["drift_count"] == 0
    assert sent == []
    on_disk = json.loads(save_path.read_text())
    assert on_disk["agree_count"] == 1


def test_reconcile_drift_alerts(isolated_db, tmp_path):
    conn = db.init_db()
    _seed_buy(conn, strategy_id="s1", symbol="GDX", qty=10,
              submitted_at="2026-05-14T13:30:00Z")
    sent = []
    result = rp.reconcile(
        conn=conn,
        alpaca_positions_fn=lambda: {"GDX": {"qty": 14.0, "avg_entry_price": 100.0}},
        send_fn=lambda t: sent.append(t) or True,
        save_path=tmp_path / "rec.json",
        now_fn=lambda: "2026-05-16T21:00:00+00:00",
    )
    assert result["drift_count"] == 1
    assert len(sent) == 1
    assert "GDX" in sent[0]


def test_reconcile_no_alert_flag_suppresses_telegram(isolated_db, tmp_path):
    conn = db.init_db()
    _seed_buy(conn, strategy_id="s1", symbol="GDX", qty=10,
              submitted_at="2026-05-14T13:30:00Z")
    sent = []
    rp.reconcile(
        conn=conn,
        alpaca_positions_fn=lambda: {},
        send_fn=lambda t: sent.append(t) or True,
        save_path=tmp_path / "rec.json",
        alert=False,
    )
    assert sent == []
