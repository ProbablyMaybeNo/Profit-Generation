"""Stage 1.1 (master plan, 2026-06-17) — ATR volatility-target position sizing.

Size so the initial-stop distance risks a fixed % of equity:
    qty = floor(equity * risk_pct / risk_per_share),  notional = qty * entry
capped by max_position_usd. This is the highest-leverage change in the plan —
constant dollar risk per trade (risk-of-ruin ~0 at 0.75%), shrinking size on
volatile names and growing it on calm ones.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import sizing  # noqa: E402


# ---------------------------------------------------------------------------
# atr_risk_notional — the pure sizing math
# ---------------------------------------------------------------------------

def test_worked_example_500_shares():
    # $100k equity, 0.75% risk = $750 budget; stop $1.50 away -> 500 shares.
    out = sizing.atr_risk_notional(
        portfolio_value=100_000, max_position_usd=1_000_000,
        inputs={"entry_price": 50.0, "risk_per_share": 1.5, "risk_pct": 0.0075},
    )
    assert out["fallback"] is False
    assert out["qty"] == 500
    assert out["notional"] == pytest.approx(25_000.0)
    assert out["risk_budget"] == pytest.approx(750.0)


def test_volatile_name_gets_smaller_size():
    calm = sizing.atr_risk_notional(
        portfolio_value=100_000, max_position_usd=1_000_000,
        inputs={"entry_price": 50.0, "risk_per_share": 1.5, "risk_pct": 0.0075},
    )
    volatile = sizing.atr_risk_notional(
        portfolio_value=100_000, max_position_usd=1_000_000,
        inputs={"entry_price": 50.0, "risk_per_share": 3.0, "risk_pct": 0.0075},
    )
    # Same equity + risk %, wider stop -> half the shares (constant $ risk).
    assert volatile["qty"] == 250
    assert volatile["qty"] < calm["qty"]


def test_max_position_usd_is_a_hard_ceiling():
    out = sizing.atr_risk_notional(
        portfolio_value=100_000, max_position_usd=10_000,
        inputs={"entry_price": 50.0, "risk_per_share": 1.5, "risk_pct": 0.0075},
    )
    # Risk math wants 500 sh = $25k, but the $10k cap binds.
    assert out["notional"] == pytest.approx(10_000.0)


def test_default_risk_pct_applied_when_unset():
    out = sizing.atr_risk_notional(
        portfolio_value=100_000, max_position_usd=1_000_000,
        inputs={"entry_price": 50.0, "risk_per_share": 1.5},
    )
    assert out["risk_pct"] == sizing.DEFAULT_RISK_PER_TRADE_PCT
    assert out["qty"] == 500


def test_fallback_when_no_stop_distance():
    out = sizing.atr_risk_notional(
        portfolio_value=100_000, max_position_usd=1_000_000,
        inputs={"entry_price": 50.0, "risk_per_share": None},
    )
    assert out["fallback"] is True
    assert out["notional"] == 0.0


def test_fallback_when_no_equity():
    out = sizing.atr_risk_notional(
        portfolio_value=None, max_position_usd=1_000_000,
        inputs={"entry_price": 50.0, "risk_per_share": 1.5},
    )
    assert out["fallback"] is True


# ---------------------------------------------------------------------------
# compute_notional routing
# ---------------------------------------------------------------------------

def test_compute_notional_routes_atr_risk(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    conn = db.init_db(test_db)
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    out = sizing.compute_notional(
        conn, "winner",
        sizing_method="atr_risk",
        portfolio_value=100_000,
        max_position_usd=1_000_000,
        atr_risk_inputs={"entry_price": 50.0, "risk_per_share": 1.5},
    )
    assert out["sizing_method"] == "atr_risk"
    assert out["qty"] == 500


def test_compute_notional_atr_risk_falls_back_to_tiered(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    conn = db.init_db(test_db)
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    out = sizing.compute_notional(
        conn, "winner",
        sizing_method="atr_risk",
        portfolio_value=100_000,
        max_position_usd=5_000,
        settings_tiered={"tier_0_usd": 5000},
        atr_risk_inputs={"entry_price": 50.0, "risk_per_share": None},
    )
    # No stop distance -> drops to the tiered fallback, never zeroes the trade.
    assert out["sizing_method"] == "tiered"
    assert out["atr_risk"]["fallback"] is True
    assert out["notional"] > 0
