"""Stage 2.3 (master plan) — market-wide event quarantine (CPI / FOMC).

On a listed high-risk macro print day: skip intraday entries outright and
de-size EOD entries to 25%. Pure calendar logic + process_signals
integration, mirroring the earnings-veto wiring style.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import event_calendar as ec  # noqa: E402


# ---------------------------------------------------------------------------
# Pure calendar logic
# ---------------------------------------------------------------------------

_SETTINGS = {"event_quarantine": {
    "enabled": True, "size_multiplier": 0.25,
    "dates": {"2026-07-14": "CPI", "2026-07-29": "FOMC"}}}


def test_market_event_for_hits_and_misses():
    assert ec.market_event_for(date(2026, 7, 14), settings=_SETTINGS) == {
        "date": "2026-07-14", "label": "CPI"}
    assert ec.market_event_for(date(2026, 7, 15), settings=_SETTINGS) is None


def test_eod_entry_desized_on_event_day():
    act = ec.event_entry_action(
        date(2026, 7, 14), bar_interval="1d", settings=_SETTINGS)
    assert act["action"] == "desize"
    assert act["multiplier"] == 0.25
    assert act["label"] == "CPI"


def test_intraday_entry_skipped_on_event_day():
    act = ec.event_entry_action(
        date(2026, 7, 29), bar_interval="15m", settings=_SETTINGS)
    assert act["action"] == "skip"
    assert act["label"] == "FOMC"


def test_non_event_day_allows():
    assert ec.event_entry_action(
        date(2026, 7, 15), bar_interval="1d", settings=_SETTINGS
    ) == {"action": "allow"}
    assert ec.event_entry_action(
        date(2026, 7, 15), bar_interval="5m", settings=_SETTINGS
    ) == {"action": "allow"}


def test_disabled_always_allows():
    s = {"event_quarantine": {"enabled": False,
                              "dates": {"2026-07-14": "CPI"}}}
    assert ec.event_entry_action(
        date(2026, 7, 14), bar_interval="1d", settings=s
    ) == {"action": "allow"}


def test_dates_accepts_list_form():
    s = {"event_quarantine": {"dates": ["2026-07-14", "2026-07-29"]}}
    assert ec.market_event_for(date(2026, 7, 14), settings=s)["label"] == "event"


def test_invalid_override_falls_back_to_builtin():
    s = {"event_quarantine": {"dates": 12345}}
    # Bad override → built-in defaults still active.
    assert ec.market_event_for(date(2026, 7, 14), settings=s) is not None


def test_builtin_defaults_present():
    assert "2026-07-14" in ec.DEFAULT_EVENT_DATES
    assert ec.DEFAULT_EVENT_DATES["2026-07-28"] == "FOMC"


def test_upcoming_events_within_horizon():
    evs = ec.upcoming_events(
        date(2026, 7, 1), horizon_days=20, settings=_SETTINGS)
    isos = [e["date"] for e in evs]
    assert "2026-07-14" in isos
    assert "2026-07-29" not in isos  # 28 days away, beyond horizon
    assert evs[0]["days_away"] == 13


# ---------------------------------------------------------------------------
# process_signals integration
# ---------------------------------------------------------------------------

def _seed_eligible(conn, strategy_id, *, interval="1d"):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    pattern = [2.0, 1.0]
    for i in range(36):
        ret = pattern[i % 2]
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="SEED",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval=interval,
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )


_BARS = [{"high": 102, "low": 98, "close": 100}] * 16

_EVENT_CFG = {"enabled": True, "size_multiplier": 0.25,
              "dates": {"2026-07-14": "CPI"}}


def _base_settings(**over):
    s = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 100000, "sizing_method": "atr_risk",
        "risk_per_trade_pct": 0.0075,
        # Pin risk_on so the regime sizing scale is 1.0 (isolate the event scale).
        "risk": {"regime_gate": {"enabled": False}},
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
        "event_quarantine": _EVENT_CFG,
    }
    s.update(over)
    return s


def _account_fn():
    return lambda: {"portfolio_value": 100000, "equity": 100000,
                    "buying_power": 1_000_000, "cash": 1_000_000}


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES",
                        [{"id": "winner", "compute": "x",
                          "strategy_class": "mean_reversion"}], raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "choppy")
    yield test_db


def _eod_qty(conn, asof):
    db.record_signal(conn, strategy_id="winner", symbol="AAA",
                     bar_ts=asof.isoformat(), signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    res = at.process_signals(
        conn, asof=asof, settings=_base_settings(),
        bars_fetcher=lambda sym: _BARS, account_summary_fn=_account_fn(),
    )
    buys = [a for a in res["actions"]
            if a["strategy_id"] == "winner" and a["action"] == "DRY_BUY"]
    assert len(buys) == 1, res["actions"]
    return buys[0]["qty"]


def test_eod_entry_desized_on_cpi_day(isolated_db):
    conn = db.init_db()
    _seed_eligible(conn, "winner")
    qty_normal = _eod_qty(conn, date(2026, 7, 15))  # non-event day

    test_db2 = isolated_db.parent / "trading2.db"
    db.DB_FILE = test_db2
    db.init_db(test_db2)
    conn2 = db.init_db()
    _seed_eligible(conn2, "winner")
    qty_event = _eod_qty(conn2, date(2026, 7, 14))  # CPI day

    assert qty_event < qty_normal
    assert qty_event == pytest.approx(round(qty_normal * 0.25), abs=1)


def test_intraday_entry_skipped_on_cpi_day(isolated_db, monkeypatch):
    from monitoring import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES",
                        [{"id": "winner", "compute": "x",
                          "strategy_class": "mean_reversion"}], raising=False)
    conn = db.init_db()
    _seed_eligible(conn, "winner", interval="15m")
    db.record_signal(conn, strategy_id="winner", symbol="AAA",
                     bar_ts="2026-07-14", signal_type="long_entry",
                     close=100.0, bar_interval="15m")
    settings = _base_settings(intraday_enabled=True,
                              intraday_intervals=["15m"],
                              skip_intraday_signals=False)
    res = at.process_signals(
        conn, asof=date(2026, 7, 14), settings=settings, bar_interval="15m",
        bars_fetcher=lambda sym: _BARS, account_summary_fn=_account_fn(),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert any(a["action"] == "SKIP_MARKET_EVENT" for a in actions), actions
    assert not any(a["action"] in ("DRY_BUY", "BUY") for a in actions)


def test_no_event_day_full_size(isolated_db):
    conn = db.init_db()
    _seed_eligible(conn, "winner")
    qty = _eod_qty(conn, date(2026, 7, 15))
    assert qty > 0  # full size on a non-event day
