"""Stage 1.2 (master plan, 2026-06-17) — portfolio heat cap.

Risk-of-ruin is bounded by TOTAL open risk, not per-trade risk. portfolio_heat_usd
sums Σ(stop distance × size) across open positions; process_signals refuses new
entries once that would exceed risk.max_portfolio_heat_pct of equity — the cap
that survives a correlated (tech/semi) selloff stopping the book out at once.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


# ---------------------------------------------------------------------------
# portfolio_heat_usd — the open-risk accumulator
# ---------------------------------------------------------------------------

def _open_position(conn, sym, *, entry, qty, stop, bar_ts):
    sig = db.record_signal(conn, strategy_id="winner", symbol=sym,
                           bar_ts=bar_ts, signal_type="long_entry",
                           close=entry, bar_interval="1d")
    db.open_outcome(conn, signal_id=sig, entry_ts=bar_ts, entry_price=entry)
    db.record_paper_trade(conn, {
        "alpaca_order_id": f"buy-{sig}", "signal_id": sig,
        "strategy_id": "winner", "symbol": sym, "side": "buy", "qty": qty,
        "order_type": "market", "status": "filled", "submitted_at": bar_ts,
        "fill_price": entry,
    })
    if stop is not None:
        db.record_paper_trade(conn, {
            "alpaca_order_id": f"stop-{sig}", "signal_id": sig,
            "strategy_id": "winner", "symbol": sym, "side": "sell", "qty": qty,
            "order_type": "stop", "stop_price": stop, "status": "accepted",
            "submitted_at": bar_ts,
        })
    conn.commit()
    return sig


def test_portfolio_heat_usd_sums_open_risk(tmp_path):
    conn = db.init_db(tmp_path / "heat.db")
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    _open_position(conn, "AAA", entry=100.0, qty=10, stop=95.0, bar_ts="2026-05-14")   # 10*5  = 50
    _open_position(conn, "BBB", entry=200.0, qty=5, stop=190.0, bar_ts="2026-05-15")   # 5*10  = 50
    _open_position(conn, "CCC", entry=100.0, qty=10, stop=None, bar_ts="2026-05-16")   # 1000*0.05 = 50
    heat = at.portfolio_heat_usd(conn, default_stop_pct=0.05)
    conn.close()
    assert heat == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# the in-run gate via process_signals
# ---------------------------------------------------------------------------

def _seed_eligible(strategy_id="winner"):
    conn = db.init_db()
    pattern = [2.0, 1.0]
    for i in range(36):
        ret = pattern[i % 2]
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="SEED",
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


def test_heat_cap_blocks_second_entry(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)

    conn = _seed_eligible("winner")
    # Two fresh entry signals on two symbols, same day.
    for sym in ("AAA", "BBB"):
        db.record_signal(conn, strategy_id="winner", symbol=sym,
                         bar_ts="2026-05-14", signal_type="long_entry",
                         close=100.0, bar_interval="1d")

    # atr_risk sizes each entry to risk exactly 0.75% of $100k = $750. A heat cap
    # of 0.75% leaves room for exactly ONE position; the second must SKIP_HEAT_CAP.
    settings = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 100000, "sizing_method": "atr_risk",
        "risk_per_trade_pct": 0.0075,
        # Disable the Stage 2.2 regime sizing scale so this stays a pure
        # heat-cap test (no regime score persisted → would otherwise default
        # to the transitional 0.5x scale and halve each entry's risk).
        "risk": {"max_portfolio_heat_pct": 0.0075,
                 "regime_gate": {"enabled": False}},
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
    }
    bars = [{"high": 102, "low": 98, "close": 100}] * 16
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: bars,
        account_summary_fn=lambda: {"portfolio_value": 100000, "equity": 100000,
                                    "buying_power": 1_000_000, "cash": 1_000_000},
    )
    actions = [a for a in res["actions"]
               if a["strategy_id"] == "winner"
               and a["action"] in ("DRY_BUY", "SKIP_HEAT_CAP")]
    kinds = sorted(a["action"] for a in actions)
    assert kinds == ["DRY_BUY", "SKIP_HEAT_CAP"], actions
