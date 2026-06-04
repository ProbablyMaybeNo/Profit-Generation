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


# ---- A3: broker-reconcile orphan-outcome sweep --------------------------
#
# The OPEN-outcome ledger diverged ~18x from broker reality (audit: 260 open
# outcomes / 179 symbols vs 14 real positions). sweep_orphan_outcomes closes
# OPEN outcomes whose symbol is NOT held at the broker with
# exit_reason='reconciled_no_position', and MUST leave outcomes whose symbol
# IS held untouched.

def _open_outcome(conn, *, strategy_id, symbol, bar_interval="1d",
                  entry_ts="2026-05-14", entry_price=100.0):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    sid = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=entry_ts, signal_type="long_entry",
        close=entry_price, bar_interval=bar_interval,
    )
    db.open_outcome(conn, signal_id=sid, entry_ts=entry_ts,
                    entry_price=entry_price)
    conn.commit()
    return sid


def test_sweep_closes_orphan_leaves_held_untouched(isolated_db):
    conn = db.init_db()
    # PHANTOM: open outcome whose symbol the broker no longer holds.
    phantom = _open_outcome(conn, strategy_id="trend-a", symbol="ZZZ",
                            entry_price=50.0)
    # A recorded sell fill gives the honest last-known mark.
    db.record_paper_trade(conn, {
        "alpaca_order_id": "sell-zzz", "signal_id": phantom,
        "strategy_id": "trend-a", "symbol": "ZZZ", "side": "sell",
        "qty": 10, "order_type": "market",
        "submitted_at": "2026-05-15T13:30:00Z", "status": "filled",
        "fill_price": 55.0,
    })
    # HELD: open outcome whose symbol the broker still holds.
    held = _open_outcome(conn, strategy_id="trend-b", symbol="NVDA",
                         entry_price=120.0)
    conn.commit()

    res = rp.sweep_orphan_outcomes(conn, {"NVDA"})
    assert res["swept"] == 1

    o_phantom = conn.execute(
        "SELECT status, exit_reason, exit_price FROM outcomes WHERE signal_id=?",
        (phantom,),
    ).fetchone()
    assert o_phantom["status"] == "closed"
    assert o_phantom["exit_reason"] == "reconciled_no_position"
    assert o_phantom["exit_price"] == pytest.approx(55.0)

    o_held = conn.execute(
        "SELECT status, exit_reason FROM outcomes WHERE signal_id=?", (held,),
    ).fetchone()
    assert o_held["status"] == "open", \
        "an outcome whose position genuinely still exists must NOT be swept"
    assert o_held["exit_reason"] is None


def test_sweep_skips_orphan_with_no_price(isolated_db):
    """Honest skip: no sell fill, no snapshot, no bar -> no fabricated price."""
    conn = db.init_db()
    orphan = _open_outcome(conn, strategy_id="trend-a", symbol="NOPRICE")
    res = rp.sweep_orphan_outcomes(conn, set())
    assert res["swept"] == 0
    assert res["skipped"] == 1
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (orphan,),
    ).fetchone()
    assert o["status"] == "open"


def test_sweep_uses_snapshot_close_when_no_sell(isolated_db):
    conn = db.init_db()
    orphan = _open_outcome(conn, strategy_id="trend-a", symbol="SPY")
    db.record_snapshot_row(conn, "2026-05-15", {"symbol": "SPY", "close": 488.0})
    conn.commit()
    res = rp.sweep_orphan_outcomes(conn, set())
    assert res["swept"] == 1
    o = conn.execute(
        "SELECT exit_price, exit_reason FROM outcomes WHERE signal_id=?",
        (orphan,),
    ).fetchone()
    assert o["exit_price"] == pytest.approx(488.0)
    assert o["exit_reason"] == "reconciled_no_position"


# ---- B1: daily-close fallback for 1d trend orphans -----------------------
#
# The first live A3 run swept only 32 of ~189 orphans; 175 were honestly
# skipped because no sell-fill / snapshot / intraday-bar mark was resolvable
# (mostly 1d donchian/ma-cross outcomes for symbols outside the ~23-symbol
# intraday universe). B1 adds the system's daily-bar close as a 4th, last-
# resort mark so those orphans converge instead of stranding OPEN.

def _daily_frame(closes):
    import pandas as pd
    idx = pd.date_range("2026-05-10", periods=len(closes), freq="D")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": [1000] * len(closes)},
                        index=idx)


def test_sweep_uses_daily_close_when_no_fill_snapshot_or_bar(isolated_db):
    """B1 acceptance: an orphan with NO sell-fill, NO snapshot, NO intraday
    bar but WITH an available daily close is swept 'reconciled_no_position'
    at that daily close. FAILS on pre-B1 code (skips for lack of a price)."""
    import pandas as pd
    conn = db.init_db()
    orphan = _open_outcome(conn, strategy_id="trend-donchian-breakout-20",
                           symbol="WMT", entry_price=90.0)
    # Daily-bar source resolves a close; latest is 97.25.
    fake_daily = lambda syms: {"WMT": _daily_frame([95.0, 96.0, 97.25])}

    res = rp.sweep_orphan_outcomes(conn, set(), daily_bars_fn=fake_daily)
    assert res["swept"] == 1
    assert res["skipped"] == 0

    o = conn.execute(
        "SELECT status, exit_reason, exit_price FROM outcomes WHERE signal_id=?",
        (orphan,),
    ).fetchone()
    assert o["status"] == "closed"
    assert o["exit_reason"] == "reconciled_no_position"
    assert o["exit_price"] == pytest.approx(97.25)


def test_sweep_daily_close_is_last_resort_after_snapshot(isolated_db):
    """Precedence preserved: a snapshot close still wins over the daily bar."""
    conn = db.init_db()
    orphan = _open_outcome(conn, strategy_id="trend-a", symbol="SPY")
    db.record_snapshot_row(conn, "2026-05-15", {"symbol": "SPY", "close": 488.0})
    conn.commit()
    fake_daily = lambda syms: {"SPY": _daily_frame([500.0, 510.0])}
    res = rp.sweep_orphan_outcomes(conn, set(), daily_bars_fn=fake_daily)
    assert res["swept"] == 1
    o = conn.execute(
        "SELECT exit_price FROM outcomes WHERE signal_id=?", (orphan,),
    ).fetchone()
    assert o["exit_price"] == pytest.approx(488.0)


def test_sweep_still_skips_when_daily_close_unavailable(isolated_db):
    """Honest skip survives B1: no fill/snapshot/bar AND no daily close."""
    conn = db.init_db()
    orphan = _open_outcome(conn, strategy_id="trend-a", symbol="NOPRICE")
    res = rp.sweep_orphan_outcomes(conn, set(), daily_bars_fn=lambda syms: {})
    assert res["swept"] == 0
    assert res["skipped"] == 1
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (orphan,),
    ).fetchone()
    assert o["status"] == "open"


# ---- B2: guarded one-time backfill entry point ---------------------------
#
# main(["--sweep-orphans"]) (alias --backfill) drives reconcile(
# sweep_orphans=True), which reuses sweep_orphan_outcomes (no parallel logic).
# It must close resolvable orphans, leave held + no-price ones alone, and be
# safe to re-run (already-closed outcomes are untouched).

def _stub_broker_for_main(monkeypatch, held_positions, *, daily=None):
    """Stub the broker + fill-sync seams reconcile() uses on the CLI path,
    so main() runs end-to-end against the isolated test DB without a network."""
    from config import utils as cfg_utils
    from monitoring import order_sync, telegram_alerter
    monkeypatch.setattr(cfg_utils, "get_alpaca_client", lambda: object(),
                        raising=False)
    monkeypatch.setattr(order_sync, "sync_order_fills",
                        lambda conn, client: {"updated": 0, "filled": 0})
    monkeypatch.setattr(rp, "alpaca_open_positions",
                        lambda client: dict(held_positions))
    monkeypatch.setattr(telegram_alerter, "send_message", lambda t: True)
    if daily is not None:
        monkeypatch.setattr(rp, "_default_daily_bars_fn", daily)


def test_main_backfill_flag_closes_resolvable_leaves_held_and_noprice(
        isolated_db, tmp_path, monkeypatch):
    """B2 acceptance: --sweep-orphans closes a resolvable orphan, leaves a
    held outcome open, and honest-skips a no-price orphan."""
    conn = db.init_db()
    # Resolvable orphan (snapshot mark), held outcome, no-price orphan.
    resolvable = _open_outcome(conn, strategy_id="trend-a", symbol="ZZZ",
                               entry_price=50.0)
    db.record_snapshot_row(conn, "2026-05-15", {"symbol": "ZZZ", "close": 47.0})
    held = _open_outcome(conn, strategy_id="trend-b", symbol="NVDA",
                         entry_price=120.0)
    noprice = _open_outcome(conn, strategy_id="trend-c", symbol="NOPRICE")
    conn.commit()

    monkeypatch.setattr(rp, "RECONCILE_SNAPSHOT", tmp_path / "rec.json")
    _stub_broker_for_main(monkeypatch, {"NVDA": {"qty": 5.0}},
                          daily=lambda syms: {})
    # main opens its own conn against the (monkeypatched) DB_FILE.
    with pytest.raises(SystemExit):
        rp.main(["--sweep-orphans", "--no-alert"])

    conn2 = db.init_db()
    o_res = conn2.execute(
        "SELECT status, exit_reason FROM outcomes WHERE signal_id=?",
        (resolvable,)).fetchone()
    assert o_res["status"] == "closed"
    assert o_res["exit_reason"] == "reconciled_no_position"
    o_held = conn2.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (held,)).fetchone()
    assert o_held["status"] == "open"
    o_np = conn2.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (noprice,)).fetchone()
    assert o_np["status"] == "open"


def test_main_backfill_is_idempotent(isolated_db, tmp_path, monkeypatch):
    """Re-running the backfill leaves already-closed outcomes untouched and
    sweeps nothing the second time."""
    conn = db.init_db()
    sid = _open_outcome(conn, strategy_id="trend-a", symbol="ZZZ",
                        entry_price=50.0)
    db.record_snapshot_row(conn, "2026-05-15", {"symbol": "ZZZ", "close": 47.0})
    conn.commit()

    monkeypatch.setattr(rp, "RECONCILE_SNAPSHOT", tmp_path / "rec.json")
    _stub_broker_for_main(monkeypatch, {}, daily=lambda syms: {})

    with pytest.raises(SystemExit):
        rp.main(["--sweep-orphans", "--no-alert"])
    first = db.init_db().execute(
        "SELECT status, exit_ts FROM outcomes WHERE signal_id=?",
        (sid,)).fetchone()
    assert first["status"] == "closed"
    first_exit_ts = first["exit_ts"]

    # Second run: the now-closed outcome is no longer an OPEN candidate.
    with pytest.raises(SystemExit):
        rp.main(["--sweep-orphans", "--no-alert"])
    second = db.init_db().execute(
        "SELECT status, exit_ts FROM outcomes WHERE signal_id=?",
        (sid,)).fetchone()
    assert second["status"] == "closed"
    assert second["exit_ts"] == first_exit_ts, \
        "idempotent: a re-run must not re-close / re-stamp the outcome"


def test_main_without_flag_does_not_sweep(isolated_db, tmp_path, monkeypatch):
    """Guard: plain `python -m monitoring.reconcile_positions` (no flag) must
    NOT sweep orphans — sweep is opt-in."""
    conn = db.init_db()
    sid = _open_outcome(conn, strategy_id="trend-a", symbol="ZZZ",
                        entry_price=50.0)
    db.record_snapshot_row(conn, "2026-05-15", {"symbol": "ZZZ", "close": 47.0})
    conn.commit()

    monkeypatch.setattr(rp, "RECONCILE_SNAPSHOT", tmp_path / "rec.json")
    _stub_broker_for_main(monkeypatch, {}, daily=lambda syms: {})
    with pytest.raises(SystemExit):
        rp.main(["--no-alert"])
    o = db.init_db().execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (sid,)).fetchone()
    assert o["status"] == "open", "no --sweep-orphans flag => no sweep"


# ---- B3: scheduled entry point reaches the orphan sweep ------------------
#
# The nightly Reconcile task runs `python -m monitoring.reconcile_positions`,
# i.e. main(). Before B3 that defaulted sweep_orphans=False, so the nightly
# task never swept. The chosen wiring: run_reconcile.bat passes --sweep-orphans
# (operator .bat edit) and main() routes that flag into sweep_orphan_outcomes.
# This test pins that the scheduled entry point (main) reaches the sweep.

def test_scheduled_main_flag_reaches_sweep_orphan_outcomes(
        isolated_db, tmp_path, monkeypatch):
    """B3 acceptance: main(['--sweep-orphans']) — the nightly Reconcile task's
    invocation — actually calls sweep_orphan_outcomes with the broker's held
    set. FAILS pre-B3 (no flag => sweep_orphan_outcomes never called)."""
    db.init_db()
    monkeypatch.setattr(rp, "RECONCILE_SNAPSHOT", tmp_path / "rec.json")
    _stub_broker_for_main(monkeypatch, {"NVDA": {"qty": 5.0}},
                          daily=lambda syms: {})

    calls = []
    real_sweep = rp.sweep_orphan_outcomes  # capture real callable (no recursion)

    def _spy(conn, held_symbols, **kw):
        calls.append(set(held_symbols))
        return real_sweep(conn, held_symbols, **kw)

    monkeypatch.setattr(rp, "sweep_orphan_outcomes", _spy)

    with pytest.raises(SystemExit):
        rp.main(["--sweep-orphans", "--no-alert"])

    assert len(calls) == 1, "scheduled entry point must invoke the sweep once"
    assert calls[0] == {"NVDA"}, "sweep must receive the broker's held set"


def test_reconcile_sweep_orphans_integration(isolated_db, tmp_path):
    """End-to-end: reconcile(sweep_orphans=True) closes the phantom outcome
    using broker truth, leaves the held one open."""
    conn = db.init_db()
    phantom = _open_outcome(conn, strategy_id="trend-a", symbol="ZZZ",
                            entry_price=50.0)
    db.record_snapshot_row(conn, "2026-05-15", {"symbol": "ZZZ", "close": 47.0})
    held = _open_outcome(conn, strategy_id="trend-b", symbol="NVDA",
                         entry_price=120.0)
    conn.commit()

    result = rp.reconcile(
        conn=conn,
        alpaca_positions_fn=lambda: {"NVDA": {"qty": 5.0,
                                              "avg_entry_price": 120.0}},
        save_path=tmp_path / "rec.json",
        alert=False,
        sweep_orphans=True,
        now_fn=lambda: "2026-05-16T21:00:00+00:00",
    )
    assert result["orphan_sweep"]["swept"] == 1

    o_phantom = conn.execute(
        "SELECT status, exit_reason FROM outcomes WHERE signal_id=?", (phantom,),
    ).fetchone()
    assert o_phantom["status"] == "closed"
    assert o_phantom["exit_reason"] == "reconciled_no_position"
    o_held = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (held,),
    ).fetchone()
    assert o_held["status"] == "open"
