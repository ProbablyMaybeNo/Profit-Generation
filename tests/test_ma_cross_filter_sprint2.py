"""test_ma_cross_filter_sprint2.py — Sprint 2 / M7 MA-cross regime/strength gate.

Proves a weak-regime MA-cross entry (thin EMA spread / flat slope) is filtered
while a strong-trend entry passes, plus wiring through process_signals scoped to
trend-ma-cross-20-50 (other strategies unaffected).
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import ma_cross_filter as mac  # noqa: E402


def _bars(closes):
    return [{"close": c, "high": c, "low": c, "open": c} for c in closes]


def _strong_uptrend(n=80, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


def _flat_chop(n=80, base=100.0):
    # Tiny alternating noise → near-zero spread + flat slope.
    return [base + (0.05 if i % 2 else -0.05) for i in range(n)]


# --- pure filter -------------------------------------------------------------

def test_strong_uptrend_confirmed():
    res = mac.evaluate_ma_cross_strength(_bars(_strong_uptrend()))
    assert res["confirmed"] is True
    assert res["spread_pct"] > 0
    assert res["slope_pct"] > 0


def test_flat_chop_vetoed():
    res = mac.evaluate_ma_cross_strength(_bars(_flat_chop()))
    assert res["confirmed"] is False
    assert "weak continuation" in res["reason"]


def test_insufficient_bars_not_blocked():
    res = mac.evaluate_ma_cross_strength(_bars([100.0] * 10))
    assert res["confirmed"] is True
    assert "insufficient" in res["reason"]


# --- wiring ------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield c
    c.close()


def _settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 0, "min_mean_ret_pct": -100.0, "min_sharpe_ish": -100.0,
        "max_position_usd": 5000, "skip_intraday_signals": True,
    }


def _seed_eligible(conn, sid):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid, "test_runs": [{
        "test_id": f"{sid}-A", "trades": 100,
        "total_return_pct": 100.0, "verdict": "PASS",
    }]}})
    for i in range(5):
        s = db.record_signal(conn, strategy_id=sid, symbol="W",
                             bar_ts=f"2024-01-{i+1:02d}",
                             signal_type="long_entry", close=100.0,
                             bar_interval="1d")
        db.open_outcome(conn, signal_id=s, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(conn, signal_id=s, exit_ts=f"2024-01-{i+2:02d}",
                         exit_price=102.0, exit_reason="long_exit_signal",
                         bars_held=1)


def test_weak_ma_cross_filtered_strong_passes(conn):
    sid = "trend-ma-cross-20-50"
    _seed_eligible(conn, sid)
    # WEAK symbol (flat chop) and STRONG symbol (uptrend).
    db.record_signal(conn, strategy_id=sid, symbol="WEAK", bar_ts="2026-05-14",
                     signal_type="long_entry", close=100.0, bar_interval="1d")
    db.record_signal(conn, strategy_id=sid, symbol="STRONG", bar_ts="2026-05-14",
                     signal_type="long_entry", close=179.0, bar_interval="1d")

    def fetcher(symbol):
        if symbol == "WEAK":
            return _bars(_flat_chop())
        return _bars(_strong_uptrend())

    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=_settings(), bars_fetcher=fetcher)
    by_symbol = {}
    for a in res["actions"]:
        by_symbol.setdefault(a.get("symbol"), []).append(a.get("action"))

    assert "SKIP_MA_CROSS_WEAK_CONTINUATION" in by_symbol.get("WEAK", [])
    assert "SKIP_MA_CROSS_WEAK_CONTINUATION" not in by_symbol.get("STRONG", [])
    assert any(x in ("DRY_BUY", "BUY") for x in by_symbol.get("STRONG", []))


def test_other_strategy_unaffected(conn):
    sid = "trend-donchian-breakout-20"
    _seed_eligible(conn, sid)
    db.record_signal(conn, strategy_id=sid, symbol="WEAK", bar_ts="2026-05-14",
                     signal_type="long_entry", close=100.0, bar_interval="1d")

    def fetcher(symbol):
        return _bars(_flat_chop())  # weak — but should NOT gate donchian

    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=_settings(), bars_fetcher=fetcher)
    actions = [a.get("action") for a in res["actions"]
               if a.get("strategy_id") == sid]
    assert "SKIP_MA_CROSS_WEAK_CONTINUATION" not in actions
    assert any(x in ("DRY_BUY", "BUY") for x in actions)
