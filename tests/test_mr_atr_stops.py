"""6.1.2 — Mean-reversion strategies flip to ATR initial stops with k=2.0.

Validates:
  - All legacy botnet101 mean-reversion strategies in TRACKED_STRATEGIES
    now declare `strategy_class: mean_reversion`.
  - The `by_class` settings block resolves k=2.0 for mean_reversion.
  - Per-strategy override still wins over by_class.
  - by_class loses to legacy multiplier (Phase 4.6 trend stays put).
  - Live-strategies segregation: paper-active strategies flip first;
    nothing about by_class forces a live strategy through ATR stops if
    its own per-strategy override pins a different value.
"""
import json
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
from monitoring.config import TRACKED_STRATEGIES  # noqa: E402


# ---------------------------------------------------------------------------
# Declaration audit — every legacy MR strategy must declare strategy_class
# ---------------------------------------------------------------------------

LEGACY_MR_STRATEGY_IDS = {
    "botnet101-3-bar-low",
    "botnet101-buy-5day-low",
    "botnet101-consec-bearish",
    "botnet101-4bar-momentum-reversal",
    "botnet101-consec-below-ema",
    "botnet101-turn-around-tuesday",
    "botnet101-turn-of-month",
}


def test_every_legacy_mr_strategy_declares_mean_reversion_class():
    """All botnet101-* legacy strategies are mean_reversion. The class
    declaration is what the auto-trader uses to apply the by_class k=2.0
    stop multiplier from 6.1.2."""
    for meta in TRACKED_STRATEGIES:
        if meta["id"] in LEGACY_MR_STRATEGY_IDS:
            assert meta.get("strategy_class") == "mean_reversion", (
                f"{meta['id']} is missing strategy_class=mean_reversion"
            )


def test_intraday_mr_strategy_already_declared():
    found = [
        m for m in TRACKED_STRATEGIES
        if m.get("id") == "intraday-mr-3bar-low-15m"
    ]
    assert len(found) == 1
    assert found[0]["strategy_class"] == "mean_reversion"


def test_trend_strategies_not_misclassified_as_mr():
    """Sanity check: 6.1.2 must NOT accidentally relabel any trend
    declaration as mean_reversion."""
    for meta in TRACKED_STRATEGIES:
        if meta.get("strategy_class") == "trend":
            assert "botnet101" not in meta["id"], (
                f"trend declaration leaked into botnet101 id {meta['id']}"
            )
        if meta.get("strategy_class") == "breakout":
            assert "botnet101" not in meta["id"]


# ---------------------------------------------------------------------------
# settings.json now ships the stops block
# ---------------------------------------------------------------------------

SETTINGS_PATH = ROOT / "config" / "settings.json"


def test_settings_json_has_stops_block_with_mr_class_override():
    with open(SETTINGS_PATH) as f:
        settings = json.load(f)
    stops = settings.get("stops")
    assert isinstance(stops, dict), "config/settings.json must declare top-level stops"
    assert stops.get("atr_multiplier") == 2.5
    assert stops.get("atr_period") == 14
    assert stops.get("fixed_percent_fallback") == 0.05
    by_class = stops.get("by_class") or {}
    mr = by_class.get("mean_reversion") or {}
    assert mr.get("atr_multiplier") == 2.0, (
        "6.1.2 expects mean_reversion class to use k=2.0 (tighter than trend's 2.5)"
    )


# ---------------------------------------------------------------------------
# resolve_atr_multiplier precedence with by_class
# ---------------------------------------------------------------------------

def test_by_class_overrides_global_default():
    out = sizing.resolve_atr_multiplier(
        strategy_id="botnet101-3-bar-low",
        strategy_class="mean_reversion",
        settings_stops={
            "atr_multiplier": 2.5,
            "by_class": {"mean_reversion": {"atr_multiplier": 2.0}},
        },
    )
    assert out == 2.0


def test_per_strategy_still_beats_by_class():
    out = sizing.resolve_atr_multiplier(
        strategy_id="botnet101-3-bar-low",
        strategy_class="mean_reversion",
        settings_stops={
            "atr_multiplier": 2.5,
            "by_class": {"mean_reversion": {"atr_multiplier": 2.0}},
            "per_strategy": {
                "botnet101-3-bar-low": {"atr_multiplier": 1.5},
            },
        },
    )
    assert out == 1.5


def test_legacy_multiple_still_beats_by_class():
    """Phase 4.6 trend strategies use the legacy stop_loss_atr_multiple
    setting. 6.1.2 must not silently retune them through by_class."""
    out = sizing.resolve_atr_multiplier(
        strategy_id="trend-something",
        strategy_class="trend",
        legacy_multiple=2.5,
        settings_stops={
            "by_class": {"trend": {"atr_multiplier": 1.0}},
        },
    )
    assert out == 2.5


def test_by_class_falls_back_to_global_when_class_unknown():
    out = sizing.resolve_atr_multiplier(
        strategy_id="x",
        strategy_class="exotic",  # unknown class
        settings_stops={
            "atr_multiplier": 2.5,
            "by_class": {"mean_reversion": {"atr_multiplier": 2.0}},
        },
    )
    assert out == 2.5


def test_by_class_default_when_no_class_provided():
    out = sizing.resolve_atr_multiplier(
        strategy_id="x",
        strategy_class=None,
        settings_stops={
            "atr_multiplier": 2.5,
            "by_class": {"mean_reversion": {"atr_multiplier": 2.0}},
        },
    )
    assert out == 2.5


def test_resolve_initial_stop_uses_by_class():
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="botnet101-3-bar-low",
        strategy_class="mean_reversion",
        settings_stops={
            "atr_multiplier": 2.5,
            "by_class": {"mean_reversion": {"atr_multiplier": 2.0}},
        },
        side="long",
    )
    assert out["method"] == "atr_initial"
    assert out["multiplier"] == 2.0
    # entry 100 - 2.0 × 2 = 96.0
    assert out["stop_price"] == pytest.approx(96.0)


# ---------------------------------------------------------------------------
# End-to-end via auto_trader._maybe_attach_stop
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "botnet101-3-bar-low"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _bars(*hlc):
    return [{"high": h, "low": l, "close": c} for (h, l, c) in hlc]


def test_mr_strategy_picks_up_class_default_in_auto_trader(isolated_db):
    """An MR strategy with no per_strategy override gets k=2.0 via
    by_class.mean_reversion, not the global 2.5 default."""
    conn = db.init_db()
    sid = db.record_signal(
        conn, strategy_id="botnet101-3-bar-low", symbol="GDX",
        bar_ts="2026-05-19", signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )
    sig = dict(conn.execute(
        "SELECT * FROM signals WHERE id=?", (sid,),
    ).fetchone())
    settings = {
        "stops": {
            "atr_multiplier": 2.5,
            "atr_period": 14,
            "by_class": {"mean_reversion": {"atr_multiplier": 2.0}},
        },
    }
    bars = _bars(*[(101, 99, 100)] * 16)
    info = at._maybe_attach_stop(
        conn, client=None, settings=settings, sig=sig,
        entry_fill=100.0, qty=10, client_order_id="cid",
        bars_fetcher=lambda sym: bars, dry_run=True,
        strategy_class="mean_reversion",
    )
    assert info["requested_multiple"] == 2.0
    # entry 100 - 2.0 × 2 = 96
    assert info["stop_price"] == pytest.approx(96.0)
    assert info["stop_method"] == "atr_initial"
