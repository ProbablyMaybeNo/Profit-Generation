import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import sizing  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "loser"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_outcomes(strategy_id: str, returns):
    conn = db.init_db()
    for i, ret in enumerate(returns):
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0,
            bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )
    return conn


# ---------------------------------------------------------------------------
# edge_stats
# ---------------------------------------------------------------------------

def test_edge_stats_empty():
    out = sizing.edge_stats([])
    assert out == {"n": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0}


def test_edge_stats_basic():
    out = sizing.edge_stats([2.0, -1.0, 3.0, -2.0])
    assert out["n"] == 4
    assert out["win_rate"] == pytest.approx(0.5)
    assert out["avg_win"] == pytest.approx(2.5)
    assert out["avg_loss"] == pytest.approx(1.5)


def test_edge_stats_all_wins():
    out = sizing.edge_stats([2.0, 3.0])
    assert out["win_rate"] == pytest.approx(1.0)
    assert out["avg_win"] == pytest.approx(2.5)
    assert out["avg_loss"] == 0.0


def test_edge_stats_all_losses():
    out = sizing.edge_stats([-2.0, -3.0])
    assert out["win_rate"] == 0.0
    assert out["avg_loss"] == pytest.approx(2.5)
    assert out["avg_win"] == 0.0


# ---------------------------------------------------------------------------
# kelly_fraction
# ---------------------------------------------------------------------------

def test_kelly_fraction_classic_coin():
    # 60% win @ 1:1 payoff → f = 0.6 - 0.4 = 0.2 → capped at 0.25 (uncapped 20%)
    f = sizing.kelly_fraction(0.6, 1.0, 1.0)
    assert f == pytest.approx(0.2)


def test_kelly_fraction_caps_at_25():
    # 90% win @ 2:1 payoff → 0.85 → clamp to 0.25.
    f = sizing.kelly_fraction(0.9, 2.0, 1.0)
    assert f == pytest.approx(0.25)


def test_kelly_fraction_custom_cap():
    f = sizing.kelly_fraction(0.6, 1.0, 1.0, cap=0.10)
    assert f == pytest.approx(0.10)


def test_kelly_fraction_negative_edge_returns_zero():
    # 40% win @ 1:1 → negative Kelly → 0.0
    assert sizing.kelly_fraction(0.4, 1.0, 1.0) == 0.0


def test_kelly_fraction_zero_loss_returns_zero():
    # No loss data → undefined; return 0 not infinity.
    assert sizing.kelly_fraction(0.6, 1.0, 0.0) == 0.0


def test_kelly_fraction_zero_win_returns_zero():
    assert sizing.kelly_fraction(0.6, 0.0, 1.0) == 0.0


def test_kelly_fraction_extreme_win_rate_clamped():
    # win_rate=2.0 (bad input) → clamp p to 1.0 → still ≤ cap
    assert sizing.kelly_fraction(2.0, 1.0, 1.0) == pytest.approx(0.25)


def test_kelly_fraction_negative_win_rate_clamped():
    assert sizing.kelly_fraction(-0.3, 1.0, 1.0) == 0.0


# ---------------------------------------------------------------------------
# normalize_sizing_method
# ---------------------------------------------------------------------------

def test_normalize_sizing_method_defaults():
    assert sizing.normalize_sizing_method(None) == "fixed"
    assert sizing.normalize_sizing_method("") == "fixed"
    assert sizing.normalize_sizing_method("fixed") == "fixed"


def test_normalize_sizing_method_kelly():
    assert sizing.normalize_sizing_method("kelly") == "kelly"
    assert sizing.normalize_sizing_method("KELLY") == "kelly"


def test_normalize_sizing_method_unknown_falls_back():
    assert sizing.normalize_sizing_method("optimal_f") == "fixed"


# ---------------------------------------------------------------------------
# kelly_notional
# ---------------------------------------------------------------------------

def test_kelly_notional_uses_min_of_max_and_kelly_target(isolated_db):
    # 36 trades, ~60% win rate, +2/-1 → mean +0.83, kelly ~ 0.20.
    rets = [2.0, -1.0, 2.0, -1.0] * 9  # 36 outcomes, 50% win, +2/-1 payoff
    _seed_outcomes("winner", rets)
    conn = db.init_db()
    out = sizing.kelly_notional(
        conn, "winner",
        portfolio_value=100_000.0, max_position_usd=1000.0,
    )
    # f* = (2*0.5 - 0.5) / 2 = 0.25 → cap
    assert out["fraction"] == pytest.approx(0.25)
    # target = 0.25 * 100_000 = 25_000; capped by max_position_usd=1000 → 1000
    assert out["notional"] == pytest.approx(1000.0)
    conn.close()


def test_kelly_notional_below_max_position_usd_uses_kelly_target(isolated_db):
    rets = [2.0, -1.0, 2.0, -1.0] * 9
    _seed_outcomes("winner", rets)
    conn = db.init_db()
    out = sizing.kelly_notional(
        conn, "winner",
        portfolio_value=2_000.0,  # 0.25 * 2000 = 500 < 1000
        max_position_usd=1000.0,
    )
    assert out["notional"] == pytest.approx(500.0)
    conn.close()


def test_kelly_notional_zero_edge_returns_zero(isolated_db):
    rets = [-1.0, -1.0] * 10  # all losers
    _seed_outcomes("loser", rets)
    conn = db.init_db()
    out = sizing.kelly_notional(
        conn, "loser",
        portfolio_value=100_000.0, max_position_usd=1000.0,
    )
    assert out["notional"] == 0.0
    assert out["fraction"] == 0.0
    conn.close()


def test_kelly_notional_no_portfolio_value_returns_zero(isolated_db):
    rets = [2.0, -1.0, 2.0, -1.0] * 9
    _seed_outcomes("winner", rets)
    conn = db.init_db()
    out = sizing.kelly_notional(
        conn, "winner",
        portfolio_value=None, max_position_usd=1000.0,
    )
    assert out["notional"] == 0.0
    conn.close()


def test_kelly_notional_no_outcomes_returns_zero(isolated_db):
    conn = db.init_db()
    out = sizing.kelly_notional(
        conn, "untested",
        portfolio_value=100_000.0, max_position_usd=1000.0,
    )
    assert out["notional"] == 0.0
    assert out["fraction"] == 0.0
    conn.close()


# ---------------------------------------------------------------------------
# compute_notional
# ---------------------------------------------------------------------------

def test_compute_notional_fixed_returns_max_position(isolated_db):
    conn = db.init_db()
    out = sizing.compute_notional(
        conn, "anything", sizing_method="fixed",
        portfolio_value=None, max_position_usd=1000.0,
    )
    assert out["notional"] == 1000.0
    assert out["sizing_method"] == "fixed"


def test_compute_notional_kelly_dispatches(isolated_db):
    rets = [2.0, -1.0, 2.0, -1.0] * 9
    _seed_outcomes("winner", rets)
    conn = db.init_db()
    out = sizing.compute_notional(
        conn, "winner", sizing_method="kelly",
        portfolio_value=5_000.0, max_position_usd=10_000.0,
    )
    assert out["sizing_method"] == "kelly"
    # 0.25 * 5000 = 1250 < max → 1250.
    assert out["notional"] == pytest.approx(1250.0)
    conn.close()


def test_compute_notional_unknown_method_falls_back_to_fixed(isolated_db):
    conn = db.init_db()
    out = sizing.compute_notional(
        conn, "anything", sizing_method="optimal_f",
        portfolio_value=100_000.0, max_position_usd=1000.0,
    )
    assert out["sizing_method"] == "fixed"
    assert out["notional"] == 1000.0


# ---------------------------------------------------------------------------
# auto_trader integration
# ---------------------------------------------------------------------------

def _winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000, "skip_intraday_signals": True,
    }


def test_auto_trader_kelly_dry_run_uses_account_fn(isolated_db):
    rets = [2.0, -1.0, 2.0, -1.0] * 9  # 36 trades, kelly cap → 0.25
    conn = _seed_outcomes("winner", rets)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=10.0, bar_interval="1d")
    settings = {**_winner_settings(), "sizing_method": "kelly",
                "max_position_usd": 10_000}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {"portfolio_value": 8_000.0},
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "DRY_BUY"
    s = actions[0]["sizing"]
    assert s["sizing_method"] == "kelly"
    assert s["fraction"] == pytest.approx(0.25)
    # 0.25 * 8000 = 2000 < 10_000 → notional 2000 → qty floor(2000/10) = 200.
    assert s["notional"] == pytest.approx(2000.0)
    assert actions[0]["qty"] == 200


def test_auto_trader_kelly_zero_edge_skips_with_sizing_zero(isolated_db):
    _seed_outcomes("loser", [-1.0, -2.0] * 18)
    conn = db.init_db()
    db.record_signal(conn, strategy_id="loser", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=10.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "min_mean_ret_pct": -10.0,  # loosen eligibility for the test
                "min_sharpe_ish": -10.0,
                "sizing_method": "kelly",
                "cool_down_losers": 0}  # this test isolates the sizing path
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {"portfolio_value": 100_000.0},
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "loser"]
    assert actions[0]["action"] == "SKIP_SIZING_ZERO"


def test_auto_trader_fixed_default_unchanged(isolated_db):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = _winner_settings()  # sizing_method missing → defaults to fixed
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "DRY_BUY"
    assert actions[0]["sizing"]["sizing_method"] == "fixed"
    assert actions[0]["sizing"]["notional"] == 1000.0
    assert actions[0]["qty"] == 14


def test_auto_trader_kelly_account_fn_failure_falls_back_to_zero(
    isolated_db, monkeypatch,
):
    conn = _seed_outcomes("winner", [2.0, -1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=10.0, bar_interval="1d")
    def boom():
        raise RuntimeError("alpaca down")
    settings = {**_winner_settings(), "sizing_method": "kelly"}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=boom,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    # No portfolio_value → kelly returns notional=0 → SKIP_SIZING_ZERO.
    assert actions[0]["action"] == "SKIP_SIZING_ZERO"


# ---------------------------------------------------------------------------
# Tiered sizing (3.2.1)
# ---------------------------------------------------------------------------

def test_tier_for_boundaries():
    caps = sizing.TIERED_DEFAULTS
    assert sizing._tier_for(0, 0.0, caps) == 0
    assert sizing._tier_for(4, 99.0, caps) == 0
    assert sizing._tier_for(5, 0.0, caps) == 1
    assert sizing._tier_for(19, 0.0, caps) == 1
    assert sizing._tier_for(20, 0.0, caps) == 2
    assert sizing._tier_for(49, 99.0, caps) == 2
    assert sizing._tier_for(50, 0.31, caps) == 3
    assert sizing._tier_for(50, 0.30, caps) == 2  # boundary: needs > 0.3
    assert sizing._tier_for(50, 0.10, caps) == 2  # 50+ but low sharpe → stay tier 2
    assert sizing._tier_for(200, 1.5, caps) == 3


def test_coerce_tiered_settings_defaults():
    out = sizing._coerce_tiered_settings(None)
    assert out == sizing.TIERED_DEFAULTS


def test_coerce_tiered_settings_overrides_numeric():
    out = sizing._coerce_tiered_settings({
        "tier_0_usd": 100, "tier_3_usd": 5000,
    })
    assert out["tier_0_usd"] == 100.0
    assert out["tier_3_usd"] == 5000.0
    # Untouched keys fall through to defaults.
    assert out["tier_1_usd"] == 500.0


def test_coerce_tiered_settings_rejects_garbage():
    out = sizing._coerce_tiered_settings({
        "tier_0_usd": "garbage",
        "tier_1_usd": -100,         # negative → default
        "tier_3_min_sharpe": "bad",
    })
    assert out["tier_0_usd"] == 200.0  # default
    assert out["tier_1_usd"] == 500.0  # default (negative rejected)
    assert out["tier_3_min_sharpe"] == 0.3  # default


def test_tiered_notional_tier_0_few_outcomes(isolated_db):
    conn = _seed_outcomes("winner", [1.0, 2.0])  # only 2 outcomes
    out = sizing.tiered_notional(conn, "winner")
    assert out["tier"] == 0
    assert out["notional"] == 200.0


def test_tiered_notional_tier_1(isolated_db):
    conn = _seed_outcomes("winner", [1.0] * 10)
    out = sizing.tiered_notional(conn, "winner")
    assert out["tier"] == 1
    assert out["notional"] == 500.0


def test_tiered_notional_tier_2(isolated_db):
    conn = _seed_outcomes("winner", [1.0] * 30)
    out = sizing.tiered_notional(conn, "winner")
    assert out["tier"] == 2
    assert out["notional"] == 1000.0


def test_tiered_notional_tier_3_requires_sharpe(isolated_db):
    """50 outcomes with great mean / low variance → tier 3."""
    rets = [2.0, 2.5] * 30  # 60 outcomes, mean=2.25, sd≈0.25
    conn = _seed_outcomes("winner", rets)
    out = sizing.tiered_notional(conn, "winner")
    assert out["tier"] == 3
    assert out["notional"] == 2000.0
    assert out["sharpe"] > 0.3


def test_tiered_notional_tier_3_falls_back_to_tier_2_on_low_sharpe(isolated_db):
    """50+ outcomes but high variance / no edge → stays at tier 2."""
    rets = [10.0, -10.0] * 30  # 60 outcomes, mean ≈ 0, sharpe ≈ 0
    conn = _seed_outcomes("winner", rets)
    out = sizing.tiered_notional(conn, "winner")
    assert out["tier"] == 2
    assert out["notional"] == 1000.0


def test_tiered_notional_max_position_caps_tier_amount(isolated_db):
    conn = _seed_outcomes("winner", [1.0, 2.5] * 30)
    out = sizing.tiered_notional(conn, "winner",
                                   max_position_usd=750.0)
    assert out["tier"] == 3
    assert out["notional"] == 750.0  # capped by max_position_usd


def test_tiered_notional_custom_caps(isolated_db):
    conn = _seed_outcomes("winner", [1.0] * 30)
    out = sizing.tiered_notional(
        conn, "winner",
        settings_tiered={"tier_2_usd": 1500},
    )
    assert out["tier"] == 2
    assert out["notional"] == 1500.0


def test_compute_notional_tiered_dispatches(isolated_db):
    conn = _seed_outcomes("winner", [1.0, 2.5] * 30)
    out = sizing.compute_notional(
        conn, "winner",
        sizing_method="tiered",
        portfolio_value=None,
        max_position_usd=999999.0,
    )
    assert out["sizing_method"] == "tiered"
    assert out["tier"] == 3
    assert out["notional"] == 2000.0


def test_normalize_sizing_method_tiered():
    assert sizing.normalize_sizing_method("tiered") == "tiered"
    assert sizing.normalize_sizing_method("TIERED") == "tiered"


def test_auto_trader_tiered_dry_run_emits_tier(isolated_db):
    """End-to-end through process_signals: a fresh strategy gets tier 0."""
    conn = _seed_outcomes("winner", [1.0, 2.0])  # tier 0
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "sizing_method": "tiered",
                "max_position_usd": 999999.0,
                "min_outcomes": 1}
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "DRY_BUY"
    sizing_out = actions[0]["sizing"]
    assert sizing_out["sizing_method"] == "tiered"
    assert sizing_out["tier"] == 0
    assert sizing_out["notional"] == 200.0
