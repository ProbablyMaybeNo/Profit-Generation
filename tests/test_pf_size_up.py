"""test_pf_size_up.py — Sprint-1 M4 expectancy-tiered size-up.

Proves a proven high-PF/high-n strategy sizes larger than a thin/low-edge
one for the same price, that sizing never breaches the configured caps, and
that the boost is hard-clamped + opt-out.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import kelly as kelly_mod  # noqa: E402
from monitoring import sizing  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = db.init_db(tmp_path / "trading.db")
    yield c
    c.close()


def _seed(c, strategy_id, returns):
    db.upsert_strategy(c, {"extra": {"strategy_id": strategy_id}})
    for i, ret in enumerate(returns):
        s = db.record_signal(
            c, strategy_id=strategy_id, symbol="X",
            bar_ts=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(c, signal_id=s, entry_ts=f"2024-01-01",
                        entry_price=100.0)
        db.close_outcome(c, signal_id=s, exit_ts="2024-01-02",
                         exit_price=100.0 * (1 + ret / 100),
                         exit_reason="long_exit_signal", bars_held=1)


# Returns engineered for known profit factors over a big sample (n=200).
# high PF: 60 wins of +4, 40 losses of -1 -> gross 240/40 = PF 6.0
HIGH_PF = ([4.0] * 60 + [-1.0] * 40) * 2  # n=200, PF 6.0
# low PF: 55 wins of +1, 45 losses of -1 -> gross 110/45 = PF ~2.44... too high.
# tune to PF < 2.0: 50 wins +1.2, 50 losses -1.0 -> 60/50 = PF 1.2
LOW_PF = ([1.2] * 50 + [-1.0] * 50) * 2  # n=200, PF 1.2


PF_KELLY = {
    "fraction_of_kelly": 0.25,
    "max_position_fraction": 0.10,
    "min_samples": 50,
    "pf_size_up": {
        "enabled": True,
        "pf_threshold": 2.0,
        "boosted_max_position_fraction": 0.15,
    },
}


def test_profit_factor_helper():
    assert kelly_mod.profit_factor([4.0] * 3 + [-1.0] * 2) == pytest.approx(6.0)
    assert kelly_mod.profit_factor([1.0, 1.0]) is None  # no losses
    assert kelly_mod.profit_factor([]) is None


def test_high_pf_gets_boosted_fraction(conn):
    _seed(conn, "winner", HIGH_PF)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=100000.0,
        max_position_usd=100000.0, settings_kelly=PF_KELLY,
    )
    assert out["pf"] > 2.0
    assert out["pf_boosted"] is True
    assert out["max_position_fraction"] == pytest.approx(0.15)


def test_low_pf_keeps_base_fraction(conn):
    _seed(conn, "thin", LOW_PF)
    out = sizing.kelly_quarter_notional(
        conn, "thin", portfolio_value=100000.0,
        max_position_usd=100000.0, settings_kelly=PF_KELLY,
    )
    assert out["pf"] < 2.0
    assert out["pf_boosted"] is False
    assert out["max_position_fraction"] == pytest.approx(0.10)


def test_high_pf_sizes_larger_than_thin_same_price(conn):
    """Acceptance (a): high-PF/high-n strategy sizes larger than a thin one."""
    _seed(conn, "winner", HIGH_PF)
    _seed(conn, "thin", LOW_PF)
    big = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=100000.0,
        max_position_usd=100000.0, settings_kelly=PF_KELLY,
    )
    small = sizing.kelly_quarter_notional(
        conn, "thin", portfolio_value=100000.0,
        max_position_usd=100000.0, settings_kelly=PF_KELLY,
    )
    assert big["notional"] > small["notional"]


def test_sizing_never_exceeds_max_position_usd(conn):
    """Acceptance (b): even a boosted high-PF strategy is capped by
    max_position_usd."""
    _seed(conn, "winner", HIGH_PF)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=100000.0,
        max_position_usd=5000.0, settings_kelly=PF_KELLY,
    )
    assert out["notional"] <= 5000.0


def test_boosted_fraction_hard_clamped(conn):
    """A typo'd boosted fraction can't push a position past the safe
    ceiling (0.20)."""
    _seed(conn, "winner", HIGH_PF)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=100000.0,
        max_position_usd=100000.0,
        settings_kelly={
            **PF_KELLY,
            "pf_size_up": {"enabled": True, "pf_threshold": 2.0,
                           "boosted_max_position_fraction": 0.9},
        },
    )
    assert out["max_position_fraction"] <= sizing.HARD_CAP_BOOSTED_MAX_POSITION_FRACTION


def test_pf_size_up_disabled_keeps_base(conn):
    _seed(conn, "winner", HIGH_PF)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=100000.0,
        max_position_usd=100000.0,
        settings_kelly={**PF_KELLY,
                        "pf_size_up": {"enabled": False}},
    )
    assert out["pf_boosted"] is False
    assert out["max_position_fraction"] == pytest.approx(0.10)


def test_fraction_of_kelly_not_raised_by_boost(conn):
    """The boost touches max_position_fraction only — fraction_of_kelly
    stays at its conservative 0.25 (the M4 guardrail)."""
    _seed(conn, "winner", HIGH_PF)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=100000.0,
        max_position_usd=100000.0, settings_kelly=PF_KELLY,
    )
    assert out["fraction_of_kelly"] == pytest.approx(0.25)
