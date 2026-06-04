"""test_intraday_edge_gate.py — Sprint 2 / M6 intraday cost/slippage edge gate.

Proves an intraday entry whose modeled expected move is below estimated
round-trip friction (spread + slippage) + buffer is vetoed, while a clearly-
profitable setup passes. Includes a wiring test through process_signals.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import intraday_edge_gate as eg  # noqa: E402


# --- pure gate ---------------------------------------------------------------

def test_thin_edge_vetoed():
    # Default friction = 0.02 + 2*0.03 = 0.08%; buffer 0.05% → threshold 0.13%.
    res = eg.evaluate_edge_gate(expected_move_pct=0.05)
    assert res["veto"] is True
    assert res["threshold_pct"] == pytest.approx(0.13)


def test_fat_edge_passes():
    res = eg.evaluate_edge_gate(expected_move_pct=0.80)
    assert res["veto"] is False


def test_missing_estimate_does_not_veto():
    res = eg.evaluate_edge_gate(expected_move_pct=None)
    assert res["veto"] is False
    assert "not applied" in res["reason"]


def test_friction_is_configurable():
    settings = {"intraday": {"spread_pct": 0.10, "slippage_pct": 0.10,
                             "min_edge_buffer_pct": 0.0}}
    # friction = 0.10 + 2*0.10 = 0.30%. A 0.25% move is now vetoed.
    res = eg.evaluate_edge_gate(expected_move_pct=0.25, settings=settings)
    assert res["friction_pct"] == pytest.approx(0.30)
    assert res["veto"] is True


def test_atr_to_pct():
    assert eg.expected_move_pct_from_atr(2.0, 100.0) == pytest.approx(2.0)
    assert eg.expected_move_pct_from_atr(None, 100.0) is None
    assert eg.expected_move_pct_from_atr(2.0, 0.0) is None


# --- wiring through process_signals -----------------------------------------

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
        "max_position_usd": 5000, "skip_intraday_signals": False,
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
                             bar_interval="1m")
        db.open_outcome(conn, signal_id=s, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(conn, signal_id=s, exit_ts=f"2024-01-{i+2:02d}",
                         exit_price=101.0, exit_reason="eod_close", bars_held=1)


def _seed_intraday_bars(conn, symbol, *, hl_spread, close=100.0, n=20):
    """Seed n intraday bars with a fixed high-low band so ATR ≈ hl_spread."""
    for i in range(n):
        ts = f"2026-06-04T15:{i:02d}:00+00:00"
        conn.execute(
            "INSERT INTO intraday_bars (symbol, ts_utc, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, 1000)",
            (symbol, ts, close, close + hl_spread / 2, close - hl_spread / 2,
             close),
        )
    conn.commit()


def test_intraday_thin_mover_vetoed_fat_mover_passes(conn):
    sid = "intraday-test"
    _seed_eligible(conn, sid)

    # THIN: ATR band ~0.05 on a $100 price → 0.05% expected move < 0.13% gate.
    _seed_intraday_bars(conn, "THIN", hl_spread=0.05)
    db.record_signal(conn, strategy_id=sid, symbol="THIN",
                     bar_ts="2026-06-04T15:20:00+00:00",
                     signal_type="long_entry", close=100.0, bar_interval="1m")
    # FAT: ATR band ~2.00 on $100 → 2% expected move, clears the gate.
    _seed_intraday_bars(conn, "FAT", hl_spread=2.00)
    db.record_signal(conn, strategy_id=sid, symbol="FAT",
                     bar_ts="2026-06-04T15:20:00+00:00",
                     signal_type="long_entry", close=100.0, bar_interval="1m")

    res = at.process_signals(conn, asof=date(2026, 6, 4), settings=_settings(),
                             bar_interval="1m")
    by_symbol = {}
    for a in res["actions"]:
        by_symbol.setdefault(a.get("symbol"), []).append(a.get("action"))

    assert "SKIP_INTRADAY_EDGE_GATE" in by_symbol.get("THIN", [])
    assert "SKIP_INTRADAY_EDGE_GATE" not in by_symbol.get("FAT", [])
    assert any(x in ("DRY_BUY", "BUY") for x in by_symbol.get("FAT", []))
