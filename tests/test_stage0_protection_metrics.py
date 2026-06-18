"""Stage 0.6 (master plan, 2026-06-17) — daily naked-long protection metrics.

A filled long entry with no resting stop is a naked position. protection_metrics
counts filled entries vs the stops that actually rest for them (matched by
signal_id), and persist_report alerts when any are naked. Had this existed, the
119-stamped-vs-0-resting gap would have alerted immediately.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import daily_report as dr  # noqa: E402


def _seed_entry(conn, signal_id, *, with_stop, submitted="2026-05-14T14:00:00Z"):
    db.record_paper_trade(conn, {
        "alpaca_order_id": f"e{signal_id}", "signal_id": signal_id,
        "strategy_id": "winner", "symbol": "GDX", "side": "buy", "qty": 5,
        "order_type": "market", "status": "filled", "submitted_at": submitted,
        "fill_price": 100.0,
    })
    if with_stop:
        db.record_paper_trade(conn, {
            "alpaca_order_id": f"s{signal_id}", "signal_id": signal_id,
            "strategy_id": "winner", "symbol": "GDX", "side": "sell", "qty": 5,
            "order_type": "stop", "stop_price": 95.0, "status": "accepted",
            "submitted_at": submitted,
        })


def test_protection_metrics_counts_protected_and_naked(tmp_path, monkeypatch):
    db_path = tmp_path / "prot.db"
    conn = db.init_db(db_path)
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    s1 = db.record_signal(conn, strategy_id="winner", symbol="GDX",
                          bar_ts="2026-05-14", signal_type="long_entry",
                          close=100.0, bar_interval="1d")
    s2 = db.record_signal(conn, strategy_id="winner", symbol="SPY",
                          bar_ts="2026-05-15", signal_type="long_entry",
                          close=100.0, bar_interval="1d")
    _seed_entry(conn, s1, with_stop=True)    # protected
    _seed_entry(conn, s2, with_stop=False)   # naked
    conn.commit()

    m = dr.protection_metrics(conn, since_iso="2026-01-01")
    conn.close()
    assert m == {"entries": 2, "protected": 1, "naked": 1}


def test_maybe_alert_naked_fires_with_injected_alert_fn():
    sent = []
    fired = dr._maybe_alert_naked(
        {"entries": 3, "protected": 2, "naked": 1}, date(2026, 5, 14),
        alert_fn=sent.append,
    )
    assert fired is True
    assert sent and "NAKED LONGS" in sent[0]


def test_maybe_alert_naked_silent_when_none_naked():
    sent = []
    fired = dr._maybe_alert_naked(
        {"entries": 3, "protected": 3, "naked": 0}, date(2026, 5, 14),
        alert_fn=sent.append,
    )
    assert fired is False
    assert sent == []


def test_persist_report_wires_protection_metrics(tmp_path, monkeypatch):
    db_path = tmp_path / "persist_prot.db"
    _real_init_db = db.init_db
    monkeypatch.setattr(dr.db, "init_db", lambda *a, **k: _real_init_db(db_path))

    conn = db.init_db(db_path)
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    sig = db.record_signal(conn, strategy_id="winner", symbol="GDX",
                           bar_ts="2026-05-14T14:00:00", signal_type="long_entry",
                           close=100.0, bar_interval="1d")
    _seed_entry(conn, sig, with_stop=False)  # a naked filled entry today
    conn.commit()
    conn.close()

    captured = {}
    monkeypatch.setattr(
        dr, "_maybe_alert_naked",
        lambda metrics, sd, **k: captured.update(metrics=metrics, sd=sd) or True,
    )

    report = dr.DailyReport(report_date=date(2026, 5, 14), market_regime="x")
    counts = dr.persist_report(report, markdown="x")

    assert counts["entries_today"] == 1
    assert counts["entries_naked"] == 1
    assert counts["entries_protected"] == 0
    assert captured["metrics"]["naked"] == 1
