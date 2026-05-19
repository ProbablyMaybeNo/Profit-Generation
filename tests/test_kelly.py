"""6.2.1 — Per-strategy Kelly fraction calculator.

Validates math correctness, sample-size guard at 50, negative-edge
handling, and the 0.25 cap.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import kelly  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "loser"}})
    yield test_db


def _seed_outcomes(strategy_id: str, returns, bar_interval="1d"):
    """Seed N closed outcomes for `strategy_id` with the given return_pct list."""
    conn = db.init_db()
    for i, ret in enumerate(returns):
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
            bar_ts=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            signal_type="long_entry", close=100.0,
            bar_interval=bar_interval,
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
# kelly_stats
# ---------------------------------------------------------------------------

def test_kelly_stats_empty():
    out = kelly.kelly_stats([])
    assert out["n"] == 0
    assert out["wins"] == 0
    assert out["losses"] == 0
    assert out["b"] == 0.0


def test_kelly_stats_known_distribution():
    # 6 wins of +2, 4 losses of -1 → p=0.6, b=2.0
    returns = [2.0] * 6 + [-1.0] * 4
    out = kelly.kelly_stats(returns)
    assert out["n"] == 10
    assert out["wins"] == 6
    assert out["losses"] == 4
    assert out["win_rate"] == pytest.approx(0.6)
    assert out["mean_winner"] == pytest.approx(2.0)
    assert out["mean_loser"] == pytest.approx(1.0)
    assert out["b"] == pytest.approx(2.0)


def test_kelly_stats_all_wins_returns_b_zero():
    out = kelly.kelly_stats([1.0] * 5)
    assert out["b"] == 0.0


def test_kelly_stats_all_losses_returns_b_zero():
    out = kelly.kelly_stats([-1.0] * 5)
    assert out["b"] == 0.0


# ---------------------------------------------------------------------------
# Raw formula correctness
# ---------------------------------------------------------------------------

def test_kelly_formula_textbook_example():
    """A bet paying 2:1 with 60% win rate → Kelly = 0.4."""
    # f* = (p × (b+1) - 1) / b = (0.6 × 3 - 1) / 2 = 0.8/2 = 0.4
    assert kelly._kelly_raw(0.6, 2.0) == pytest.approx(0.4)


def test_kelly_formula_break_even_returns_zero_or_negative():
    """50% win rate with even-money pays → Kelly = 0."""
    assert kelly._kelly_raw(0.5, 1.0) == pytest.approx(0.0)


def test_kelly_formula_negative_edge_returns_negative():
    """40% win rate with even-money → negative Kelly (we'd lose)."""
    # f* = (0.4 × 2 - 1) / 1 = -0.2
    assert kelly._kelly_raw(0.4, 1.0) == pytest.approx(-0.2)


def test_kelly_formula_zero_b_returns_zero():
    assert kelly._kelly_raw(0.6, 0) == 0.0
    assert kelly._kelly_raw(0.6, -1) == 0.0


# ---------------------------------------------------------------------------
# calc_kelly_fraction — sample guard
# ---------------------------------------------------------------------------

def test_calc_returns_none_when_fewer_than_min_samples(isolated_db):
    # 49 closed outcomes → below the 50 guard.
    conn = _seed_outcomes("winner", [2.0, -1.0] * 24 + [2.0])
    assert len(conn.execute(
        "SELECT * FROM outcomes WHERE status='closed'"
    ).fetchall()) == 49
    out = kelly.calc_kelly_fraction(conn, "winner")
    assert out is None


def test_calc_returns_fraction_at_min_samples(isolated_db):
    # Exactly 50 closed outcomes → guard satisfied.
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    out = kelly.calc_kelly_fraction(conn, "winner")
    assert out is not None
    # 25 wins of +2, 25 losses of -1 → p=0.5, b=2 → f* = (0.5×3 - 1)/2 = 0.25
    # That hits the cap exactly → returns 0.25.
    assert out == pytest.approx(0.25)


def test_calc_min_samples_override(isolated_db):
    """Caller can lower the guard for testing or experimental strategies."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 10)  # 20 outcomes
    assert kelly.calc_kelly_fraction(conn, "winner") is None
    assert kelly.calc_kelly_fraction(
        conn, "winner", min_samples=20,
    ) is not None


# ---------------------------------------------------------------------------
# calc_kelly_fraction — negative edge
# ---------------------------------------------------------------------------

def test_calc_returns_zero_on_negative_edge(isolated_db):
    """40% win rate with 1:1 payoff → negative Kelly → return 0."""
    # 20 wins of +1, 30 losses of -1 → p=0.4, b=1 → f*=-0.2
    conn = _seed_outcomes("loser", [1.0] * 20 + [-1.0] * 30)
    out = kelly.calc_kelly_fraction(conn, "loser")
    assert out == 0.0


def test_calc_returns_zero_on_all_wins(isolated_db):
    """All-wins distribution has undefined b → treat as no edge."""
    conn = _seed_outcomes("winner", [1.0] * 60)
    out = kelly.calc_kelly_fraction(conn, "winner")
    assert out == 0.0


def test_calc_returns_zero_on_all_losses(isolated_db):
    conn = _seed_outcomes("loser", [-1.0] * 60)
    out = kelly.calc_kelly_fraction(conn, "loser")
    assert out == 0.0


# ---------------------------------------------------------------------------
# calc_kelly_fraction — cap enforcement
# ---------------------------------------------------------------------------

def test_calc_caps_at_025(isolated_db):
    """A massive edge would push Kelly past 1.0; the cap brings it back."""
    # 90% win rate, +10 / -1 → p=0.9, b=10 → f* = (0.9×11 - 1)/10 = 0.89
    # Capped at 0.25.
    conn = _seed_outcomes("winner", [10.0] * 45 + [-1.0] * 5)
    out = kelly.calc_kelly_fraction(conn, "winner")
    assert out == 0.25


def test_calc_cap_override(isolated_db):
    """Caller can raise the cap (e.g. for testing — but production
    code in 6.2.2 must keep 0.25 default)."""
    conn = _seed_outcomes("winner", [10.0] * 45 + [-1.0] * 5)
    out = kelly.calc_kelly_fraction(conn, "winner", cap=0.5)
    # Raw was 0.89 → capped at 0.5 now.
    assert out == 0.5


def test_calc_under_cap_passes_through(isolated_db):
    """A modest edge produces a fraction under the cap — returns raw."""
    # 55% win rate, b=2 → f* = (0.55×3 - 1)/2 = 0.325 — above 0.25 cap
    # Use a smaller edge: 52%, b=1.5 → f* = (0.52×2.5 - 1)/1.5 ≈ 0.2
    returns = [1.5] * 26 + [-1.0] * 24
    conn = _seed_outcomes("winner", returns)
    out = kelly.calc_kelly_fraction(conn, "winner")
    expected = (0.52 * 2.5 - 1) / 1.5
    assert out == pytest.approx(round(expected, 4), abs=0.01)
    assert out < 0.25


# ---------------------------------------------------------------------------
# Strategy isolation — only the named strategy's outcomes count
# ---------------------------------------------------------------------------

def test_calc_isolates_strategy(isolated_db):
    """Outcomes from another strategy don't leak into the target's Kelly."""
    conn = _seed_outcomes("winner", [2.0, -1.0] * 25)
    # Pollute with loser outcomes
    _seed_outcomes("loser", [-5.0] * 50)
    out = kelly.calc_kelly_fraction(conn, "winner")
    assert out == pytest.approx(0.25)
    out_loser = kelly.calc_kelly_fraction(conn, "loser")
    assert out_loser == 0.0


# ---------------------------------------------------------------------------
# kelly_diagnostic — 6.2.3 dashboard card support
# ---------------------------------------------------------------------------

def test_diagnostic_need_more_samples(isolated_db):
    conn = _seed_outcomes("winner", [2.0, -1.0] * 5)  # 10 outcomes
    out = kelly.kelly_diagnostic(conn, "winner")
    assert out["guard"] == "need_more_samples"
    assert out["samples_needed"] == 40
    assert out["fraction"] is None
    assert out["stats"]["n"] == 10


def test_diagnostic_no_edge(isolated_db):
    conn = _seed_outcomes("loser", [1.0] * 20 + [-1.0] * 30)
    out = kelly.kelly_diagnostic(conn, "loser")
    assert out["guard"] == "no_edge"
    assert out["fraction"] == 0.0


def test_diagnostic_capped(isolated_db):
    conn = _seed_outcomes("winner", [10.0] * 45 + [-1.0] * 5)
    out = kelly.kelly_diagnostic(conn, "winner")
    assert out["guard"] == "capped"
    assert out["fraction"] == 0.25
    assert out["raw_fraction"] > 0.25


def test_diagnostic_qualifying(isolated_db):
    returns = [1.5] * 26 + [-1.0] * 24
    conn = _seed_outcomes("winner", returns)
    out = kelly.kelly_diagnostic(conn, "winner")
    assert out["guard"] == "qualifying"
    assert 0 < out["fraction"] < 0.25
