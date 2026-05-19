"""6.2.2 — Fractional-Kelly sizing tier.

Validates:
  - kelly_quarter_notional math against known-good inputs
  - Fallback to tiered (3.2.1) when Kelly guard fails
  - max_position_fraction cap
  - fraction_of_kelly multiplier configurability + hard ceiling at ½ Kelly
  - End-to-end via compute_notional + auto_trader integration
"""
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
            bar_ts=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
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


# ---------------------------------------------------------------------------
# _coerce_kelly_settings
# ---------------------------------------------------------------------------

def test_coerce_defaults_when_none():
    s = sizing._coerce_kelly_settings(None)
    assert s["fraction_of_kelly"] == 0.25
    assert s["max_position_fraction"] == 0.05
    assert s["min_samples"] == 50


def test_coerce_user_overrides():
    s = sizing._coerce_kelly_settings({
        "fraction_of_kelly": 0.5,
        "max_position_fraction": 0.10,
        "min_samples": 100,
    })
    assert s["fraction_of_kelly"] == 0.5
    assert s["max_position_fraction"] == 0.10
    assert s["min_samples"] == 100


def test_coerce_fraction_of_kelly_capped_at_half():
    """Hard ceiling at 0.5 (½ Kelly) — typo / config error can never push
    a strategy to full Kelly through this code path."""
    s = sizing._coerce_kelly_settings({"fraction_of_kelly": 0.9})
    assert s["fraction_of_kelly"] == 0.5  # clamped


def test_coerce_fraction_of_kelly_full_kelly_attempt_clamped():
    s = sizing._coerce_kelly_settings({"fraction_of_kelly": 1.0})
    assert s["fraction_of_kelly"] == 0.5
    # Even bigger.
    s = sizing._coerce_kelly_settings({"fraction_of_kelly": 100})
    assert s["fraction_of_kelly"] == 0.5


def test_coerce_garbage_falls_back():
    s = sizing._coerce_kelly_settings({
        "fraction_of_kelly": "huh",
        "max_position_fraction": -1,
        "min_samples": "no",
    })
    assert s["fraction_of_kelly"] == 0.25
    assert s["max_position_fraction"] == 0.05
    assert s["min_samples"] == 50


# ---------------------------------------------------------------------------
# kelly_quarter_notional — math
# ---------------------------------------------------------------------------

def test_kelly_quarter_uses_quarter_of_kelly_fraction(isolated_db):
    """50 outcomes, 25 wins of +2 / 25 losses of -1 → Kelly = 0.25
    (already at the cap). ¼ Kelly fraction = 0.25 × 0.25 = 0.0625 of
    portfolio. But max_position_fraction = 0.05 → caps at 0.05.
    portfolio_value = $10k → notional = $500."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=10_000,
        max_position_usd=5_000,
    )
    assert out["fraction"] == 0.25
    # ¼ × 0.25 = 0.0625 → capped at max_position_fraction=0.05.
    assert out["sized_fraction"] == 0.05
    assert out["notional"] == 500.0
    assert out["fallback"] is False
    # Raw Kelly = (0.5 × 3 - 1)/2 = 0.25, exactly at the cap — qualifying.
    assert out["guard_status"] == "qualifying"


def test_kelly_quarter_math_under_max_fraction(isolated_db):
    """A modest edge: 60 outcomes, 30 wins of +1 / 30 losses of -1.5 — p=0.5,
    b=0.667. f* = (0.5 × 1.667 - 1) / 0.667 = -0.25 → returns 0.
    Use a different distribution: 30 wins +1 / 30 losses -0.5 → p=0.5,
    b=2. f* = (0.5 × 3 - 1) / 2 = 0.25 (capped). ¼ × 0.25 = 0.0625 → capped
    at 0.05 again.

    To exercise under-cap: 60 outcomes 32W/28L of +1/-0.8 → p=0.533, b=1.25
    f* = (0.533 × 2.25 - 1) / 1.25 ≈ 0.16. ¼ × 0.16 = 0.04 (under 0.05 cap).
    """
    returns = [1.0] * 32 + [-0.8] * 28
    conn = _seed_outcomes("winner", returns)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=10_000,
        max_position_usd=5_000,
    )
    assert out["fraction"] is not None
    assert out["fraction"] > 0
    assert out["fraction"] < 0.25
    # Sized_fraction is ¼ × raw_kelly, should be < 0.05.
    assert out["sized_fraction"] == pytest.approx(0.25 * out["fraction"])
    assert out["sized_fraction"] < 0.05
    # Notional = sized_fraction × 10k
    assert out["notional"] == pytest.approx(out["sized_fraction"] * 10_000)
    assert out["fallback"] is False


def test_kelly_quarter_respects_max_position_usd(isolated_db):
    """Even when Kelly+portfolio would call for $1000, max_position_usd
    of $300 caps it."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=10_000,
        max_position_usd=300,
    )
    # Without the USD cap, would be 500. With $300 cap → 300.
    assert out["notional"] == 300.0


def test_kelly_quarter_max_fraction_override(isolated_db):
    """Raise max_position_fraction to 0.10 → sized_fraction can use the
    full ¼ × raw_kelly = 0.0625."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=10_000,
        max_position_usd=5_000,
        settings_kelly={"max_position_fraction": 0.10},
    )
    assert out["sized_fraction"] == pytest.approx(0.0625)
    assert out["notional"] == pytest.approx(625.0)


# ---------------------------------------------------------------------------
# Fallback when Kelly guard fails
# ---------------------------------------------------------------------------

def test_kelly_quarter_fallback_when_below_min_samples(isolated_db):
    """49 closed outcomes → Kelly guard fails → fallback=True."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 24 + [2.0])
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=10_000,
        max_position_usd=5_000,
    )
    assert out["fallback"] is True
    assert out["notional"] is None
    assert out["fraction"] is None
    assert out["guard_status"] == "need_more_samples"


def test_kelly_quarter_returns_zero_on_negative_edge(isolated_db):
    """Negative-edge strategy returns notional=0 (not None) — caller
    knows we evaluated and skipped."""
    conn = _seed_outcomes("loser", [1.0] * 20 + [-1.0] * 40)
    out = sizing.kelly_quarter_notional(
        conn, "loser", portfolio_value=10_000,
        max_position_usd=5_000,
    )
    assert out["fallback"] is False
    assert out["notional"] == 0.0
    assert out["fraction"] == 0.0


def test_kelly_quarter_min_samples_override(isolated_db):
    """Lower min_samples lets a strategy with fewer outcomes qualify."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 10)  # 20 outcomes
    # Default min_samples=50 → fallback.
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=10_000,
        max_position_usd=5_000,
    )
    assert out["fallback"] is True
    # min_samples=20 → qualifies.
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=10_000,
        max_position_usd=5_000,
        settings_kelly={"min_samples": 20},
    )
    assert out["fallback"] is False
    assert out["notional"] > 0


# ---------------------------------------------------------------------------
# compute_notional integration — fallback to tiered
# ---------------------------------------------------------------------------

def test_compute_notional_kelly_quarter_uses_kelly_when_qualifying(isolated_db):
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    out = sizing.compute_notional(
        conn, "winner",
        sizing_method="kelly_quarter",
        portfolio_value=10_000,
        max_position_usd=5_000,
    )
    assert out["sizing_method"] == "kelly_quarter"
    assert out["notional"] == 500.0
    assert "kelly_quarter" in out


def test_compute_notional_kelly_quarter_falls_back_to_tiered(isolated_db):
    """When Kelly guard fails, compute_notional drops to tiered tier."""
    # 10 outcomes → below the 5..19 range → tier 1.
    conn = _seed_outcomes("winner", [2.0, -1.0] * 5)
    out = sizing.compute_notional(
        conn, "winner",
        sizing_method="kelly_quarter",
        portfolio_value=10_000,
        max_position_usd=5_000,
        settings_tiered={"tier_1_usd": 750.0},
    )
    assert out["sizing_method"] == "tiered"
    assert out["tier"] == 1
    assert out["notional"] == 750.0
    assert out["kelly_quarter"]["fallback"] is True
    assert out["kelly_quarter"]["guard_status"] == "need_more_samples"


def test_compute_notional_kelly_quarter_explicit_fallback_to_fixed(isolated_db):
    """Caller can request a different fallback method."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 10)
    out = sizing.compute_notional(
        conn, "winner",
        sizing_method="kelly_quarter",
        portfolio_value=10_000,
        max_position_usd=1_234,
        fallback_method="fixed",
    )
    assert out["sizing_method"] == "fixed"
    assert out["notional"] == 1_234.0


# ---------------------------------------------------------------------------
# Half-Kelly path — for promoted strategies
# ---------------------------------------------------------------------------

def test_half_kelly_doubles_the_quarter(isolated_db):
    """With fraction_of_kelly=0.5 (½ Kelly), sized_fraction doubles."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=10_000,
        max_position_usd=5_000,
        settings_kelly={"fraction_of_kelly": 0.5, "max_position_fraction": 0.10},
    )
    # ½ × 0.25 = 0.125 → capped at max_position_fraction 0.10
    assert out["sized_fraction"] == 0.10
    assert out["notional"] == 1000.0


def test_half_kelly_is_the_hard_ceiling(isolated_db):
    """Even when caller asks for full Kelly, code clamps to ½ Kelly."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=10_000,
        max_position_usd=5_000,
        settings_kelly={
            "fraction_of_kelly": 1.0,  # request full Kelly
            "max_position_fraction": 0.50,
        },
    )
    # fraction_of_kelly clamped at 0.5; raw Kelly = 0.25 (capped) →
    # sized_fraction = 0.5 × 0.25 = 0.125. Capped at max_position_fraction 0.50.
    assert out["sized_fraction"] == 0.125
    assert out["fraction_of_kelly"] == 0.5  # clamped down


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_kelly_quarter_no_portfolio_value_returns_zero(isolated_db):
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=None,
        max_position_usd=5_000,
    )
    assert out["notional"] == 0.0
    assert out["fraction"] == 0.25
    assert out["fallback"] is False


def test_kelly_quarter_zero_portfolio_returns_zero(isolated_db):
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    out = sizing.kelly_quarter_notional(
        conn, "winner", portfolio_value=0,
        max_position_usd=5_000,
    )
    assert out["notional"] == 0.0


def test_kelly_quarter_listed_in_supported_methods():
    assert "kelly_quarter" in sizing.SUPPORTED_SIZING_METHODS


def test_normalize_sizing_method_accepts_kelly_quarter():
    assert sizing.normalize_sizing_method("kelly_quarter") == "kelly_quarter"
    assert sizing.normalize_sizing_method("KELLY_QUARTER") == "kelly_quarter"
