"""
test_intraday_sizing_tier.py — 5.5.1: intraday sizing tier.

Covers:
  - resolve_intraday_multiplier:
      * EOD (1d) → None (no discount)
      * Default 0.5 when nothing overridden
      * Strategy declaration override beats settings default
      * Settings default beats fallback constant
      * Negative / non-numeric overrides fall through
  - compute_notional with intraday_multiplier:
      * fixed sizing: notional * intraday_multiplier
      * combined with regime_multiplier (both applied)
      * min_position_usd floor enforced when adjusted < min
      * sizing dict carries intraday_multiplier + base_notional
  - auto_trader integration:
      * EOD entry produces full max_position_usd (no discount)
      * intraday entry produces half max_position_usd (default 0.5)
      * per-strategy override changes the multiplier
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import sizing as sizing_mod  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


# ---------------- resolve_intraday_multiplier ----------------

def test_resolve_returns_none_for_eod():
    assert sizing_mod.resolve_intraday_multiplier(bar_interval="1d") is None
    assert sizing_mod.resolve_intraday_multiplier(bar_interval=None) is None


def test_resolve_default_is_0_5():
    assert sizing_mod.resolve_intraday_multiplier(bar_interval="15m") == 0.5
    assert sizing_mod.resolve_intraday_multiplier(bar_interval="5m") == 0.5
    assert sizing_mod.resolve_intraday_multiplier(bar_interval="1h") == 0.5


def test_resolve_declaration_override_wins():
    """Per-strategy override beats settings default."""
    m = sizing_mod.resolve_intraday_multiplier(
        bar_interval="15m",
        declaration={"intraday_size_multiplier": 0.75},
        settings_auto_trade={"intraday_size_multiplier": 0.3},
    )
    assert m == 0.75


def test_resolve_settings_default_when_no_declaration():
    m = sizing_mod.resolve_intraday_multiplier(
        bar_interval="15m",
        settings_auto_trade={"intraday_size_multiplier": 0.3},
    )
    assert m == 0.3


def test_resolve_fallback_on_invalid_overrides():
    """Negative / non-numeric overrides fall through to the next source."""
    m = sizing_mod.resolve_intraday_multiplier(
        bar_interval="15m",
        declaration={"intraday_size_multiplier": -1},   # invalid
        settings_auto_trade={"intraday_size_multiplier": "abc"},  # invalid
    )
    assert m == 0.5  # default


def test_resolve_explicit_default_arg():
    m = sizing_mod.resolve_intraday_multiplier(
        bar_interval="15m", default=0.25,
    )
    assert m == 0.25


# ---------------- compute_notional with intraday_multiplier ----------------

def test_compute_notional_intraday_half_size(isolated_db):
    conn = db.init_db()
    out = sizing_mod.compute_notional(
        conn, "any-strat",
        sizing_method="fixed",
        portfolio_value=10_000.0,
        max_position_usd=1000.0,
        intraday_multiplier=0.5,
    )
    assert out["notional"] == 500.0
    assert out["intraday_multiplier"] == 0.5
    assert out["base_notional"] == 1000.0


def test_compute_notional_intraday_combined_with_regime(isolated_db):
    """regime_multiplier 0.8 then intraday 0.5 → 1000 * 0.8 * 0.5 = 400."""
    conn = db.init_db()
    out = sizing_mod.compute_notional(
        conn, "any-strat",
        sizing_method="fixed",
        portfolio_value=10_000.0,
        max_position_usd=1000.0,
        regime_multiplier=0.8,
        strategy_class="trend",
        intraday_multiplier=0.5,
    )
    assert out["notional"] == 400.0
    assert out["regime_multiplier"] == 0.8
    assert out["intraday_multiplier"] == 0.5


def test_compute_notional_intraday_floor_at_min(isolated_db):
    """0.1 multiplier on 1000 = 100; floor=250 → snaps up to 250."""
    conn = db.init_db()
    out = sizing_mod.compute_notional(
        conn, "any-strat",
        sizing_method="fixed",
        portfolio_value=10_000.0,
        max_position_usd=1000.0,
        intraday_multiplier=0.1,
        min_position_usd=250.0,
    )
    assert out["notional"] == 250.0


def test_compute_notional_intraday_none_unchanged(isolated_db):
    """intraday_multiplier=None leaves notional alone (back-compat)."""
    conn = db.init_db()
    out = sizing_mod.compute_notional(
        conn, "any-strat",
        sizing_method="fixed",
        portfolio_value=10_000.0,
        max_position_usd=1000.0,
        intraday_multiplier=None,
    )
    assert out["notional"] == 1000.0
    assert "intraday_multiplier" not in out


# ---------------- auto_trader integration ----------------

def _seed(conn, *, sid: str, sym: str, bar_ts: str, bar_interval: str,
           close: float = 50.0):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    return db.record_signal(
        conn, strategy_id=sid, symbol=sym,
        bar_ts=bar_ts, signal_type="long_entry",
        close=close, bar_interval=bar_interval,
    )


def _settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 0, "min_mean_ret_pct": 0.0,
        "min_sharpe_ish": 0.0, "max_position_usd": 1000.0,
        "sizing_method": "fixed",
        # Neutralize grace-period discount so test math is purely about
        # the intraday multiplier under test.
        "grace_period_size_multiplier": 1.0,
    }


_NO_META_INTRA = [{
    "id": "test-intra-15m",
    "compute": "compute_3bar_low_intraday",
    "bar_interval": "15m",
    "active_on": ["SPY"],
    "grace_period": True,  # so n=0 path returns eligible
}]

_NO_META_EOD = [{
    "id": "test-eod-1d",
    "compute": "compute_3bar_low",
    "active_on": ["SPY"],
    "grace_period": True,
}]


def _patch_tracked(monkeypatch, decls):
    """Replace monitoring.config.TRACKED_STRATEGIES for the test scope."""
    from monitoring import config as mc
    monkeypatch.setattr(mc, "TRACKED_STRATEGIES", decls)


def test_auto_trader_eod_entry_no_intraday_discount(isolated_db, monkeypatch):
    """EOD entry: no intraday_multiplier in the sizing dict, notional = max."""
    _patch_tracked(monkeypatch, _NO_META_EOD)
    conn = db.init_db()
    _seed(conn, sid="test-eod-1d",
          sym="SPY", bar_ts="2026-05-14", bar_interval="1d")
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=_settings(),
    )
    actions = res["actions"]
    submits = [a for a in actions
                if a.get("action") in ("DRY_BUY", "SUBMITTED")]
    assert submits, f"expected DRY_BUY in {actions}"
    sizing = submits[0]["sizing"]
    assert "intraday_multiplier" not in sizing
    assert sizing["notional"] == 1000.0


def test_auto_trader_intraday_entry_half_size(isolated_db, monkeypatch):
    """Intraday entry: default 0.5 multiplier → notional 500."""
    _patch_tracked(monkeypatch, _NO_META_INTRA)
    conn = db.init_db()
    _seed(conn, sid="test-intra-15m",
          sym="SPY", bar_ts="2026-05-14T14:30:00", bar_interval="15m")
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        bar_interval="15m", settings=_settings(),
    )
    actions = res["actions"]
    submits = [a for a in actions
                if a.get("action") in ("DRY_BUY", "SUBMITTED")]
    assert submits, f"expected DRY_BUY in {actions}"
    sizing = submits[0]["sizing"]
    assert sizing["intraday_multiplier"] == 0.5
    assert sizing["notional"] == 500.0
    assert sizing["base_notional"] == 1000.0


def test_auto_trader_intraday_per_strategy_override(isolated_db, monkeypatch):
    """Declaration override of 0.75 changes the multiplier vs default 0.5."""
    overlay = [dict(_NO_META_INTRA[0], intraday_size_multiplier=0.75)]
    _patch_tracked(monkeypatch, overlay)
    conn = db.init_db()
    _seed(conn, sid="test-intra-15m",
          sym="SPY", bar_ts="2026-05-14T14:30:00", bar_interval="15m")
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        bar_interval="15m",
        settings=_settings(),
    )
    actions = res["actions"]
    submits = [a for a in actions
                if a.get("action") in ("DRY_BUY", "SUBMITTED")]
    assert submits, f"expected DRY_BUY in {actions}"
    sizing = submits[0]["sizing"]
    assert sizing["intraday_multiplier"] == 0.75
    assert sizing["notional"] == 750.0


def test_auto_trader_intraday_settings_override(isolated_db, monkeypatch):
    """Settings override of 0.3 (no declaration override) lands at 0.3."""
    _patch_tracked(monkeypatch, _NO_META_INTRA)
    conn = db.init_db()
    _seed(conn, sid="test-intra-15m",
          sym="SPY", bar_ts="2026-05-14T14:30:00", bar_interval="15m")
    s = _settings()
    s["intraday_size_multiplier"] = 0.3
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        bar_interval="15m", settings=s,
    )
    actions = res["actions"]
    submits = [a for a in actions
                if a.get("action") in ("DRY_BUY", "SUBMITTED")]
    assert submits, f"expected DRY_BUY in {actions}"
    sizing = submits[0]["sizing"]
    assert sizing["intraday_multiplier"] == 0.3
    assert sizing["notional"] == 300.0


# ---------------- M4: intraday floor lets liquid names size >= 1 share ----------------

def test_intraday_floor_lets_liquid_name_size_one_share(isolated_db, monkeypatch):
    """A $760 intraday SPY entry: base 1000 × 0.5 = 500 (< one share), but
    the intraday floor of $800 lifts notional so qty floors to 1 share."""
    _patch_tracked(monkeypatch, _NO_META_INTRA)
    conn = db.init_db()
    _seed(conn, sid="test-intra-15m",
          sym="SPY", bar_ts="2026-05-14T14:30:00", bar_interval="15m",
          close=760.0)
    # max_position_usd=1000 (from _settings) × 0.5 intraday = 500 (< one
    # $760 share). The $800 floor lifts it so qty floors to 1.
    s = _settings()
    s["intraday"] = {"min_position_usd": 800}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        bar_interval="15m", settings=s,
    )
    submits = [a for a in res["actions"]
               if a.get("action") in ("DRY_BUY", "SUBMITTED")]
    assert submits, f"expected DRY_BUY in {res['actions']}"
    sizing = submits[0]["sizing"]
    # Floored to 800 (from the 500 the multiplier would have produced).
    assert sizing["notional"] == pytest.approx(800.0)
    assert submits[0]["qty"] >= 1


def test_intraday_floor_not_applied_to_eod(isolated_db, monkeypatch):
    """The intraday floor must not touch EOD sizing."""
    _patch_tracked(monkeypatch, _NO_META_EOD)
    conn = db.init_db()
    _seed(conn, sid="test-eod-1d",
          sym="SPY", bar_ts="2026-05-14", bar_interval="1d", close=50.0)
    s = _settings()
    s["intraday"] = {"min_position_usd": 800}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=s,
    )
    submits = [a for a in res["actions"]
               if a.get("action") in ("DRY_BUY", "SUBMITTED")]
    assert submits, f"expected DRY_BUY in {res['actions']}"
    # EOD notional unchanged at max_position_usd (1000), not floored to 800.
    assert submits[0]["sizing"]["notional"] == 1000.0
