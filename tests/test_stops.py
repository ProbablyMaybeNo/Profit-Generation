import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import stops  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_outcomes_with_open(returns, open_at=None):
    """Seed N closed outcomes plus optionally one OPEN outcome for stop
    reconciliation tests."""
    conn = db.init_db()
    for i, ret in enumerate(returns):
        sid = db.record_signal(
            conn, strategy_id="winner", symbol="GDX",
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
    if open_at is not None:
        sid = db.record_signal(
            conn, strategy_id="winner", symbol="GDX",
            bar_ts=open_at, signal_type="long_entry",
            close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=open_at,
                        entry_price=100.0)
    return conn


def _bars(*hlc):
    """Build a bars list of dicts from triplets (high, low, close)."""
    return [{"high": h, "low": l, "close": c} for (h, l, c) in hlc]


# ---------------------------------------------------------------------------
# _coerce_multiple
# ---------------------------------------------------------------------------

def test_coerce_multiple_default():
    assert stops._coerce_multiple(None) == 0.0
    assert stops._coerce_multiple(0) == 0.0
    assert stops._coerce_multiple(-2) == 0.0
    assert stops._coerce_multiple("oops") == 0.0


def test_coerce_multiple_positive():
    assert stops._coerce_multiple(2) == 2.0
    assert stops._coerce_multiple("1.5") == 1.5


# ---------------------------------------------------------------------------
# compute_atr (simple mean of TR)
# ---------------------------------------------------------------------------

def test_compute_atr_constant_bars():
    # H=10, L=8, C=9 → TR=2 every bar → ATR=2
    bars = _bars(*[(10, 8, 9)] * 25)
    assert stops.compute_atr(bars, period=20) == pytest.approx(2.0)


def test_compute_atr_uses_prev_close_gap():
    # First bar (10,8,9), second bar (12,9,11): TR2 = max(3, 3, 0) = 3
    bars = _bars((10, 8, 9), (12, 9, 11)) + _bars(*[(11, 10, 10.5)] * 30)
    out = stops.compute_atr(bars, period=20)
    # last 20 TRs all = 1 except first which is 3; we're past it.
    assert out is not None
    assert out > 0


def test_compute_atr_too_few_bars_returns_none():
    bars = _bars(*[(10, 8, 9)] * 5)
    assert stops.compute_atr(bars, period=20) is None


def test_compute_atr_none_input_returns_none():
    assert stops.compute_atr(None, period=20) is None
    assert stops.compute_atr([], period=20) is None


def test_compute_atr_pandas_input():
    pd = pytest.importorskip("pandas")
    rows = [(10, 8, 9)] * 25
    df = pd.DataFrame(rows, columns=["High", "Low", "Close"])
    assert stops.compute_atr(df, period=20) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# compute_atr_wilder
# ---------------------------------------------------------------------------

def test_compute_atr_wilder_smooths():
    bars = _bars(*[(10, 8, 9)] * 25)
    assert stops.compute_atr_wilder(bars, period=20) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# stop_price_for
# ---------------------------------------------------------------------------

def test_stop_price_for_basic():
    assert stops.stop_price_for(100.0, 2.0, 1.5) == pytest.approx(97.0)
    assert stops.stop_price_for(100.0, 2.0, 3.0) == pytest.approx(94.0)


def test_stop_price_for_disabled_inputs():
    assert stops.stop_price_for(100.0, None, 1.5) is None
    assert stops.stop_price_for(100.0, 0, 1.5) is None
    assert stops.stop_price_for(100.0, 2.0, 0) is None
    assert stops.stop_price_for(None, 2.0, 1.5) is None


def test_stop_price_for_returns_none_when_stop_above_entry():
    # ATR larger than entry / multiple → stop ≤ 0
    assert stops.stop_price_for(2.0, 5.0, 1.0) is None


# ---------------------------------------------------------------------------
# quantize_stop_price (M0 — sub-penny tick fix)
# ---------------------------------------------------------------------------

def test_quantize_stop_price_2dp_for_dollar_and_up():
    # >= $1 must snap to a 1-cent tick (Alpaca rejects sub-penny).
    assert stops.quantize_stop_price(123.4567) == pytest.approx(123.46)
    assert stops.quantize_stop_price(741.9597) == pytest.approx(741.96)
    assert stops.quantize_stop_price(1.0) == pytest.approx(1.0)
    assert stops.quantize_stop_price(99.991) == pytest.approx(99.99)


def test_quantize_stop_price_keeps_finer_precision_below_dollar():
    # Sub-$1 names may use up to 4dp ticks.
    assert stops.quantize_stop_price(0.8755) == pytest.approx(0.8755)
    assert stops.quantize_stop_price(0.123456) == pytest.approx(0.1235)


def test_quantize_stop_price_sentinels():
    assert stops.quantize_stop_price(None) is None
    assert stops.quantize_stop_price(0) is None
    assert stops.quantize_stop_price(-5) is None


def test_submit_atr_stop_quantizes_subpenny(monkeypatch):
    import types
    fake_alpaca = types.ModuleType("alpaca")
    fake_trading = types.ModuleType("alpaca.trading")
    fake_requests = types.ModuleType("alpaca.trading.requests")
    fake_enums = types.ModuleType("alpaca.trading.enums")

    class StopOrderRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
    class OrderSide:
        BUY = "buy"
        SELL = "sell"
    class TimeInForce:
        DAY = "day"
        GTC = "gtc"
    fake_requests.StopOrderRequest = StopOrderRequest
    fake_enums.OrderSide = OrderSide
    fake_enums.TimeInForce = TimeInForce
    monkeypatch.setitem(sys.modules, "alpaca", fake_alpaca)
    monkeypatch.setitem(sys.modules, "alpaca.trading", fake_trading)
    monkeypatch.setitem(sys.modules, "alpaca.trading.requests", fake_requests)
    monkeypatch.setitem(sys.modules, "alpaca.trading.enums", fake_enums)

    captured = {}
    client = MagicMock()
    def submit(req):
        captured["req"] = req
        return MagicMock(id="stop-1")
    client.submit_order = submit
    # A real sub-penny value that Alpaca rejected (see INTRADAY_REALITY_CHECK).
    stops.submit_atr_stop(
        client, symbol="SPY", qty=1, stop_price=741.9597,
    )
    assert captured["req"].kwargs["stop_price"] == pytest.approx(741.96)


# ---------------------------------------------------------------------------
# submit_atr_stop (mocked, no alpaca-py needed at import time)
# ---------------------------------------------------------------------------

def test_submit_atr_stop_uses_alpaca_request(monkeypatch):
    # Build a stand-in alpaca module tree so the lazy imports inside
    # submit_atr_stop resolve in the test env.
    import types
    fake_alpaca = types.ModuleType("alpaca")
    fake_trading = types.ModuleType("alpaca.trading")
    fake_requests = types.ModuleType("alpaca.trading.requests")
    fake_enums = types.ModuleType("alpaca.trading.enums")

    class StopOrderRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
    class OrderSide:
        BUY = "buy"
        SELL = "sell"
    class TimeInForce:
        DAY = "day"
        GTC = "gtc"
    fake_requests.StopOrderRequest = StopOrderRequest
    fake_enums.OrderSide = OrderSide
    fake_enums.TimeInForce = TimeInForce
    monkeypatch.setitem(sys.modules, "alpaca", fake_alpaca)
    monkeypatch.setitem(sys.modules, "alpaca.trading", fake_trading)
    monkeypatch.setitem(sys.modules, "alpaca.trading.requests", fake_requests)
    monkeypatch.setitem(sys.modules, "alpaca.trading.enums", fake_enums)

    captured = {}
    client = MagicMock()
    def submit(req):
        captured["req"] = req
        return MagicMock(id="stop-1")
    client.submit_order = submit
    out = stops.submit_atr_stop(
        client, symbol="GDX", qty=10, stop_price=97.5,
        client_order_id="ato-x-GDX-b-2026-05-14-stop",
    )
    assert captured["req"].kwargs["symbol"] == "GDX"
    assert captured["req"].kwargs["stop_price"] == 97.5
    assert captured["req"].kwargs["side"] == "sell"
    assert captured["req"].kwargs["time_in_force"] == "gtc"
    assert captured["req"].kwargs["client_order_id"].endswith("-stop")


# ---------------------------------------------------------------------------
# reconcile_stop_fills
# ---------------------------------------------------------------------------

def test_reconcile_stop_fills_closes_open_outcome(isolated_db):
    conn = _seed_outcomes_with_open(
        [2.0, 1.0] * 18, open_at="2026-05-14",
    )
    # Seed a pending stop order in paper_trades.
    db.record_paper_trade(conn, {
        "alpaca_order_id": "stop-abc",
        "signal_id": None,
        "strategy_id": "winner", "symbol": "GDX",
        "side": "sell", "qty": 10,
        "order_type": "stop",
        "stop_price": 97.0,
        "submitted_at": "2026-05-14T14:00:00Z",
        "status": "accepted",
        "notes": "stop seed",
    })
    # Client returns a "filled" stop order.
    order = MagicMock()
    order.status = "filled"
    order.filled_avg_price = 97.0
    order.filled_at = "2026-05-15T17:00:00Z"
    client = MagicMock()
    client.get_order_by_id.return_value = order

    out = stops.reconcile_stop_fills(conn, client)
    assert out["checked"] == 1
    assert out["filled"] == 1
    assert out["closed"] == 1
    # The open outcome is now closed.
    row = conn.execute(
        "SELECT status, exit_reason, exit_price FROM outcomes "
        " WHERE status='closed' AND exit_reason='stop_loss_atr'"
    ).fetchone()
    assert row is not None
    assert row["exit_price"] == pytest.approx(97.0)
    # paper_trades row updated to filled + has fill_price.
    pt = conn.execute(
        "SELECT status, fill_price FROM paper_trades WHERE alpaca_order_id=?",
        ("stop-abc",),
    ).fetchone()
    assert pt["status"] == "filled"
    assert pt["fill_price"] == pytest.approx(97.0)
    conn.close()


def test_reconcile_stop_fills_records_mfe_mae(isolated_db):
    """F5 (audit 2026-06-03): when a bars_fetcher is supplied, a stop fill
    closes the outcome with exit_reason='stop_loss_atr' AND non-NULL MFE/MAE
    windowed over entry..fill. Without the fetcher (default) excursion stays
    NULL but the close still lands (back-compat)."""
    conn = _seed_outcomes_with_open([2.0, 1.0] * 18, open_at="2026-05-14")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "stop-mm",
        "strategy_id": "winner", "symbol": "GDX",
        "side": "sell", "qty": 10, "order_type": "stop",
        "stop_price": 96.0,
        "submitted_at": "2026-05-14T14:00:00Z",
        "status": "accepted",
    })
    order = MagicMock()
    order.status = "filled"
    order.filled_avg_price = 96.0
    order.filled_at = "2026-05-15T17:00:00Z"
    client = MagicMock()
    client.get_order_by_id.return_value = order

    # Entry 100.0 on 2026-05-14; bars between entry and fill: high 108 (+8%),
    # low 94 (-6%). Bars carry ts inside the window.
    bars = [
        {"high": 108.0, "low": 99.0, "close": 104.0,
         "ts": "2026-05-14T18:00:00+00:00"},
        {"high": 103.0, "low": 94.0, "close": 96.0,
         "ts": "2026-05-15T16:00:00+00:00"},
    ]
    out = stops.reconcile_stop_fills(conn, client, bars_fetcher=lambda s: bars)
    assert out["closed"] == 1
    row = conn.execute(
        "SELECT exit_reason, mfe_pct, mae_pct FROM outcomes "
        " WHERE status='closed' AND exit_reason='stop_loss_atr'"
    ).fetchone()
    assert row is not None
    assert row["mfe_pct"] == pytest.approx(0.08)
    assert row["mae_pct"] == pytest.approx(-0.06)
    conn.close()


def test_reconcile_stop_fills_no_fetcher_keeps_excursion_null(isolated_db):
    """Back-compat: no bars_fetcher → close still lands, MFE/MAE NULL."""
    conn = _seed_outcomes_with_open([2.0, 1.0] * 18, open_at="2026-05-14")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "stop-nm",
        "strategy_id": "winner", "symbol": "GDX",
        "side": "sell", "qty": 10, "order_type": "stop",
        "stop_price": 96.0,
        "submitted_at": "2026-05-14T14:00:00Z",
        "status": "accepted",
    })
    order = MagicMock()
    order.status = "filled"
    order.filled_avg_price = 96.0
    order.filled_at = "2026-05-15T17:00:00Z"
    client = MagicMock()
    client.get_order_by_id.return_value = order

    out = stops.reconcile_stop_fills(conn, client)
    assert out["closed"] == 1
    row = conn.execute(
        "SELECT mfe_pct, mae_pct FROM outcomes WHERE status='closed' "
        " AND exit_reason='stop_loss_atr'"
    ).fetchone()
    assert row["mfe_pct"] is None
    assert row["mae_pct"] is None
    conn.close()


def test_reconcile_stop_fills_ignores_unfilled(isolated_db):
    conn = _seed_outcomes_with_open(
        [2.0, 1.0] * 18, open_at="2026-05-14",
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": "stop-abc",
        "strategy_id": "winner", "symbol": "GDX",
        "side": "sell", "qty": 10, "order_type": "stop",
        "stop_price": 97.0,
        "submitted_at": "2026-05-14T14:00:00Z",
        "status": "accepted",
    })
    order = MagicMock()
    order.status = "new"  # still pending
    client = MagicMock()
    client.get_order_by_id.return_value = order
    out = stops.reconcile_stop_fills(conn, client)
    assert out["filled"] == 0
    assert out["closed"] == 0
    # Outcome still open.
    n_open = conn.execute(
        "SELECT COUNT(*) FROM outcomes WHERE status='open'"
    ).fetchone()[0]
    assert n_open == 1
    conn.close()


def test_reconcile_stop_fills_handles_client_error(isolated_db, monkeypatch):
    conn = _seed_outcomes_with_open([2.0, 1.0] * 18, open_at="2026-05-14")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "stop-abc",
        "strategy_id": "winner", "symbol": "GDX",
        "side": "sell", "qty": 10, "order_type": "stop",
        "stop_price": 97.0,
        "submitted_at": "2026-05-14T14:00:00Z",
        "status": "accepted",
    })
    client = MagicMock()
    client.get_order_by_id.side_effect = RuntimeError("alpaca timeout")
    out = stops.reconcile_stop_fills(conn, client)
    # Doesn't raise, doesn't close anything.
    assert out["filled"] == 0
    conn.close()


# ---------------------------------------------------------------------------
# auto_trader integration (dry-run path so submit_atr_stop isn't invoked)
# ---------------------------------------------------------------------------

def _winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000, "skip_intraday_signals": True,
    }


def _seed_for_buy(strategy_id="winner"):
    conn = db.init_db()
    # Alternating +2/+1 keeps stdev > 0 so the sharpe check passes.
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


def test_auto_trader_attaches_stop_in_dry_run(isolated_db):
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {**_winner_settings(), "stop_loss_atr_multiple": 2.0}
    # Constant ATR = 2 → stop = 100 - 2*2 = 96.
    bars = _bars(*[(101, 99, 100)] * 25)
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "DRY_BUY"
    stop = actions[0]["stop"]
    assert stop is not None
    assert stop["requested_multiple"] == 2.0
    assert stop["atr"] == pytest.approx(2.0)
    assert stop["stop_price"] == pytest.approx(96.0)
    assert stop["status"] == "dry_run"


def test_auto_trader_no_stop_when_setting_zero(isolated_db):
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {**_winner_settings(), "stop_loss_atr_multiple": 0}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: _bars(*[(101, 99, 100)] * 25),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["stop"] is None


def test_auto_trader_stop_no_bars_returns_status(isolated_db):
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {**_winner_settings(), "stop_loss_atr_multiple": 2.0}
    # No bars_fetcher passed → status no_bars.
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    assert stop["status"] == "no_bars"
    assert stop["stop_price"] is None


def test_auto_trader_stop_atr_unavailable_returns_no_stop(isolated_db):
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {**_winner_settings(), "stop_loss_atr_multiple": 2.0}
    # Only 5 bars → not enough for ATR(20).
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: _bars(*[(101, 99, 100)] * 5),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    assert stop["atr"] is None
    assert stop["status"] == "no_stop"
    assert stop["stop_price"] is None


def test_auto_trader_live_submits_stop_and_records_paper_trade(
    isolated_db, monkeypatch,
):
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")

    # Mock entry submit.
    def fake_submit_market(client, *, symbol, qty, side, client_order_id=None):
        order = MagicMock()
        order.id = "alpaca-entry-1"
        order.status = "filled"
        order.submitted_at = "2026-05-14T14:00:00Z"
        order.filled_avg_price = 100.0
        return order
    # Mock stop submit.
    submitted_stops = []
    def fake_submit_stop(client, *, symbol, qty, stop_price, client_order_id=None):
        submitted_stops.append({
            "symbol": symbol, "qty": qty, "stop_price": stop_price,
            "client_order_id": client_order_id,
        })
        order = MagicMock()
        order.id = "alpaca-stop-1"
        order.status = "accepted"
        order.submitted_at = "2026-05-14T14:00:01Z"
        return order
    monkeypatch.setattr(at, "_submit_market_order", fake_submit_market)
    monkeypatch.setattr(stops, "submit_atr_stop", fake_submit_stop)

    settings = {**_winner_settings(),
                "dry_run": False, "stop_loss_atr_multiple": 2.0}
    bars = _bars(*[(101, 99, 100)] * 25)
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(),
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "BUY"
    stop = actions[0]["stop"]
    assert stop["status"] == "submitted"
    assert stop["stop_price"] == pytest.approx(96.0)
    assert stop["order_id"] == "alpaca-stop-1"
    assert submitted_stops[0]["stop_price"] == pytest.approx(96.0)
    # paper_trades has both entry + stop rows.
    n_pt = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert n_pt == 2
    stop_row = conn.execute(
        "SELECT * FROM paper_trades WHERE alpaca_order_id='alpaca-stop-1'"
    ).fetchone()
    assert stop_row["side"] == "sell"
    assert stop_row["order_type"] == "stop"
    assert stop_row["stop_price"] == pytest.approx(96.0)
    conn.close()


# ---------------------------------------------------------------------------
# F5-LIVE: reconcile_stop_fills is wired into the live in-loop sync path.
#
# The earlier reconcile tests above call stops.reconcile_stop_fills in
# ISOLATION — they would still pass even if nothing in auto_trader ever
# called it. This test exercises the REAL production call path: it drives
# process_signals down the built_own_client branch (client=None + dry_run
# False + a client_factory stub) and asserts the loop itself invokes
# reconcile_stop_fills WITH a bars_fetcher, landing a stop fill in outcomes
# with non-NULL MFE/MAE and exit_reason='stop_loss_atr'. With the wiring
# absent this test fails (outcome stays open, excursion NULL).
# ---------------------------------------------------------------------------

def test_reconcile_stop_fills_wired_into_live_loop(isolated_db, monkeypatch):
    # One OPEN outcome for (winner, GDX) entered 2026-05-14, plus a pending
    # stop paper_trade pointing at a broker order that has since filled.
    conn = _seed_outcomes_with_open([2.0, 1.0] * 18, open_at="2026-05-14")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "stop-live-1",
        "strategy_id": "winner", "symbol": "GDX",
        "side": "sell", "qty": 10, "order_type": "stop",
        "stop_price": 96.0,
        "submitted_at": "2026-05-14T14:00:00Z",
        "status": "accepted",
    })

    filled_stop = MagicMock()
    filled_stop.status = "filled"
    filled_stop.filled_avg_price = 96.0
    filled_stop.filled_at = "2026-05-15T17:00:00Z"

    # The client the live loop builds itself. get_order_by_id answers for
    # both order_sync and reconcile_stop_fills.
    live_client = MagicMock()
    live_client.get_order_by_id.return_value = filled_stop

    # Bars windowed entry(100 @ 2026-05-14)..fill: high 108 (+8%), low 94
    # (-6%). _build_default_bars_fetcher would hit yfinance, so replace it
    # with a deterministic fetcher — asserting the wiring passes SOME
    # bars_fetcher through to reconcile (excursion lands non-NULL).
    bars = [
        {"high": 108.0, "low": 99.0, "close": 104.0,
         "ts": "2026-05-14T18:00:00+00:00"},
        {"high": 103.0, "low": 94.0, "close": 96.0,
         "ts": "2026-05-15T16:00:00+00:00"},
    ]
    monkeypatch.setattr(at, "_build_default_bars_fetcher",
                        lambda *a, **k: (lambda sym: bars))

    # No entry signal for asof → no live order submission; the loop's only
    # broker work is the in-loop order_sync + reconcile_stop_fills pass.
    settings = {**_winner_settings(), "dry_run": False}
    res = at.process_signals(
        conn,
        asof=date(2026, 5, 16),
        settings=settings,
        client=None,                       # forces built_own_client path
        client_factory=lambda: live_client,
        account_summary_fn=lambda: {"portfolio_value": 100000.0,
                                    "cash": 100000.0, "equity": 100000.0,
                                    "buying_power": 100000.0},
    )
    assert res["status"] == "OK"

    row = conn.execute(
        "SELECT exit_reason, exit_price, mfe_pct, mae_pct FROM outcomes "
        " WHERE status='closed' AND exit_reason='stop_loss_atr'"
    ).fetchone()
    assert row is not None, "live loop did not reconcile the stop fill"
    assert row["exit_price"] == pytest.approx(96.0)
    assert row["mfe_pct"] == pytest.approx(0.08)
    assert row["mae_pct"] == pytest.approx(-0.06)
    conn.close()
