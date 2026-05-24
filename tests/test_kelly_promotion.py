"""7.4.1 — Kelly tier promotion machinery.

Validates:
  - kelly_tier column added to strategies + kelly_tier_alerts table.
  - get/set tier helpers; default tier is 'quarter'.
  - Threshold math: n_closed, win_rate ±5pp, max_dd ≤ 1.5× backtest.
  - Live stats computed from closed outcomes (n, win_rate, max_dd).
  - Backtest stats parsed from raw_record_json.extra.test_runs.
  - alert_eligible_promotions fires once per (strategy, tier-transition).
  - confirm_promotion advances the tier on the ladder.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import kelly_promotion as kp  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


def _seed_strategy(conn, sid, *, test_runs=None):
    record = {
        "extra": {
            "strategy_id": sid,
            "title": sid,
            "current_verdict": "TESTED",
            "test_runs": test_runs or [],
        },
    }
    db.upsert_strategy(conn, record)


def _seed_closed_outcomes(conn, sid, returns):
    """Insert signal + open outcome + close outcome for each return."""
    for i, ret in enumerate(returns):
        sig_id = db.record_signal(
            conn, strategy_id=sid, symbol="SPY",
            bar_ts=f"2026-{(i // 25) + 1:02d}-{(i % 25) + 1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
            ts=f"2026-01-01T{i % 24:02d}:00:00",
        )
        if sig_id is None:
            continue
        db.open_outcome(
            conn, signal_id=sig_id, entry_ts=f"2026-01-{i+1:02d}",
            entry_price=100.0,
        )
        db.close_outcome(
            conn, signal_id=sig_id, exit_ts=f"2026-01-{i+2:02d}",
            exit_price=100.0 * (1.0 + ret / 100.0),
            exit_reason="test",
        )


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_ensure_schema_adds_kelly_tier_column(conn):
    kp.ensure_schema(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(strategies)"
    ).fetchall()}
    assert "kelly_tier" in cols


def test_ensure_schema_creates_kelly_tier_alerts(conn):
    kp.ensure_schema(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type='table' AND name='kelly_tier_alerts'"
    ).fetchone()
    assert row is not None


def test_ensure_schema_is_idempotent(conn):
    kp.ensure_schema(conn)
    kp.ensure_schema(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(strategies)"
    ).fetchall()}
    assert "kelly_tier" in cols


# ---------------------------------------------------------------------------
# Tier read / write
# ---------------------------------------------------------------------------

def test_default_tier_is_quarter(conn):
    _seed_strategy(conn, "a")
    assert kp.get_current_tier(conn, "a") == "quarter"


def test_set_and_read_tier(conn):
    _seed_strategy(conn, "a")
    kp.set_tier(conn, "a", "half")
    assert kp.get_current_tier(conn, "a") == "half"


# ---------------------------------------------------------------------------
# Stats — live + backtest
# ---------------------------------------------------------------------------

def test_compute_live_stats_empty_returns_zeros(conn):
    _seed_strategy(conn, "a")
    stats = kp.compute_live_stats(conn, "a")
    assert stats == {"n": 0, "win_rate": 0.0, "max_drawdown_pct": 0.0}


def test_compute_live_stats_counts_wins_and_max_dd(conn):
    _seed_strategy(conn, "a")
    # 6 outcomes: 4 wins, 2 losses → win_rate = 4/6
    _seed_closed_outcomes(conn, "a", [2.0, 3.0, -5.0, 1.5, 2.5, -3.0])
    stats = kp.compute_live_stats(conn, "a")
    assert stats["n"] == 6
    assert stats["win_rate"] == pytest.approx(4 / 6, abs=1e-4)
    # max_dd should be a negative number (or 0 if equity never dipped).
    assert stats["max_drawdown_pct"] <= 0.0


def test_compute_backtest_stats_weights_win_rate_by_trades():
    record = {
        "extra": {
            "test_runs": [
                {"trades": 100, "win_rate_pct": 60.0,
                 "max_drawdown_pct": -8.0, "verdict": "PASS"},
                {"trades": 200, "win_rate_pct": 50.0,
                 "max_drawdown_pct": -12.0, "verdict": "PASS"},
            ],
        },
    }
    stats = kp.compute_backtest_stats(record)
    # Weighted: (60 * 100 + 50 * 200) / 300 = 53.333% → 0.5333
    assert stats["win_rate"] == pytest.approx(0.5333, abs=1e-3)
    # Worst dd across runs: -12.0
    assert stats["max_drawdown_pct"] == pytest.approx(-12.0)


def test_compute_backtest_stats_no_runs_returns_nones():
    stats = kp.compute_backtest_stats({"extra": {"test_runs": []}})
    assert stats["win_rate"] is None
    assert stats["max_drawdown_pct"] is None


def test_compute_backtest_stats_skips_scenario_runs():
    record = {
        "extra": {
            "test_runs": [
                {"trades": 100, "win_rate_pct": 60.0,
                 "max_drawdown_pct": -8.0, "verdict": "PASS"},
                {"trades": 50, "win_rate_pct": 30.0,
                 "max_drawdown_pct": -20.0, "scenario": "stress_2008",
                 "verdict": "INFO"},
            ],
        },
    }
    stats = kp.compute_backtest_stats(record)
    # Only the non-scenario run counts.
    assert stats["win_rate"] == pytest.approx(0.6)
    assert stats["max_drawdown_pct"] == pytest.approx(-8.0)


# ---------------------------------------------------------------------------
# Eligibility math
# ---------------------------------------------------------------------------

def test_eligible_when_all_thresholds_met():
    elig = kp.evaluate_promotion_eligibility(
        live_stats={"n": 250, "win_rate": 0.52, "max_drawdown_pct": -7.0},
        backtest_stats={"win_rate": 0.55, "max_drawdown_pct": -10.0},
    )
    assert elig["eligible"] is True
    assert elig["n_closed_ok"] is True
    assert elig["win_rate_ok"] is True
    assert elig["max_dd_ok"] is True


def test_fail_when_n_below_200():
    elig = kp.evaluate_promotion_eligibility(
        live_stats={"n": 150, "win_rate": 0.55, "max_drawdown_pct": -8.0},
        backtest_stats={"win_rate": 0.55, "max_drawdown_pct": -10.0},
    )
    assert elig["eligible"] is False
    assert elig["n_closed_ok"] is False


def test_fail_when_win_rate_drifts_more_than_5pp():
    elig = kp.evaluate_promotion_eligibility(
        live_stats={"n": 250, "win_rate": 0.45, "max_drawdown_pct": -8.0},
        backtest_stats={"win_rate": 0.55, "max_drawdown_pct": -10.0},
    )
    assert elig["eligible"] is False
    assert elig["win_rate_ok"] is False
    # delta is -0.10
    assert elig["win_rate_delta"] == pytest.approx(-0.10)


def test_fail_when_live_dd_exceeds_1_5x_backtest():
    """Live dd of -18% exceeds 1.5 × -10% = -15% bound."""
    elig = kp.evaluate_promotion_eligibility(
        live_stats={"n": 250, "win_rate": 0.55, "max_drawdown_pct": -18.0},
        backtest_stats={"win_rate": 0.55, "max_drawdown_pct": -10.0},
    )
    assert elig["eligible"] is False
    assert elig["max_dd_ok"] is False


def test_dd_within_bound_passes():
    """Live dd of -14% within bound 1.5 × -10% = -15%."""
    elig = kp.evaluate_promotion_eligibility(
        live_stats={"n": 250, "win_rate": 0.55, "max_drawdown_pct": -14.0},
        backtest_stats={"win_rate": 0.55, "max_drawdown_pct": -10.0},
    )
    assert elig["max_dd_ok"] is True


def test_no_backtest_data_fails_gracefully():
    elig = kp.evaluate_promotion_eligibility(
        live_stats={"n": 250, "win_rate": 0.55, "max_drawdown_pct": -8.0},
        backtest_stats={"win_rate": None, "max_drawdown_pct": None},
    )
    assert elig["eligible"] is False
    assert elig["win_rate_ok"] is False
    assert elig["max_dd_ok"] is False


# ---------------------------------------------------------------------------
# Alert dedupe + send
# ---------------------------------------------------------------------------

def test_alert_dedupe_prevents_double_send(conn):
    _seed_strategy(conn, "a", test_runs=[
        {"trades": 200, "win_rate_pct": 55.0, "max_drawdown_pct": -10.0,
         "verdict": "PASS"},
    ])
    # 250 outcomes with ~55% win rate.
    returns = [1.0] * 138 + [-1.0] * 112  # 55.2% win rate, n=250
    _seed_closed_outcomes(conn, "a", returns)

    sent_messages = []

    def fake_send(msg):
        sent_messages.append(msg)
        return True

    # First call sends 1 alert.
    alerted = kp.alert_eligible_promotions(conn, send_fn=fake_send)
    assert len(alerted) == 1
    assert len(sent_messages) == 1

    # Second call: same eligibility, but alert already logged → no send.
    alerted2 = kp.alert_eligible_promotions(conn, send_fn=fake_send)
    assert len(alerted2) == 0
    assert len(sent_messages) == 1


def test_alert_dedupe_resets_after_tier_change(conn):
    """If the tier is manually promoted, the next promotion (e.g. half→full,
    when ladder is extended) would be a NEW (current, candidate) pair and
    should be alertable again."""
    _seed_strategy(conn, "a", test_runs=[
        {"trades": 200, "win_rate_pct": 55.0, "max_drawdown_pct": -10.0,
         "verdict": "PASS"},
    ])
    kp.record_alert_sent(
        conn, strategy_id="a",
        current_tier="quarter", candidate_tier="half",
    )
    # Verify deduped at current pair.
    assert kp.alert_already_sent(
        conn, strategy_id="a",
        current_tier="quarter", candidate_tier="half",
    ) is True
    # Different transition not yet alerted.
    assert kp.alert_already_sent(
        conn, strategy_id="a",
        current_tier="half", candidate_tier="full",
    ) is False


def test_alert_not_sent_when_ineligible(conn):
    _seed_strategy(conn, "a", test_runs=[
        {"trades": 200, "win_rate_pct": 55.0, "max_drawdown_pct": -10.0,
         "verdict": "PASS"},
    ])
    # Only 50 outcomes → fails n_closed threshold.
    _seed_closed_outcomes(conn, "a", [1.0] * 50)
    sent = []

    def fake_send(msg):
        sent.append(msg)
        return True

    alerted = kp.alert_eligible_promotions(conn, send_fn=fake_send)
    assert alerted == []
    assert sent == []


def test_alert_send_failure_does_not_record(conn):
    """If telegram send returns False, the alert is NOT recorded — next
    invocation will retry."""
    _seed_strategy(conn, "a", test_runs=[
        {"trades": 200, "win_rate_pct": 55.0, "max_drawdown_pct": -10.0,
         "verdict": "PASS"},
    ])
    returns = [1.0] * 138 + [-1.0] * 112
    _seed_closed_outcomes(conn, "a", returns)

    def failing_send(_msg):
        return False

    alerted = kp.alert_eligible_promotions(conn, send_fn=failing_send)
    assert alerted == []
    assert kp.alert_already_sent(
        conn, strategy_id="a",
        current_tier="quarter", candidate_tier="half",
    ) is False


# ---------------------------------------------------------------------------
# Promotion confirmation
# ---------------------------------------------------------------------------

def test_confirm_promotion_advances_tier(conn):
    _seed_strategy(conn, "a")
    new_tier = kp.confirm_promotion(conn, "a")
    assert new_tier == "half"
    assert kp.get_current_tier(conn, "a") == "half"


def test_confirm_promotion_at_top_returns_none(conn):
    _seed_strategy(conn, "a")
    kp.set_tier(conn, "a", "half")
    # No half → ?? in the ladder.
    new_tier = kp.confirm_promotion(conn, "a")
    assert new_tier is None
    assert kp.get_current_tier(conn, "a") == "half"


# ---------------------------------------------------------------------------
# Evaluate_strategy end-to-end
# ---------------------------------------------------------------------------

def test_evaluate_strategy_returns_full_payload(conn):
    _seed_strategy(conn, "a", test_runs=[
        {"trades": 200, "win_rate_pct": 55.0, "max_drawdown_pct": -10.0,
         "verdict": "PASS"},
    ])
    returns = [1.0] * 138 + [-1.0] * 112
    _seed_closed_outcomes(conn, "a", returns)
    verdict = kp.evaluate_strategy(conn, "a")
    assert verdict["strategy_id"] == "a"
    assert verdict["current_tier"] == "quarter"
    assert verdict["candidate_tier"] == "half"
    assert verdict["live_stats"]["n"] == 250
    assert verdict["backtest_stats"]["win_rate"] == pytest.approx(0.55)
    assert verdict["eligibility"]["eligible"] is True
