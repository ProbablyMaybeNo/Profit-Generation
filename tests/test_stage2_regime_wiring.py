"""Stage 2.2 (master plan) — wire the regime score into eligibility & sizing.

On a risk_off tape, directional/momentum entries (trend/breakout/momentum)
are blocked (SKIP_RISK_REGIME) while mean-reversion stays eligible; the
per-trade risk % is scaled by the regime risk_scale (risk_on 1.0x /
transitional 0.5x / risk_off 0.25x). Pure helper tests + process_signals
integration tests, mirroring the Stage 1 heat-cap wiring style.
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
from monitoring import regime  # noqa: E402


# ---------------------------------------------------------------------------
# Pure: regime_blocks_class / regime_eligibility_skip
# ---------------------------------------------------------------------------

def test_risk_off_blocks_directional_classes():
    for sc in ("trend", "breakout", "momentum"):
        assert regime.regime_blocks_class(sc, regime.RISK_OFF) is True


def test_risk_off_allows_mean_reversion():
    assert regime.regime_blocks_class("mean_reversion", regime.RISK_OFF) is False
    assert regime.regime_blocks_class("mean-reversion", regime.RISK_OFF) is False


def test_risk_on_and_transitional_block_nothing():
    for rg in (regime.RISK_ON, regime.TRANSITIONAL):
        for sc in ("trend", "breakout", "momentum", "mean_reversion"):
            assert regime.regime_blocks_class(sc, rg) is False


def test_unknown_class_never_blocked():
    assert regime.regime_blocks_class(None, regime.RISK_OFF) is False
    assert regime.regime_blocks_class("", regime.RISK_OFF) is False
    assert regime.regime_blocks_class("arbitrage", regime.RISK_OFF) is False


def test_eligibility_skip_descriptor_shape():
    skip = regime.regime_eligibility_skip("trend", regime=regime.RISK_OFF)
    assert skip is not None
    assert skip["regime"] == regime.RISK_OFF
    assert skip["strategy_class"] == "trend"
    assert "blocked" in skip["reason"]
    assert regime.regime_eligibility_skip(
        "mean_reversion", regime=regime.RISK_OFF) is None


# ---------------------------------------------------------------------------
# process_signals integration
# ---------------------------------------------------------------------------

def _seed_eligible(conn, strategy_id):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    pattern = [2.0, 1.0]
    for i in range(36):
        ret = pattern[i % 2]
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="SEED",
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


def _set_regime(conn, label, scale):
    db.upsert_regime_score(
        conn, score_date="2026-05-14", regime=label, risk_scale=scale,
        vix=20.0, vix_200dma=18.0, adx=32.0, confidence=0.9, detail="test",
    )


_BARS = [{"high": 102, "low": 98, "close": 100}] * 16


def _base_settings(**over):
    s = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 100000, "sizing_method": "atr_risk",
        "risk_per_trade_pct": 0.0075,
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
    }
    s.update(over)
    return s


def _account_fn():
    return lambda: {"portfolio_value": 100000, "equity": 100000,
                    "buying_power": 1_000_000, "cash": 1_000_000}


def test_risk_off_blocks_trend_entry(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES",
                        [{"id": "trender", "compute": "x",
                          "strategy_class": "trend"}], raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "trending_up")

    conn = db.init_db()
    _seed_eligible(conn, "trender")
    _set_regime(conn, regime.RISK_OFF, 0.25)
    db.record_signal(conn, strategy_id="trender", symbol="AAA",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")

    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=_base_settings(),
        bars_fetcher=lambda sym: _BARS, account_summary_fn=_account_fn(),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "trender"]
    assert any(a["action"] == "SKIP_RISK_REGIME" for a in actions), actions
    assert not any(a["action"] in ("DRY_BUY", "BUY") for a in actions)


def test_risk_off_allows_mean_reversion_entry(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES",
                        [{"id": "reverter", "compute": "x",
                          "strategy_class": "mean_reversion"}], raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "choppy")

    conn = db.init_db()
    _seed_eligible(conn, "reverter")
    _set_regime(conn, regime.RISK_OFF, 0.25)
    db.record_signal(conn, strategy_id="reverter", symbol="AAA",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")

    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=_base_settings(),
        bars_fetcher=lambda sym: _BARS, account_summary_fn=_account_fn(),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "reverter"]
    assert not any(a["action"] == "SKIP_RISK_REGIME" for a in actions), actions
    assert any(a["action"] == "DRY_BUY" for a in actions), actions


def _mr_buy_qty(conn, regime_label, scale, monkeypatch):
    """Run one MR entry and return the sized qty for the given regime."""
    _set_regime(conn, regime_label, scale)
    db.record_signal(conn, strategy_id="reverter", symbol="AAA",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=_base_settings(),
        bars_fetcher=lambda sym: _BARS, account_summary_fn=_account_fn(),
    )
    buys = [a for a in res["actions"]
            if a["strategy_id"] == "reverter" and a["action"] == "DRY_BUY"]
    assert len(buys) == 1, res["actions"]
    return buys[0]["qty"]


def test_risk_scale_shrinks_size_on_stress_tape(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES",
                        [{"id": "reverter", "compute": "x",
                          "strategy_class": "mean_reversion"}], raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "choppy")

    conn = db.init_db()
    _seed_eligible(conn, "reverter")
    qty_on = _mr_buy_qty(conn, regime.RISK_ON, 1.0, monkeypatch)

    # Fresh DB for the risk_off run so the duplicate-guard doesn't block.
    test_db2 = tmp_path / "trading2.db"
    monkeypatch.setattr(db, "DB_FILE", test_db2)
    db.init_db(test_db2)
    conn2 = db.init_db()
    _seed_eligible(conn2, "reverter")
    qty_off = _mr_buy_qty(conn2, regime.RISK_OFF, 0.25, monkeypatch)

    # risk_off scales risk_pct to 0.25x → quarter the qty of risk_on.
    assert qty_off < qty_on
    assert qty_off == pytest.approx(round(qty_on * 0.25), abs=1)


def test_gate_disabled_no_block_no_scale(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES",
                        [{"id": "trender", "compute": "x",
                          "strategy_class": "trend"}], raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "trending_up")

    conn = db.init_db()
    _seed_eligible(conn, "trender")
    _set_regime(conn, regime.RISK_OFF, 0.25)
    db.record_signal(conn, strategy_id="trender", symbol="AAA",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")

    settings = _base_settings(risk={"regime_gate": {"enabled": False}})
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: _BARS, account_summary_fn=_account_fn(),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "trender"]
    # Gate off → directional entry on a risk_off tape is NOT blocked.
    assert not any(a["action"] == "SKIP_RISK_REGIME" for a in actions), actions
    assert any(a["action"] == "DRY_BUY" for a in actions), actions
