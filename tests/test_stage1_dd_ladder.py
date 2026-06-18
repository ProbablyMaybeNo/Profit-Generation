"""Stage 1.4 (master plan, 2026-06-17) — drawdown kill-switch ladder.

Config-only milestone over the existing drawdown_throttle + daily-loss breaker.
With atr_risk sizing the throttle's size multiplier scales the notional, which
scales qty, which scales per-trade risk — so 0.5x halves risk to 0.375%, exactly
the plan's spec. Ladder: halve at 15% account DD, quarter at 20%, halt + kill
switch at a catastrophic 25% (research target: keep DD < 25%); 3% daily-loss
breaker pauses new entries for the day.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import drawdown_throttle as dt  # noqa: E402

_CFG = {
    "halve_at_pct": 85.0, "quarter_at_pct": 80.0,
    "kill_at_pct": 75.0, "recover_at_pct": 90.0,
}


def test_ladder_halves_at_15pct_drawdown():
    info = dt.evaluate(current_pv=8400, peak_pv=10000, settings_throttle=_CFG)  # 16% DD
    assert info["multiplier"] == 0.5
    assert info["trip_kill_switch"] is False


def test_ladder_quarters_at_20pct_drawdown():
    info = dt.evaluate(current_pv=7900, peak_pv=10000, settings_throttle=_CFG)  # 21% DD
    assert info["multiplier"] == 0.25


def test_ladder_halts_and_trips_kill_switch_at_25pct():
    info = dt.evaluate(current_pv=7400, peak_pv=10000, settings_throttle=_CFG)  # 26% DD
    assert info["multiplier"] == 0.0
    assert info["trip_kill_switch"] is True


def test_ladder_full_size_above_15pct_drawdown():
    info = dt.evaluate(current_pv=8600, peak_pv=10000, settings_throttle=_CFG)  # 14% DD
    assert info["multiplier"] == 1.0


def test_settings_json_wires_the_ladder_and_daily_breaker():
    s = json.load(open(ROOT / "config" / "settings.json", encoding="utf-8"))
    assert s["risk"]["max_daily_loss_pct"] == 3.0
    thr = s["drawdown_throttle"]
    assert thr["halve_at_pct"] == 85.0   # halve at 15% account DD
    assert thr["kill_at_pct"] == 75.0    # halt at 25% DD
    # The configured block must actually drive evaluate() to halve at 15% DD.
    info = dt.evaluate(current_pv=8400, peak_pv=10000, settings_throttle=thr)
    assert info["multiplier"] == 0.5
