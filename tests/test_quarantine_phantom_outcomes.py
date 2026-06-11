import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from scripts import quarantine_phantom_outcomes as q  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    db.upsert_strategy(c, {"extra": {"strategy_id": "strat"}})
    yield c
    c.close()


def _entry(c, sym, bar_ts, *, bar_interval, close=100.0):
    sid = db.record_signal(c, strategy_id="strat", symbol=sym, bar_ts=bar_ts,
                           signal_type="long_entry", close=close,
                           bar_interval=bar_interval)
    db.open_outcome(c, signal_id=sid, entry_ts=bar_ts, entry_price=close)
    return sid


def _fill(c, sid, sym, *, side="buy", status="filled"):
    db.record_paper_trade(c, {
        "alpaca_order_id": f"{side}-{sid}", "signal_id": sid,
        "strategy_id": "strat", "symbol": sym, "side": side, "qty": 10,
        "order_type": "market", "submitted_at": "2026-06-08", "status": status,
        "fill_price": 100.0,
    })


# ---- db helpers ----------------------------------------------------------

def test_signal_has_fill_true_only_for_filled_buy(conn):
    sid = _entry(conn, "AMD", "2026-06-08", bar_interval="15m")
    assert db.signal_has_fill(conn, sid) is False
    _fill(conn, sid, "AMD", side="buy", status="accepted")
    assert db.signal_has_fill(conn, sid) is False  # accepted != filled
    _fill(conn, sid, "AMD", side="buy", status="filled")
    assert db.signal_has_fill(conn, sid) is True


def test_signal_has_any_fill_counts_sell(conn):
    sid = _entry(conn, "AMD", "2026-06-08", bar_interval="15m")
    assert db.signal_has_any_fill(conn, sid) is False
    _fill(conn, sid, "AMD", side="sell", status="filled")
    assert db.signal_has_any_fill(conn, sid) is True
    assert db.signal_has_fill(conn, sid) is False  # buy-only check still False


def test_mark_outcome_phantom_nulls_price_and_return(conn):
    sid = _entry(conn, "AMD", "2026-06-08", bar_interval="15m")
    assert db.mark_outcome_phantom(conn, sid) is True
    o = conn.execute("SELECT * FROM outcomes WHERE signal_id=?", (sid,)).fetchone()
    assert o["status"] == "closed"
    assert o["exit_reason"] == db.PHANTOM_NO_FILL_REASON
    assert o["exit_price"] is None
    assert o["return_pct"] is None


# ---- quarantine script ---------------------------------------------------

def test_find_phantoms_intraday_only_excludes_filled_and_1d(conn):
    ph_intra = _entry(conn, "AMD", "2026-06-08", bar_interval="15m")   # phantom
    filled = _entry(conn, "TSLA", "2026-06-08", bar_interval="15m")    # real
    _fill(conn, filled, "TSLA")
    _entry(conn, "SPY", "2026-06-08", bar_interval="1d")               # 1d phantom
    conn.commit()

    rows = q.find_phantoms(conn, intraday_only=True)
    ids = {r["signal_id"] for r in rows}
    assert ids == {ph_intra}  # only the intraday no-fill row


def test_find_phantoms_all_includes_1d(conn):
    ph_intra = _entry(conn, "AMD", "2026-06-08", bar_interval="15m")
    ph_1d = _entry(conn, "SPY", "2026-06-08", bar_interval="1d")
    conn.commit()
    ids = {r["signal_id"] for r in q.find_phantoms(conn, intraday_only=False)}
    assert ids == {ph_intra, ph_1d}


def test_run_dry_then_apply_is_idempotent(conn):
    sid = _entry(conn, "AMD", "2026-06-08", bar_interval="15m")
    conn.commit()

    dry = q.run(conn, intraday_only=True, apply=False)
    assert dry["found"] == 1 and dry["quarantined"] == 0
    assert _outcome_reason(conn, sid) is None  # untouched on dry-run

    applied = q.run(conn, intraday_only=True, apply=True)
    assert applied["found"] == 1 and applied["quarantined"] == 1
    assert _outcome_reason(conn, sid) == db.PHANTOM_NO_FILL_REASON

    again = q.run(conn, intraday_only=True, apply=True)
    assert again["found"] == 0  # already tagged → no longer a candidate


def _outcome_reason(conn, sid):
    return conn.execute(
        "SELECT exit_reason FROM outcomes WHERE signal_id=?", (sid,)
    ).fetchone()["exit_reason"]
