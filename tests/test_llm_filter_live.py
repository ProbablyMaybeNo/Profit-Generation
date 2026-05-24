"""7.1.3 — LLM filter graduation to live consumption.

Validates the consume-path behavior of the auto_trader's llm filter
integration:
  - When `auto_trade.llm_filter_live` is False (default), verdicts are
    ignored entirely (the filter remains observational).
  - When True AND verdict='skip', the signal is blocked and an
    intraday_skips row is written with gate='llm_filter_skip'.
  - When True AND verdict='downsize', qty is halved via the
    throttle_multiplier path.
  - When True AND verdict='allow', no change to entry behavior.
  - Fail-open verdicts always pass through, even when filter_live=true.
  - settings.json default for the flag is False.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader  # noqa: E402


# ---------------------------------------------------------------------------
# Pure helper — _llm_filter_live_action
# ---------------------------------------------------------------------------

def test_action_pass_when_filter_live_off():
    settings = {"auto_trade": {"llm_filter_live": False}}
    verdict = {"verdict": "skip", "confidence": 0.9,
               "rationale": "earnings tomorrow"}
    out = auto_trader._llm_filter_live_action(
        settings=settings, verdict=verdict,
    )
    assert out["action"] == "pass"
    assert out["qty_multiplier"] == 1.0
    assert out["reason"] == "filter_off"


def test_action_skip_when_live_and_skip_verdict():
    settings = {"auto_trade": {"llm_filter_live": True}}
    verdict = {"verdict": "skip", "confidence": 0.9,
               "rationale": "earnings tomorrow"}
    out = auto_trader._llm_filter_live_action(
        settings=settings, verdict=verdict,
    )
    assert out["action"] == "skip"
    assert out["qty_multiplier"] == 0.0
    assert "earnings tomorrow" in out["reason"]


def test_action_downsize_halves_qty():
    settings = {"auto_trade": {"llm_filter_live": True}}
    verdict = {"verdict": "downsize", "confidence": 0.7,
               "rationale": "thin liquidity day"}
    out = auto_trader._llm_filter_live_action(
        settings=settings, verdict=verdict,
    )
    assert out["action"] == "downsize"
    assert out["qty_multiplier"] == 0.5


def test_action_pass_for_allow_verdict():
    settings = {"auto_trade": {"llm_filter_live": True}}
    verdict = {"verdict": "allow", "confidence": 0.9,
               "rationale": "context looks normal"}
    out = auto_trader._llm_filter_live_action(
        settings=settings, verdict=verdict,
    )
    assert out["action"] == "pass"
    assert out["qty_multiplier"] == 1.0


def test_action_pass_for_fail_open_verdict():
    """Fail-open rationale starts with 'fail-open:' — that bypasses live consume."""
    settings = {"auto_trade": {"llm_filter_live": True}}
    verdict = {"verdict": "allow", "confidence": 0.0,
               "rationale": "fail-open: no_api_key"}
    out = auto_trader._llm_filter_live_action(
        settings=settings, verdict=verdict,
    )
    assert out["action"] == "pass"
    assert out["reason"] == "fail_open_passthrough"


def test_action_pass_when_no_verdict():
    settings = {"auto_trade": {"llm_filter_live": True}}
    out = auto_trader._llm_filter_live_action(
        settings=settings, verdict=None,
    )
    assert out["action"] == "pass"


# ---------------------------------------------------------------------------
# Settings default — llm_filter_live flag is False unless explicitly true
# ---------------------------------------------------------------------------

def test_default_settings_does_not_enable_llm_filter_live():
    """If settings.json exists in the repo, llm_filter_live must be False."""
    settings_path = ROOT / "config" / "settings.json"
    if not settings_path.exists():
        pytest.skip("settings.json not present in test env")
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)
    auto = settings.get("auto_trade") or {}
    assert auto.get("llm_filter_live", False) is False, (
        "auto_trade.llm_filter_live should default to False — flipping "
        "it on is a manual decision after 7.1.2's A/B gate clears."
    )


# ---------------------------------------------------------------------------
# Integration smoke — process_signals consumes the verdict
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


def _seed_entry_signal(conn, *, strategy_id, symbol, asof_iso):
    db.ensure_strategies_seeded(conn, [{"id": strategy_id}])
    return db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=asof_iso, signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )


def _seed_enough_history(conn, strategy_id, *, n_wins=28, n_losses=7):
    """Seed enough closed outcomes that the strategy is eligible."""
    db.ensure_strategies_seeded(conn, [{"id": strategy_id}])
    for i in range(n_wins):
        sig_id = db.record_signal(
            conn, strategy_id=strategy_id, symbol="SPY",
            bar_ts=f"2025-{(i // 25) + 1:02d}-{(i % 25) + 1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
            ts=f"2025-01-01T{i % 24:02d}:00:00",
        )
        if sig_id:
            db.open_outcome(conn, signal_id=sig_id,
                            entry_ts=f"2025-01-{i+1:02d}",
                            entry_price=100.0)
            db.close_outcome(conn, signal_id=sig_id,
                             exit_ts=f"2025-01-{i+2:02d}",
                             exit_price=102.0, exit_reason="test")
    for i in range(n_losses):
        sig_id = db.record_signal(
            conn, strategy_id=strategy_id, symbol="QQQ",
            bar_ts=f"2025-{(i // 25) + 6:02d}-{(i % 25) + 1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
            ts=f"2025-06-01T{i % 24:02d}:00:00",
        )
        if sig_id:
            db.open_outcome(conn, signal_id=sig_id,
                            entry_ts=f"2025-06-{i+1:02d}",
                            entry_price=100.0)
            db.close_outcome(conn, signal_id=sig_id,
                             exit_ts=f"2025-06-{i+2:02d}",
                             exit_price=98.0, exit_reason="test")


def test_skip_writes_intraday_skips_row_when_filter_live(conn, monkeypatch):
    """settings.auto_trade.llm_filter_live=True + verdict=skip → SKIP_LLM_FILTER
    action + intraday_skips row with gate='llm_filter_skip'."""
    from datetime import date

    sid = "test-strat-skip"
    _seed_enough_history(conn, sid)
    # Today's entry signal:
    asof = date(2026, 5, 24)
    _seed_entry_signal(conn, strategy_id=sid, symbol="SPY",
                        asof_iso=asof.isoformat())

    # Stub the LLM filter to return a skip verdict.
    def fake_assess(signal, conn_arg, **_kw):
        v = {"verdict": "skip", "confidence": 0.9,
             "rationale": "fed minutes today"}
        # Persist a shadow row like the real assess_signal would.
        from monitoring import llm_filter as llmf
        llmf._persist_verdict(
            conn_arg, signal=signal, verdict=v,
            usage={}, latency_ms=None, failure_mode=None,
            model="test-model",
        )
        return v

    monkeypatch.setattr(
        "monitoring.llm_filter.assess_signal", fake_assess,
    )

    settings = {
        "enabled": True,
        "dry_run": True,
        "llm_filter": {"enabled": True},
        "auto_trade": {"llm_filter_live": True},
    }
    result = auto_trader.process_signals(
        conn, asof=asof, settings=settings,
    )
    actions = result.get("actions", [])
    skip_actions = [a for a in actions if a.get("action") == "SKIP_LLM_FILTER"]
    assert skip_actions, (
        f"expected SKIP_LLM_FILTER, got actions={[a.get('action') for a in actions]}"
    )
    # And the intraday_skips row.
    rows = conn.execute(
        "SELECT gate FROM intraday_skips WHERE gate=?",
        ("llm_filter_skip",),
    ).fetchall()
    assert len(rows) >= 1


def test_skip_ignored_when_filter_live_off(conn, monkeypatch):
    """Default settings (llm_filter_live=false) → verdict=skip has no
    effect, entry proceeds (BUY / DRY_BUY)."""
    from datetime import date

    sid = "test-strat-shadow"
    _seed_enough_history(conn, sid)
    asof = date(2026, 5, 24)
    _seed_entry_signal(conn, strategy_id=sid, symbol="SPY",
                        asof_iso=asof.isoformat())

    def fake_assess(signal, conn_arg, **_kw):
        v = {"verdict": "skip", "confidence": 0.9, "rationale": "x"}
        from monitoring import llm_filter as llmf
        llmf._persist_verdict(
            conn_arg, signal=signal, verdict=v,
            usage={}, latency_ms=None, failure_mode=None,
            model="test-model",
        )
        return v

    monkeypatch.setattr(
        "monitoring.llm_filter.assess_signal", fake_assess,
    )

    settings = {
        "enabled": True,
        "dry_run": True,
        "llm_filter": {"enabled": True},
        # Note: llm_filter_live absent / default False.
    }
    result = auto_trader.process_signals(
        conn, asof=asof, settings=settings,
    )
    actions = result.get("actions", [])
    skip_actions = [a for a in actions if a.get("action") == "SKIP_LLM_FILTER"]
    assert not skip_actions
    # Should have produced a DRY_BUY since dry_run=True.
    buys = [a for a in actions if a.get("action") in ("BUY", "DRY_BUY")]
    assert buys, f"expected a DRY_BUY when filter_live off, got {actions}"


def test_downsize_halves_throttle_multiplier_when_filter_live(conn, monkeypatch):
    """verdict=downsize × filter_live → entry_action carries the downsize flag
    and the underlying _process_entry sees a halved throttle_multiplier."""
    from datetime import date

    sid = "test-strat-downsize"
    _seed_enough_history(conn, sid)
    asof = date(2026, 5, 24)
    _seed_entry_signal(conn, strategy_id=sid, symbol="SPY",
                        asof_iso=asof.isoformat())

    def fake_assess(signal, conn_arg, **_kw):
        v = {"verdict": "downsize", "confidence": 0.7,
             "rationale": "thin liquidity"}
        from monitoring import llm_filter as llmf
        llmf._persist_verdict(
            conn_arg, signal=signal, verdict=v,
            usage={}, latency_ms=None, failure_mode=None,
            model="test-model",
        )
        return v

    monkeypatch.setattr(
        "monitoring.llm_filter.assess_signal", fake_assess,
    )

    settings = {
        "enabled": True,
        "dry_run": True,
        "llm_filter": {"enabled": True},
        "auto_trade": {"llm_filter_live": True},
    }
    result = auto_trader.process_signals(
        conn, asof=asof, settings=settings,
    )
    actions = result.get("actions", [])
    dry_buys = [a for a in actions if a.get("action") == "DRY_BUY"]
    assert dry_buys, f"expected DRY_BUY, got {actions}"
    assert dry_buys[0].get("llm_filter_downsize") is True
    assert "thin liquidity" in dry_buys[0].get("llm_filter_reason", "")
