"""Stage 1.3 (master plan, 2026-06-17) — hybrid stop: swing-low initial + Chandelier trail.

The trail is adopted as Chandelier(22, 3.0) via config (the evidence-backed
default; the engine + floor-against-initial already exist, so the initial stop
effectively holds until ~+1R then the trail engages). The structure-based
swing-low initial stop is built + tested here but kept OPT-IN
(stops.initial_method, default 'atr_initial') because it changes live stop
distance — which feeds atr_risk sizing and stop-out frequency — so it should be
flipped only after observing the trade-frequency impact.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import sizing  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


# ---------------------------------------------------------------------------
# swing_low_initial_stop — the structure-based stop math
# ---------------------------------------------------------------------------

def test_swing_low_within_cap_uses_swing_low():
    # entry 100, swing 99, ATR 2: raw = 99 − 1×2 = 97 (1.5 ATR away, within 2×).
    stop = sizing.swing_low_initial_stop(
        entry_price=100.0, swing_low=99.0, atr=2.0, buffer_mult=1.0, max_atr_dist=2.0)
    assert stop == pytest.approx(97.0)


def test_swing_low_too_far_is_capped_at_max_atr_dist():
    # entry 100, swing 90, ATR 2: raw = 88 (6 away = 3 ATR) → capped to entry − 2×ATR = 96.
    stop = sizing.swing_low_initial_stop(
        entry_price=100.0, swing_low=90.0, atr=2.0, buffer_mult=1.0, max_atr_dist=2.0)
    assert stop == pytest.approx(96.0)


def test_swing_low_above_entry_returns_none():
    # A swing low so high the buffered stop sits at/above entry → no valid stop.
    stop = sizing.swing_low_initial_stop(
        entry_price=100.0, swing_low=103.0, atr=2.0)
    assert stop is None


def test_swing_low_short_side_unsupported():
    assert sizing.swing_low_initial_stop(
        entry_price=100.0, swing_low=99.0, atr=2.0, side="short") is None


def test_swing_low_missing_atr_returns_none():
    assert sizing.swing_low_initial_stop(
        entry_price=100.0, swing_low=99.0, atr=None) is None


# ---------------------------------------------------------------------------
# resolve_initial_stop routing
# ---------------------------------------------------------------------------

def test_resolve_uses_swing_low_when_opted_in():
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0, strategy_id="w", side="long",
        swing_low=99.0,
        settings_stops={"atr_multiplier": 2.5, "initial_method": "swing_low"},
    )
    assert out["method"] == "swing_low_atr"
    assert out["stop_price"] == pytest.approx(97.0)


def test_resolve_defaults_to_atr_initial():
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0, strategy_id="w", side="long",
        swing_low=99.0,  # provided but not opted in
        settings_stops={"atr_multiplier": 2.5},
    )
    assert out["method"] == "atr_initial"
    assert out["stop_price"] == pytest.approx(95.0)  # 100 − 2.5×2


def test_resolve_swing_low_falls_back_when_no_swing_low():
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0, strategy_id="w", side="long",
        swing_low=None,
        settings_stops={"atr_multiplier": 2.5, "initial_method": "swing_low"},
    )
    assert out["method"] == "atr_initial"
    assert out["stop_price"] == pytest.approx(95.0)


# ---------------------------------------------------------------------------
# _recent_swing_low helper + adopted Chandelier config
# ---------------------------------------------------------------------------

def test_recent_swing_low_from_bar_dicts():
    bars = [{"low": 99}, {"low": 97}, {"low": 98}, {"low": 100}]
    assert at._recent_swing_low(bars, 10) == pytest.approx(97.0)


def test_chandelier_trail_is_adopted_in_config():
    s = json.load(open(ROOT / "config" / "settings.json", encoding="utf-8"))
    ts = s["trailing_stop"]
    assert ts["method"] == "chandelier"
    assert ts["multiplier"] == 3.0
    assert ts["chandelier_lookback"] == 22
