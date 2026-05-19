"""6.1.3 — Regime-aware ATR multiplier.

k_effective = k_base × regime_multiplier where the multiplier widens
stops in chop and tightens them in low-vol regimes. Multiplier hard-
capped to [0.7, 1.5] to prevent extreme stops.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import sizing  # noqa: E402
from monitoring import regime_router as rr  # noqa: E402


# ---------------------------------------------------------------------------
# regime_router.stop_regime_multiplier — defaults
# ---------------------------------------------------------------------------

def test_default_choppy_widens_stops():
    assert rr.stop_regime_multiplier("choppy") == 1.25


def test_default_low_vol_tightens_stops():
    assert rr.stop_regime_multiplier("low_vol") == 0.85


def test_default_trending_down_slightly_wider():
    assert rr.stop_regime_multiplier("trending_down") == 1.10


def test_default_trending_up_neutral():
    assert rr.stop_regime_multiplier("trending_up") == 1.00


def test_default_mixed_neutral():
    assert rr.stop_regime_multiplier("mixed") == 1.00


def test_unknown_regime_returns_neutral():
    assert rr.stop_regime_multiplier("foobar") == 1.00
    assert rr.stop_regime_multiplier(None) == 1.00


# ---------------------------------------------------------------------------
# Confidence floor
# ---------------------------------------------------------------------------

def test_confidence_below_floor_returns_neutral():
    """When the classifier is unsure (< 0.6 confidence), don't trust
    the regime label — fall back to 1.0."""
    assert rr.stop_regime_multiplier("choppy", confidence=0.5) == 1.00
    assert rr.stop_regime_multiplier("choppy", confidence=0.0) == 1.00


def test_confidence_at_floor_trusts_label():
    """0.6 confidence is the threshold — at or above it, use the label."""
    assert rr.stop_regime_multiplier("choppy", confidence=0.6) == 1.25
    assert rr.stop_regime_multiplier("low_vol", confidence=0.7) == 0.85


def test_confidence_none_trusts_label():
    """Confidence=None means classifier didn't report a confidence
    score — caller is asking us to trust the label."""
    assert rr.stop_regime_multiplier("choppy", confidence=None) == 1.25


def test_garbage_confidence_returns_neutral():
    """A non-numeric confidence is treated as 'unknown' → neutral."""
    assert rr.stop_regime_multiplier("choppy", confidence="hmm") == 1.00


def test_custom_confidence_floor():
    """Caller can raise the floor for stricter confidence requirements."""
    assert rr.stop_regime_multiplier(
        "choppy", confidence=0.7, confidence_floor=0.8,
    ) == 1.00
    assert rr.stop_regime_multiplier(
        "choppy", confidence=0.85, confidence_floor=0.8,
    ) == 1.25


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------

def test_overrides_capped_above_15():
    """Override of 2.0 is clamped to 1.5."""
    out = rr.stop_regime_multiplier(
        "choppy", overrides={"choppy": 2.0},
    )
    assert out == 1.5


def test_overrides_capped_below_07():
    """Override of 0.3 is clamped to 0.7."""
    out = rr.stop_regime_multiplier(
        "low_vol", overrides={"low_vol": 0.3},
    )
    assert out == 0.7


def test_overrides_inside_range_pass_through():
    out = rr.stop_regime_multiplier(
        "choppy", overrides={"choppy": 1.4},
    )
    assert out == 1.4


def test_override_garbage_ignored():
    """A non-numeric override falls back to the default for that regime."""
    out = rr.stop_regime_multiplier(
        "choppy", overrides={"choppy": "bad"},
    )
    assert out == 1.25
    out = rr.stop_regime_multiplier(
        "choppy", overrides={"choppy": -1.0},
    )
    assert out == 1.25


def test_partial_overrides_keep_other_defaults():
    out = rr.stop_regime_multiplier(
        "low_vol", overrides={"choppy": 1.4},
    )
    assert out == 0.85  # low_vol default still applies


# ---------------------------------------------------------------------------
# resolve_initial_stop integration
# ---------------------------------------------------------------------------

def test_resolve_initial_stop_no_regime_no_multiplier_applied():
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="x",
        side="long",
        settings_stops={"atr_multiplier": 2.5},
    )
    assert out["regime_multiplier"] == 1.0
    assert out["base_multiplier"] == 2.5
    assert out["multiplier"] == 2.5
    # stop = 100 - 2.5×2 = 95
    assert out["stop_price"] == pytest.approx(95.0)


def test_resolve_initial_stop_choppy_widens():
    """With k_base=2.5 and choppy multiplier 1.25 → k_eff = 3.125
    → stop = 100 - 3.125×2 = 93.75."""
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="x",
        side="long",
        settings_stops={"atr_multiplier": 2.5},
        regime="choppy",
    )
    assert out["regime_multiplier"] == 1.25
    assert out["base_multiplier"] == 2.5
    assert out["multiplier"] == pytest.approx(3.125)
    assert out["stop_price"] == pytest.approx(93.75)
    assert out["regime"] == "choppy"


def test_resolve_initial_stop_low_vol_tightens():
    """k_base=2.5 × 0.85 = 2.125 → stop = 100 - 2.125×2 = 95.75."""
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="x",
        side="long",
        settings_stops={"atr_multiplier": 2.5},
        regime="low_vol",
    )
    assert out["regime_multiplier"] == 0.85
    assert out["multiplier"] == pytest.approx(2.125)
    assert out["stop_price"] == pytest.approx(95.75)


def test_resolve_initial_stop_low_confidence_neutral():
    """Confidence below the 0.6 floor → multiplier stays at 1.0
    regardless of regime label."""
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="x",
        side="long",
        settings_stops={"atr_multiplier": 2.5},
        regime="choppy",
        regime_confidence=0.45,
    )
    assert out["regime_multiplier"] == 1.0
    assert out["multiplier"] == 2.5


def test_resolve_initial_stop_regime_overrides_from_settings():
    """settings.stops.regime_multipliers overrides the defaults."""
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="x",
        side="long",
        settings_stops={
            "atr_multiplier": 2.5,
            "regime_multipliers": {"choppy": 1.40},
        },
        regime="choppy",
    )
    assert out["regime_multiplier"] == 1.40
    assert out["multiplier"] == pytest.approx(3.5)


def test_resolve_initial_stop_override_clamped():
    """An override outside [0.7, 1.5] is clamped at the resolver layer."""
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="x",
        side="long",
        settings_stops={
            "atr_multiplier": 2.5,
            "regime_multipliers": {"choppy": 5.0},
        },
        regime="choppy",
    )
    assert out["regime_multiplier"] == 1.5
    # k_eff = 2.5 × 1.5 = 3.75 → stop = 100 - 3.75×2 = 92.5
    assert out["stop_price"] == pytest.approx(92.5)


def test_regime_aware_short_side_mirror():
    """Short stop with regime widening: entry + k_eff × ATR."""
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="x",
        side="short",
        settings_stops={"atr_multiplier": 2.5},
        regime="choppy",
    )
    # k_eff = 3.125 → stop = 100 + 3.125×2 = 106.25
    assert out["stop_price"] == pytest.approx(106.25)


# ---------------------------------------------------------------------------
# Auto-trader wiring: only activates when settings.stops.regime_aware=true
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _bars(*hlc):
    return [{"high": h, "low": l, "close": c} for (h, l, c) in hlc]


def _winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000, "skip_intraday_signals": True,
    }


def _seed_for_buy(strategy_id="winner"):
    conn = db.init_db()
    pattern = [2.0, 1.0]
    for i in range(36):
        ret = pattern[i % 2]
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
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


def test_auto_trader_skips_regime_aware_when_disabled(isolated_db):
    """Default behavior: settings.stops.regime_aware unset → no
    regime widening even though regime is choppy."""
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-19", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {
        **_winner_settings(),
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
    }
    # Seed a recent daily_report with regime=choppy so latest_regime
    # would return it if the path were active.
    conn.execute(
        "INSERT INTO daily_reports(report_date, market_regime, generated_at) "
        "VALUES ('2026-05-19', 'choppy', '2026-05-19T16:30:00Z')"
    )
    conn.commit()
    bars = _bars(*[(101, 99, 100)] * 16)
    res = at.process_signals(
        conn, asof=date(2026, 5, 19), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    # No regime widening applied — multiplier stays at 2.5.
    assert stop["regime_multiplier"] == 1.0
    assert stop["requested_multiple"] == 2.5
    assert stop["stop_price"] == pytest.approx(95.0)


def test_auto_trader_applies_regime_aware_when_enabled(isolated_db):
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-19", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {
        **_winner_settings(),
        "stops": {
            "atr_multiplier": 2.5, "atr_period": 14,
            "regime_aware": True,
        },
    }
    conn.execute(
        "INSERT INTO daily_reports(report_date, market_regime, generated_at) "
        "VALUES ('2026-05-19', 'choppy', '2026-05-19T16:30:00Z')"
    )
    conn.commit()
    bars = _bars(*[(101, 99, 100)] * 16)
    res = at.process_signals(
        conn, asof=date(2026, 5, 19), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    # k_eff = 2.5 × 1.25 = 3.125 → stop = 100 - 6.25 = 93.75
    assert stop["regime_multiplier"] == 1.25
    assert stop["base_multiplier"] == 2.5
    assert stop["requested_multiple"] == pytest.approx(3.125)
    assert stop["stop_price"] == pytest.approx(93.75)
    assert stop["regime"] == "choppy"
    assert stop["regime_aware"] is True


def test_auto_trader_regime_aware_low_vol_tightens(isolated_db):
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-19", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {
        **_winner_settings(),
        "stops": {
            "atr_multiplier": 2.5, "atr_period": 14,
            "regime_aware": True,
        },
    }
    conn.execute(
        "INSERT INTO daily_reports(report_date, market_regime, generated_at) "
        "VALUES ('2026-05-19', 'low_vol', '2026-05-19T16:30:00Z')"
    )
    conn.commit()
    bars = _bars(*[(101, 99, 100)] * 16)
    res = at.process_signals(
        conn, asof=date(2026, 5, 19), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    # k_eff = 2.5 × 0.85 = 2.125 → stop = 100 - 4.25 = 95.75
    assert stop["regime_multiplier"] == 0.85
    assert stop["stop_price"] == pytest.approx(95.75)
