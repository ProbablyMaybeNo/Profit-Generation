"""Stage 0.2 (master plan, 2026-06-17) — the protective stop must rest on the book.

Production reality at authoring: 0 of 409 paper_trades had a resting stop, yet
119 buys were stamped entry_stops='atr_initial'. Root cause: a fill-settlement
race. A market BUY returns status='accepted' with filled_avg_price=None and the
symbol isn't in the broker's positions list yet, so available_to_sell() reads 0
(not None) and safe_submit_stop SKIPs the stop -> naked long. Unit tests never
caught it because a MagicMock client makes positions read 'unknown' (None), which
hits the existing None-fallback.

The fix: (1) poll the entry to a settled fill (_await_entry_fill) so the stop
arms against a position the broker actually shows, and (2) when the entry is
confirmed filled but available reads 0, safe_submit_stop arms at the requested
qty anyway (entry_filled=True).
"""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402
from monitoring import stops as stops_mod  # noqa: E402


def _order(oid, status, filled_avg_price=None):
    o = MagicMock()
    o.id = oid
    o.status = status
    o.filled_avg_price = filled_avg_price
    o.submitted_at = "2026-05-14T14:00:00Z"
    return o


# ---------------------------------------------------------------------------
# _entry_is_live
# ---------------------------------------------------------------------------

def test_entry_is_live_filled():
    assert at._entry_is_live(_order("e1", "filled", 100.0)) is True


def test_entry_is_live_accepted():
    # A just-accepted (still-settling) buy is live -> stop must still arm.
    assert at._entry_is_live(_order("e2", "accepted", None)) is True


def test_entry_is_live_rejected():
    assert at._entry_is_live(_order("e3", "rejected")) is False


# ---------------------------------------------------------------------------
# safe_submit_stop — the entry_filled fallback
# ---------------------------------------------------------------------------

def test_safe_submit_stop_arms_on_zero_when_entry_filled(monkeypatch):
    monkeypatch.setattr(pm, "available_to_sell", lambda *a, **k: 0)
    monkeypatch.setattr(pm, "reconcile_exit_orders", lambda *a, **k: 0)
    seen = {}

    def submit_fn(c, *, symbol, qty, stop_price):
        seen.update(symbol=symbol, qty=qty, stop_price=stop_price)
        return _order("stop-1", "accepted")

    res = pm.safe_submit_stop(
        object(), symbol="NVDA", requested_qty=10, stop_price=95.0,
        submit_fn=submit_fn, reconcile=False, entry_filled=True,
    )
    assert res["action"] == "SUBMITTED"
    assert res["qty"] == 10
    assert seen["qty"] == 10


def test_safe_submit_stop_skips_on_zero_when_not_entry(monkeypatch):
    # A non-entry re-arm with the broker flat (avail==0) must NOT oversell-arm.
    monkeypatch.setattr(pm, "available_to_sell", lambda *a, **k: 0)
    monkeypatch.setattr(pm, "reconcile_exit_orders", lambda *a, **k: 0)
    calls = {"n": 0}

    def submit_fn(c, *, symbol, qty, stop_price):
        calls["n"] += 1
        return _order("stop-x", "accepted")

    res = pm.safe_submit_stop(
        object(), symbol="NVDA", requested_qty=10, stop_price=95.0,
        submit_fn=submit_fn, reconcile=False, entry_filled=False,
    )
    assert res["action"] == "SKIP_NO_AVAILABLE_QTY"
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# End-to-end: the production race, through process_signals
# ---------------------------------------------------------------------------

def _seed_eligible(strategy_id="winner"):
    conn = db.init_db()
    pattern = [2.0, 1.0]
    for i in range(36):
        ret = pattern[i % 2]
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )
    return conn


def test_entry_fills_race_still_rests_a_stop(tmp_path, monkeypatch):
    """The fill-settlement race: entry returns 'accepted'/None and the broker
    shows no position (avail==0). A stop must STILL be recorded on the book."""
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)

    conn = _seed_eligible("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")

    # Entry comes back UNSETTLED (the race) — live but not yet filled.
    def fake_submit_market(client, *, symbol, qty, side, client_order_id=None):
        return _order("entry-race", "accepted", filled_avg_price=None)

    def fake_submit_stop(client, *, symbol, qty, stop_price, client_order_id=None):
        return _order("stop-race", "accepted")

    monkeypatch.setattr(at, "_submit_market_order", fake_submit_market)
    monkeypatch.setattr(stops_mod, "submit_atr_stop", fake_submit_stop)

    # Client reproduces the race: the position isn't surfaced yet ->
    # available_to_sell() reads 0 (not None).
    client = MagicMock()
    client.get_open_position = lambda symbol: None
    client.get_all_positions = lambda *a, **k: []
    client.get_orders = lambda *a, **k: []
    client.cancel_order_by_id = lambda oid: None

    settings = {
        "enabled": True, "dry_run": False,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000,
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
    }
    bars = [{"high": 101, "low": 99, "close": 100}] * 16
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings, client=client,
        bars_fetcher=lambda sym: bars, sleep_fn=lambda s: None,
    )

    # The action reports a submitted stop...
    action = [a for a in res["actions"]
              if a["strategy_id"] == "winner" and a.get("action") == "BUY"][0]
    assert action["stop"]["status"] == "submitted"

    # ...and a real SELL STOP row rests in paper_trades (the ACCEPT criterion).
    stop_row = conn.execute(
        "SELECT order_type, stop_price, side FROM paper_trades "
        "WHERE order_type='stop'"
    ).fetchone()
    assert stop_row is not None, "naked-long regression: no resting stop recorded"
    assert stop_row["side"] == "sell"
    assert stop_row["stop_price"] == 95.0  # 100 - 2.5 * 2

    # The entry row IS stamped protected — the stop genuinely submitted (0.3).
    entry_row = conn.execute(
        "SELECT entry_stops FROM paper_trades WHERE alpaca_order_id='entry-race'"
    ).fetchone()
    assert entry_row["entry_stops"] == "atr_initial"


def test_rejected_entry_is_not_stamped_protected(tmp_path, monkeypatch):
    """Stage 0.3 — a buy whose stop never rests must NOT be stamped protected."""
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)

    conn = _seed_eligible("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")

    # Entry is REJECTED -> entry_filled False -> stop is skipped (no position).
    def fake_submit_market(client, *, symbol, qty, side, client_order_id=None):
        return _order("entry-rej", "rejected", filled_avg_price=None)

    stop_calls = {"n": 0}

    def fake_submit_stop(client, *, symbol, qty, stop_price, client_order_id=None):
        stop_calls["n"] += 1
        return _order("stop-rej", "accepted")

    monkeypatch.setattr(at, "_submit_market_order", fake_submit_market)
    monkeypatch.setattr(stops_mod, "submit_atr_stop", fake_submit_stop)

    client = MagicMock()
    client.get_open_position = lambda symbol: None
    client.get_all_positions = lambda *a, **k: []
    client.get_orders = lambda *a, **k: []
    client.cancel_order_by_id = lambda oid: None

    settings = {
        "enabled": True, "dry_run": False,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000,
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
    }
    bars = [{"high": 101, "low": 99, "close": 100}] * 16
    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings, client=client,
        bars_fetcher=lambda sym: bars, sleep_fn=lambda s: None,
    )

    assert stop_calls["n"] == 0, "no stop should submit for a rejected entry"
    entry_row = conn.execute(
        "SELECT entry_stops FROM paper_trades WHERE alpaca_order_id='entry-rej'"
    ).fetchone()
    assert entry_row["entry_stops"] is None, \
        "rejected entry must not be stamped as stop-protected"
