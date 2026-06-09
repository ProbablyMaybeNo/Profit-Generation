"""test_stop_protection_m7.py — Sprint 3 / M7: post-fill stop-protection verify.

A long that fills without a protective stop is a naked position — a gap-down has
no floor (the ENPH/AVGO -16% tail this week). M7 verifies, after every buy fill,
that a protective stop is actually attached (this run's submit OR a stop already
resting at the broker) and alerts loudly otherwise.

These tests drive the REAL production entry path (auto_trader._process_entry) and
prove an unprotected fill raises the alert while a protected fill passes silently.
FAILS on pre-M7 code, which never verified protection after the fill.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402
from monitoring import stops as stops_mod  # noqa: E402
import monitoring.telegram_alerter as tg  # noqa: E402


class _Order:
    def __init__(self, oid, symbol, qty, side, otype="market"):
        self.id = oid
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.order_type = otype
        self.filled_qty = 0
        self.status = "accepted"
        self.submitted_at = "2026-05-14T14:30:00Z"
        self.filled_avg_price = 100.0


class _Pos:
    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty


class ProtBroker:
    """Records orders; can be configured to FAIL stop submission (naked fill)
    or to accept it (protected fill). get_orders returns resting stops so the
    broker-truth verification path is exercised too."""

    def __init__(self, *, accept_stop=True):
        self._accept_stop = accept_stop
        self._orders = []
        self.submitted = []
        self._n = 0
        self._held = {}

    def get_open_position(self, symbol):
        q = self._held.get(symbol, 0)
        if not q:
            raise Exception("position does not exist")
        return _Pos(symbol, float(q))

    def get_all_positions(self):
        return [_Pos(s, float(q)) for s, q in self._held.items() if q]

    def get_orders(self, filter=None):
        return list(self._orders)

    def cancel_order_by_id(self, oid):
        self._orders = [o for o in self._orders if o.id != oid]

    def submit_order(self, req):
        # stops.submit_atr_stop path. Fail when configured to model a naked fill.
        if not self._accept_stop:
            raise Exception("stop rejected (40310000)")
        self._n += 1
        qty = int(getattr(req, "qty", 0))
        o = _Order(f"stop-{self._n}", getattr(req, "symbol", "?"), qty,
                   "sell", otype="stop")
        self._orders.append(o)
        return o


def _entry_submit(client, *, symbol, qty, side, client_order_id=None):
    # The entry market buy — record the held position so later reads see it.
    client._held[symbol] = client._held.get(symbol, 0) + qty
    return _Order(f"buy-{symbol}", symbol, qty, "buy")


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    monkeypatch.setattr(at, "_submit_market_order", _entry_submit)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    pm.reset_run_reservations()
    yield c
    pm.reset_run_reservations()
    c.close()


def _settings():
    return {
        "enabled": True, "dry_run": False,
        "min_outcomes": 1, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.0,
        "max_position_usd": 100000, "skip_intraday_signals": False,
        "stops": {"atr_multiplier": 2.0, "fixed_percent_fallback": 0.05},
    }


def _bars(symbol):
    return [{"high": 102.0, "low": 98.0, "close": 100.0} for _ in range(30)]


def _make_eligible(conn, sid):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    for i in range(3):
        s = db.record_signal(conn, strategy_id=sid, symbol="ELIG",
                             bar_ts=f"2024-01-0{i+1}", signal_type="long_entry",
                             close=100.0, bar_interval="1d")
        db.open_outcome(conn, signal_id=s, entry_ts=f"2024-01-0{i+1}",
                        entry_price=100.0)
        db.close_outcome(conn, signal_id=s, exit_ts=f"2024-01-0{i+2}",
                         exit_price=102.0, exit_reason="long_exit_signal",
                         bars_held=1)


def _entry_sig(conn, sid, sym):
    sig_id = db.record_signal(conn, strategy_id=sid, symbol=sym,
                              bar_ts="2026-05-14", signal_type="long_entry",
                              close=100.0, bar_interval="1d")
    return conn.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()


def test_unprotected_fill_raises_alert(conn, monkeypatch):
    """The stop submission FAILS, leaving a naked long. M7 must detect the
    unprotected fill and fire the alert."""
    alerts = []
    monkeypatch.setattr(tg, "send_message", lambda msg, **k: alerts.append(msg))
    sid, sym = "trend-donchian-breakout-20", "ENPH"
    _make_eligible(conn, sid)
    broker = ProtBroker(accept_stop=False)
    sig = _entry_sig(conn, sid, sym)

    action = at._process_entry(conn, broker, _settings(), sig, False,
                               portfolio_value=1_000_000.0,
                               bars_fetcher=_bars)
    assert action["action"] == "BUY", action
    prot = action["stop_protection"]
    assert prot is not None and prot["protected"] is False, prot
    assert prot["alerted"] is True
    assert alerts and "UNPROTECTED FILL" in alerts[0]
    assert "ENPH" in alerts[0]


def test_protected_fill_passes_silently(conn, monkeypatch):
    """The stop submits fine — the fill is protected, no alert."""
    alerts = []
    monkeypatch.setattr(tg, "send_message", lambda msg, **k: alerts.append(msg))
    sid, sym = "trend-donchian-breakout-20", "AVGO"
    _make_eligible(conn, sid)
    broker = ProtBroker(accept_stop=True)
    sig = _entry_sig(conn, sid, sym)

    action = at._process_entry(conn, broker, _settings(), sig, False,
                               portfolio_value=1_000_000.0,
                               bars_fetcher=_bars)
    assert action["action"] == "BUY", action
    prot = action["stop_protection"]
    assert prot["protected"] is True, prot
    assert prot["alerted"] is False
    assert alerts == []


def test_entry_is_blocked_when_required_stop_cannot_be_computed(conn, monkeypatch):
    """If stops are configured but ATR/fallback cannot produce a hard stop,
    the entry must not submit. No new position opens unless stops are explicitly
    disabled/exempted by config."""
    alerts = []
    monkeypatch.setattr(tg, "send_message", lambda msg, **k: alerts.append(msg))
    sid, sym = "trend-donchian-breakout-20", "GDX"
    _make_eligible(conn, sid)
    broker = ProtBroker(accept_stop=True)
    sig = _entry_sig(conn, sid, sym)
    settings = _settings()
    settings["stops"] = {"atr_multiplier": 2.0, "atr_period": 14}

    action = at._process_entry(conn, broker, settings, sig, False,
                               portfolio_value=1_000_000.0,
                               bars_fetcher=lambda symbol: _bars(symbol)[:5])

    assert action["action"] == "SKIP_UNPROTECTED_ENTRY", action
    assert action["stop"]["status"] == "no_stop"
    assert broker.submitted == []
    assert broker._held == {}
    assert alerts == []


def test_no_stops_configured_no_false_alarm(conn, monkeypatch):
    """Stops globally disabled (no settings.stops, no legacy multiple): the fill
    is intentionally unprotected -> stop_info is None -> no verify, no alert."""
    alerts = []
    monkeypatch.setattr(tg, "send_message", lambda msg, **k: alerts.append(msg))
    sid, sym = "trend-donchian-breakout-20", "SPY"
    _make_eligible(conn, sid)
    settings = {k: v for k, v in _settings().items() if k != "stops"}
    broker = ProtBroker(accept_stop=True)
    sig = _entry_sig(conn, sid, sym)

    action = at._process_entry(conn, broker, settings, sig, False,
                               portfolio_value=1_000_000.0,
                               bars_fetcher=_bars)
    assert action["action"] == "BUY", action
    assert action["stop_protection"] is None
    assert alerts == []


def test_verify_fill_protected_unit_broker_stop_counts():
    """A resting broker stop counts as protected even if this run didn't submit
    one (verified equivalent)."""
    broker = ProtBroker(accept_stop=True)
    # Pre-existing resting stop on the book.
    broker._orders.append(_Order("pre-stop", "QQQ", 5, "sell", otype="stop"))
    res = pm.verify_fill_protected(
        broker, symbol="QQQ",
        stop_info={"status": "submit_failed", "order_id": None},
        stops_expected=True, alert_fn=lambda m: None,
    )
    assert res["protected"] is True
    assert res["source"] == "broker_stop"
