"""Tests for the 4.7.* wiring of trailing stops, pyramiding, and the
trend/mean-reversion regime allocator into `auto_trader`.

Strategy is end-to-end and synthetic:

  - Seeded TRACKED_STRATEGIES is monkeypatched to include a trend strategy
    with `pyramidable: True`, `trailing_stop.method: atr_trail`, and the
    edge-eligibility thresholds satisfied via fake closed outcomes.
  - bars_fetcher is injected with handcrafted OHLC sequences.
  - Order submission is stubbed.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import trailing_stops as ts_mod  # noqa: E402
from monitoring import pyramiding as py_mod  # noqa: E402


TREND_SID = "trend-test"
MR_SID = "mr-test"


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    conn = db.init_db(test_db)
    db.upsert_strategy(conn, {"extra": {"strategy_id": TREND_SID}})
    db.upsert_strategy(conn, {"extra": {"strategy_id": MR_SID}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


@pytest.fixture()
def winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 5, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000, "skip_intraday_signals": True,
        "cool_down_losers": 0, "earnings_veto_days": 0,
        "trailing_stop": {"method": "atr_trail", "multiplier": 3.0},
        "pyramiding": {"max_tiers": 4,
                        "tier_schedule": [1.0, 0.5, 0.25, 0.125]},
    }


@pytest.fixture()
def trend_declarations():
    return [
        {"id": TREND_SID, "compute": "compute_donchian_breakout_20",
         "strategy_class": "trend", "pyramidable": True,
         "trailing_stop": {"method": "atr_trail", "multiplier": 3.0}},
        {"id": MR_SID, "compute": "compute_rsi2",
         "strategy_class": "mean_reversion"},
    ]


def _seed_outcomes(strat: str, returns):
    conn = db.init_db()
    for i, ret in enumerate(returns):
        sid = db.record_signal(conn, strategy_id=strat, symbol="X",
                                bar_ts=f"2024-01-{i+1:02d}",
                                signal_type="long_entry", close=100.0,
                                bar_interval="1d")
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        exit_price = 100.0 * (1 + ret / 100)
        db.close_outcome(conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
                          exit_price=exit_price,
                          exit_reason="long_exit_signal", bars_held=1)
    return conn


def _ohlc(closes, *, range_=1.0):
    return [
        {"high": c + range_ / 2, "low": c - range_ / 2,
         "close": c, "volume": 1_000_000}
        for c in closes
    ]


@pytest.fixture()
def stub_submit(monkeypatch):
    submitted = []
    def fake_market(client, *, symbol, qty, side, client_order_id=None):
        submitted.append({"type": "market", "symbol": symbol,
                          "qty": qty, "side": side,
                          "client_order_id": client_order_id})
        order = MagicMock()
        order.id = f"alpaca-mkt-{len(submitted)}"
        order.status = "accepted"
        order.submitted_at = "2026-05-14T20:30:00Z"
        order.filled_avg_price = 100.0
        return order
    monkeypatch.setattr(at, "_submit_market_order", fake_market)
    return submitted


def _account_fn():
    return lambda: {
        "portfolio_value": 100_000.0, "cash": 100_000.0,
        "equity": 100_000.0, "buying_power": 100_000.0,
        "equity_at_open": 100_000.0, "last_equity": 100_000.0,
    }


# ---------------------------------------------------------------------------
# 4.7.1 — trailing stops
# ---------------------------------------------------------------------------

def test_resolve_trailing_config_per_strategy_overrides_global(
    winner_settings, trend_declarations,
):
    cfg = at._resolve_trailing_config(
        TREND_SID, winner_settings, trend_declarations,
    )
    assert cfg["method"] == "atr_trail"
    assert cfg["multiplier"] == 3.0


def test_resolve_trailing_config_returns_none_when_no_method(winner_settings):
    s = dict(winner_settings)
    s.pop("trailing_stop", None)
    assert at._resolve_trailing_config(TREND_SID, s, []) is None


def test_trailing_stop_advances_after_entry(
    isolated_db, winner_settings, trend_declarations, stub_submit,
):
    """End-to-end: BUY fires → trailing stop is advanced on each subsequent
    process_signals pass as new bars arrive."""
    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                      bar_ts="2026-05-14", signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    closes = list(range(80, 101))  # 21 bars climbing from 80 → 100
    bars = {"NVDA": _ohlc(closes)}
    bars_fn = lambda s: bars[s]
    settings = {**winner_settings, "dry_run": False}
    import monitoring.regime_router as rr
    import monitoring.auto_trader as at_mod
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(), bars_fetcher=bars_fn,
        account_summary_fn=lambda: {"portfolio_value": 100_000.0,
                                     "cash": 100_000.0, "equity": 100_000.0,
                                     "buying_power": 100_000.0,
                                     "equity_at_open": 100_000.0,
                                     "last_equity": 100_000.0},
    )
    # BUY happened.
    buys = [a for a in res["actions"] if a.get("action") == "BUY"]
    assert len(buys) == 1
    # Now advance bars (climb to 110) and re-process — trailing should ratchet.
    bars["NVDA"] = _ohlc(list(range(80, 111)))
    res2 = at.process_signals(
        conn, asof=date(2026, 5, 15), settings=settings,
        client=MagicMock(), bars_fetcher=bars_fn,
        account_summary_fn=lambda: {"portfolio_value": 100_000.0,
                                     "cash": 100_000.0, "equity": 100_000.0,
                                     "buying_power": 100_000.0,
                                     "equity_at_open": 100_000.0,
                                     "last_equity": 100_000.0},
    )
    stop = ts_mod.get_stop(conn, strategy_id=TREND_SID, symbol="NVDA")
    assert stop is not None
    assert stop["method"] == "atr_trail"
    # Stop has ratcheted upward; extreme is 110.5 (= max(high)).
    assert stop["extreme_price"] == pytest.approx(110.5)


def test_trailing_stop_triggers_exit_when_price_crosses(
    isolated_db, winner_settings, trend_declarations, stub_submit,
):
    """Entry → favorable bars → price drops below trailing stop → exit."""
    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                      bar_ts="2026-05-14", signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    bars = {"NVDA": _ohlc(list(range(80, 101)))}
    settings = {**winner_settings, "dry_run": False}
    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars[s],
    )
    # Push to 130 to lift the stop, then crash to 70 (well under stop).
    bars["NVDA"] = _ohlc(list(range(80, 131)))
    at.process_signals(
        conn, asof=date(2026, 5, 15), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars[s],
    )
    stop_before = ts_mod.get_stop(conn, strategy_id=TREND_SID, symbol="NVDA")
    assert stop_before is not None
    crash_close = stop_before["stop_price"] - 5.0  # punch through
    bars["NVDA"] = _ohlc(list(range(80, 131)) + [crash_close])
    res = at.process_signals(
        conn, asof=date(2026, 5, 16), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars[s],
    )
    sells = [a for a in res["actions"]
             if a.get("action") == "SELL" and a.get("strategy_id") == TREND_SID]
    assert len(sells) == 1
    assert sells[0]["exit_reason"] == "trailing_stop"
    assert sells[0].get("synthetic_trailing_exit") is True


def test_trailing_stop_exit_closes_outcome_with_reason_and_excursion(
    isolated_db, winner_settings, trend_declarations, stub_submit,
):
    """F5 (audit 2026-06-03): a trailing-stop exit must close the outcome
    itself with exit_reason='trailing_stop' + non-NULL MFE/MAE, so the later
    generic 1d signal-exit reconcile can't overwrite it as 'long_exit_signal'
    with NULL excursion."""
    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    entry_sig = db.record_signal(
        conn, strategy_id=TREND_SID, symbol="NVDA",
        bar_ts="2026-05-14", signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )
    bars = {"NVDA": _ohlc(list(range(80, 101)))}
    settings = {**winner_settings, "dry_run": False}
    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars[s],
    )
    # In production the EOD reconcile (reconcile_signals/open_for_entry) opens
    # the outcome for this entry between the buy and the next-day exit. Mirror
    # that here so the trailing exit has an open outcome to close.
    db.open_outcome(conn, signal_id=entry_sig,
                    entry_ts="2026-05-14", entry_price=100.0)
    assert conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (entry_sig,),
    ).fetchone()["status"] == "open"

    # Lift the stop with favorable bars, then crash through it.
    bars["NVDA"] = _ohlc(list(range(80, 131)))
    at.process_signals(
        conn, asof=date(2026, 5, 15), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars[s],
    )
    stop_before = ts_mod.get_stop(conn, strategy_id=TREND_SID, symbol="NVDA")
    crash_close = stop_before["stop_price"] - 5.0
    bars["NVDA"] = _ohlc(list(range(80, 131)) + [crash_close])
    res = at.process_signals(
        conn, asof=date(2026, 5, 16), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars[s],
    )
    sells = [a for a in res["actions"]
             if a.get("action") == "SELL" and a.get("strategy_id") == TREND_SID]
    assert len(sells) == 1
    assert sells[0]["exit_reason"] == "trailing_stop"

    o = conn.execute(
        "SELECT status, exit_reason, mfe_pct, mae_pct FROM outcomes "
        " WHERE signal_id=?", (entry_sig,),
    ).fetchone()
    assert o["status"] == "closed"
    assert o["exit_reason"] == "trailing_stop", \
        "F5 regression: trailing exit reason overwritten / outcome not closed"
    assert o["mfe_pct"] is not None, "F5 regression: trailing close has NULL mfe"
    assert o["mae_pct"] is not None, "F5 regression: trailing close has NULL mae"
    # Favorable run to ~130.5 high, crash low well below entry 100.
    assert o["mfe_pct"] > 0
    assert o["mae_pct"] < 0


def test_trailing_stop_does_not_loosen_below_entry_floor(
    isolated_db, winner_settings, trend_declarations,
):
    """Entry-time ATR stop is the floor; trailing never falls below it."""
    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    # Manually insert a paper_trades BUY + STOP pair to simulate the post-
    # entry world (the auto-trader would normally do this on its own).
    db.record_paper_trade(conn, {
        "alpaca_order_id": "test-buy", "signal_id": None,
        "strategy_id": TREND_SID, "symbol": "NVDA",
        "side": "buy", "qty": 10, "order_type": "market",
        "fill_price": 100.0, "limit_price": None, "stop_price": None,
        "submitted_at": "2026-05-14T20:30:00Z", "status": "filled",
        "notes": "test-entry",
    })
    db.record_paper_trade(conn, {
        "alpaca_order_id": "test-stop", "signal_id": None,
        "strategy_id": TREND_SID, "symbol": "NVDA",
        "side": "sell", "qty": 10, "order_type": "stop",
        "stop_price": 95.0, "limit_price": None, "fill_price": None,
        "submitted_at": "2026-05-14T20:30:01Z", "status": "accepted",
        "notes": "test-entry-stop",
    })
    bars = _ohlc(list(range(85, 106)))  # HH=105.5, ATR=1, atr-trail=102.5
    state = at._advance_trailing_stop_for_position(
        conn, strategy_id=TREND_SID, symbol="NVDA",
        entry_price=100.0,
        trailing_cfg={"method": "atr_trail", "multiplier": 3.0},
        bars_fetcher=lambda s: bars,
    )
    assert state is not None
    # Computed stop = ~102.5 (above floor=95) → not floored.
    assert state["stop_price"] >= 95.0
    # Now contrive an entry floor that's higher than the trailing.
    conn.execute(
        "UPDATE paper_trades SET stop_price=? WHERE alpaca_order_id='test-stop'",
        (104.0,),
    )
    conn.commit()
    state2 = at._advance_trailing_stop_for_position(
        conn, strategy_id=TREND_SID, symbol="NVDA",
        entry_price=100.0,
        trailing_cfg={"method": "atr_trail", "multiplier": 3.0},
        bars_fetcher=lambda s: bars,
    )
    assert state2 is not None
    assert state2["stop_price"] >= 104.0  # floored up to entry stop


def test_no_trail_strategies_unaffected(
    isolated_db, winner_settings, trend_declarations, stub_submit,
):
    """Strategies without a trailing config should never get a trailing_stops
    row written."""
    settings = dict(winner_settings)
    settings.pop("trailing_stop", None)  # disable globally
    # And remove per-strategy
    decls = [{"id": TREND_SID, "compute": "x",
              "strategy_class": "trend", "pyramidable": True}]
    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                      bar_ts="2026-05-14", signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    bars = _ohlc(list(range(80, 101)))
    settings = {**settings, "dry_run": False}
    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars,
    )
    assert ts_mod.get_stop(conn, strategy_id=TREND_SID, symbol="NVDA") is None


# ---------------------------------------------------------------------------
# 4.7.2 — pyramiding
# ---------------------------------------------------------------------------

def test_pyramid_addon_fires_on_second_entry(
    isolated_db, winner_settings, trend_declarations, stub_submit, monkeypatch,
):
    """First entry → tier 0; second entry from same strategy → tier 1."""
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES", trend_declarations,
                         raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "trending_up")

    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                      bar_ts="2026-05-14", signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    # Pyramidable strategy: initial entry sized at cap / sum(tier_schedule),
    # leaving room for full ladder. $10k cap → initial ~$5,333, regime 0.7
    # → $3,733, qty = 37 @ $100. Tier-1 add-on = 19 shares × $105 ≈ $1,995.
    # Aggregate ~$5,728 ≤ $10k cap. Healthy headroom.
    settings = {**winner_settings, "dry_run": False,
                "max_position_usd": 10_000}
    bars = _ohlc(list(range(80, 105)))
    res1 = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars,
        account_summary_fn=_account_fn(),
    )
    buys = [a for a in res1["actions"] if a.get("action") == "BUY"]
    assert len(buys) == 1, f"res1 actions: {res1['actions']}"
    initial_qty = buys[0]["qty"]
    # 2nd signal — different bar_ts so it isn't deduped.
    db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                      bar_ts="2026-05-15", signal_type="long_entry",
                      close=105.0, bar_interval="1d")
    res2 = at.process_signals(
        conn, asof=date(2026, 5, 15), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars,
        account_summary_fn=_account_fn(),
    )
    addons = [a for a in res2["actions"]
              if a.get("action") == "PYRAMID_ADDON"]
    assert len(addons) == 1, f"res2 actions: {res2['actions']}"
    assert addons[0]["tier"] == 1
    assert addons[0]["qty"] == round(initial_qty * 0.5)


def test_pyramid_refused_for_non_pyramidable(
    isolated_db, winner_settings, trend_declarations, stub_submit, monkeypatch,
):
    """A mean-reversion strategy (no pyramidable) refuses add-ons."""
    decls = [{"id": MR_SID, "compute": "x",
              "strategy_class": "mean_reversion"}]
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES", decls, raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "trending_up")
    conn = _seed_outcomes(MR_SID, [2.0, 1.0] * 10)
    db.record_signal(conn, strategy_id=MR_SID, symbol="NVDA",
                      bar_ts="2026-05-14", signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": False}
    bars = _ohlc(list(range(80, 105)))
    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars,
        account_summary_fn=_account_fn(),
    )
    db.record_signal(conn, strategy_id=MR_SID, symbol="NVDA",
                      bar_ts="2026-05-15", signal_type="long_entry",
                      close=105.0, bar_interval="1d")
    res2 = at.process_signals(
        conn, asof=date(2026, 5, 15), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars,
        account_summary_fn=_account_fn(),
    )
    skips = [a for a in res2["actions"]
              if a.get("action") == "SKIP_NO_PYRAMID"]
    assert len(skips) == 1


def test_pyramid_blocked_at_max_tier(
    isolated_db, winner_settings, trend_declarations, stub_submit, monkeypatch,
):
    """Tier 4 is the cap (tiers 0,1,2,3 exist) — 5th entry refuses."""
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES", trend_declarations,
                         raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "trending_up")
    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    # All 4 tiers + a 5th-veto signal. With pyramidable=True, the initial
    # entry is sized at cap/sum_of_tier_schedule so the full ladder fits.
    settings = {**winner_settings, "dry_run": False,
                "max_position_usd": 50_000}
    bars = _ohlc(list(range(80, 105)))
    days = ["2026-05-14", "2026-05-15", "2026-05-16",
            "2026-05-17", "2026-05-18"]
    actions = []
    for i, d in enumerate(days):
        db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                          bar_ts=d, signal_type="long_entry",
                          close=100.0 + i, bar_interval="1d")
        res = at.process_signals(
            conn, asof=date.fromisoformat(d), settings=settings,
            client=MagicMock(), bars_fetcher=lambda s: bars,
            account_summary_fn=_account_fn(),
        )
        actions.extend(res["actions"])
    pyramid_outcomes = [a.get("action") for a in actions
                        if a.get("action") in
                        ("BUY", "PYRAMID_ADDON", "SKIP_MAX_TIERS")]
    assert pyramid_outcomes.count("BUY") == 1
    assert pyramid_outcomes.count("PYRAMID_ADDON") == 3
    assert pyramid_outcomes.count("SKIP_MAX_TIERS") == 1


def test_pyramid_blocked_when_aggregate_exceeds_cap(
    isolated_db, winner_settings, trend_declarations, stub_submit, monkeypatch,
):
    """The sum of all tier notionals must not breach max_position_usd."""
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES", trend_declarations,
                         raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "trending_up")
    # The aggregate-cap guard ensures pyramiding can't breach max_position_usd
    # even when prices move sharply between add-ons. We force the breach by
    # making the second add-on at a much higher price than the initial buy.
    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    settings = {**winner_settings, "dry_run": False,
                "max_position_usd": 10_000}
    bars = _ohlc(list(range(80, 105)))
    db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                      bar_ts="2026-05-14", signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars,
        account_summary_fn=_account_fn(),
    )
    # 10× price move between entry and add-on bar → tier-1 sized in shares
    # against the old initial qty, but priced at $1000 / share → add-on
    # notional spikes and the aggregate trips the cap.
    db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                      bar_ts="2026-05-15", signal_type="long_entry",
                      close=10_000.0, bar_interval="1d")
    res2 = at.process_signals(
        conn, asof=date(2026, 5, 15), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars,
        account_summary_fn=_account_fn(),
    )
    blocks = [a for a in res2["actions"]
              if a.get("action") == "SKIP_PYRAMID_OVER_CAP"]
    assert len(blocks) == 1


# ---------------------------------------------------------------------------
# 4.7.3 — regime allocator wired into sizing
# ---------------------------------------------------------------------------

def test_sizing_applies_regime_multiplier_for_trend(
    isolated_db, winner_settings, trend_declarations, stub_submit, monkeypatch,
):
    """Trend strategy in `trending_up` regime → 0.70 × base notional."""
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES", trend_declarations,
                         raising=False)
    monkeypatch.setattr(rr, "latest_regime", lambda c: "trending_up")
    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    settings = {**winner_settings, "dry_run": True}
    bars = _ohlc(list(range(80, 105)))
    db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                      bar_ts="2026-05-14", signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars,
    )
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    assert len(buys) == 1
    sizing = buys[0]["sizing"]
    # max_position_usd is the AGGREGATE pyramid-ladder cap, so the
    # initial-tier base is max_position_usd / sum(tier_schedule) = 1000 /
    # 1.875 ≈ 533.33. Apply the trend regime multiplier 0.70 → 373.33.
    assert sizing.get("regime_multiplier") == pytest.approx(0.70)
    assert sizing.get("notional_after_throttle") == pytest.approx(
        round(1000.0 / 1.875 * 0.70, 2)
    )


def test_sizing_falls_back_to_50_50_on_low_confidence(
    isolated_db, winner_settings, trend_declarations, monkeypatch,
):
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr
    monkeypatch.setattr(cfg_mod, "TRACKED_STRATEGIES", trend_declarations,
                         raising=False)
    # latest_regime is 'mixed' → allocator returns (0.5, 0.5).
    monkeypatch.setattr(rr, "latest_regime", lambda c: "mixed")
    conn = _seed_outcomes(TREND_SID, [2.0, 1.0] * 10)
    settings = {**winner_settings, "dry_run": True}
    bars = _ohlc(list(range(80, 105)))
    db.record_signal(conn, strategy_id=TREND_SID, symbol="NVDA",
                      bar_ts="2026-05-14", signal_type="long_entry",
                      close=100.0, bar_interval="1d")
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(), bars_fetcher=lambda s: bars,
    )
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    sizing = buys[0]["sizing"]
    assert sizing.get("regime_multiplier") == pytest.approx(0.50)
