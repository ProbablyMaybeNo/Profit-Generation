"""6.1.1 — ATR-based initial stops, generalized across all strategies.

Covers:
  - atr_initial_stop math (longs + shorts)
  - resolve_atr_multiplier precedence (legacy > per-strategy > global > default)
  - resolve_initial_stop fallback to fixed_percent when ATR is missing
  - auto_trader integration: per-strategy override, fallback path,
    short-direction handling, entry_stops column written on paper_trades.
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
from monitoring import sizing  # noqa: E402
from monitoring import stops as stops_mod  # noqa: E402


# ---------------------------------------------------------------------------
# atr_initial_stop — math
# ---------------------------------------------------------------------------

def test_atr_initial_stop_long_basic():
    # entry=100, ATR=2, multiplier=2.5 → stop = 100 - 5 = 95
    assert sizing.atr_initial_stop(
        entry_price=100.0, atr=2.0, multiplier=2.5, side="long",
    ) == pytest.approx(95.0)


def test_atr_initial_stop_short_mirror():
    # entry=100, ATR=2, multiplier=2.5 → stop = 100 + 5 = 105
    assert sizing.atr_initial_stop(
        entry_price=100.0, atr=2.0, multiplier=2.5, side="short",
    ) == pytest.approx(105.0)


def test_atr_initial_stop_tighter_multiplier():
    assert sizing.atr_initial_stop(
        entry_price=100.0, atr=2.0, multiplier=1.0, side="long",
    ) == pytest.approx(98.0)


def test_atr_initial_stop_missing_inputs():
    assert sizing.atr_initial_stop(
        entry_price=100.0, atr=None, multiplier=2.5, side="long",
    ) is None
    assert sizing.atr_initial_stop(
        entry_price=100.0, atr=0, multiplier=2.5, side="long",
    ) is None
    assert sizing.atr_initial_stop(
        entry_price=100.0, atr=2.0, multiplier=0, side="long",
    ) is None
    assert sizing.atr_initial_stop(
        entry_price=None, atr=2.0, multiplier=2.5, side="long",
    ) is None


def test_atr_initial_stop_invalid_side_returns_none():
    assert sizing.atr_initial_stop(
        entry_price=100.0, atr=2.0, multiplier=2.5, side="diagonal",
    ) is None


def test_atr_initial_stop_negative_stop_returns_none():
    # Long where ATR × multiplier > entry → stop would be <= 0.
    assert sizing.atr_initial_stop(
        entry_price=2.0, atr=5.0, multiplier=1.0, side="long",
    ) is None


# ---------------------------------------------------------------------------
# resolve_atr_multiplier — precedence
# ---------------------------------------------------------------------------

def test_resolve_multiplier_default_when_no_settings():
    assert sizing.resolve_atr_multiplier(
        strategy_id="botnet101-3-bar-low",
        settings_stops=None, legacy_multiple=None,
    ) == sizing.DEFAULT_ATR_INITIAL_MULTIPLIER


def test_resolve_multiplier_global_override():
    out = sizing.resolve_atr_multiplier(
        strategy_id="botnet101-3-bar-low",
        settings_stops={"atr_multiplier": 3.0},
    )
    assert out == 3.0


def test_resolve_multiplier_per_strategy_beats_global():
    settings_stops = {
        "atr_multiplier": 3.0,
        "per_strategy": {
            "botnet101-3-bar-low": {"atr_multiplier": 2.0},
        },
    }
    assert sizing.resolve_atr_multiplier(
        strategy_id="botnet101-3-bar-low",
        settings_stops=settings_stops,
    ) == 2.0
    # Other strategies fall back to global.
    assert sizing.resolve_atr_multiplier(
        strategy_id="other", settings_stops=settings_stops,
    ) == 3.0


def test_resolve_multiplier_legacy_wins_over_new_block():
    # Phase 4.6 trend strategies set stop_loss_atr_multiple — that must
    # keep working unchanged when the new `stops` block isn't configured.
    out = sizing.resolve_atr_multiplier(
        strategy_id="trend-strategy",
        settings_stops={"atr_multiplier": 2.5},
        legacy_multiple=1.5,
    )
    assert out == 1.5


def test_resolve_multiplier_zero_legacy_falls_through():
    # legacy_multiple=0 → ignore, use settings_stops.
    out = sizing.resolve_atr_multiplier(
        strategy_id="x",
        settings_stops={"atr_multiplier": 2.5},
        legacy_multiple=0,
    )
    assert out == 2.5


def test_resolve_multiplier_garbage_input_falls_through():
    # Non-numeric per-strategy entry → falls back to global.
    out = sizing.resolve_atr_multiplier(
        strategy_id="x",
        settings_stops={
            "atr_multiplier": 2.5,
            "per_strategy": {"x": {"atr_multiplier": "garbage"}},
        },
    )
    assert out == 2.5
    # Garbage global → default.
    out = sizing.resolve_atr_multiplier(
        strategy_id="x", settings_stops={"atr_multiplier": -1.0},
    )
    assert out == sizing.DEFAULT_ATR_INITIAL_MULTIPLIER


# ---------------------------------------------------------------------------
# fixed_percent_stop
# ---------------------------------------------------------------------------

def test_fixed_percent_stop_long():
    assert sizing.fixed_percent_stop(
        entry_price=100.0, percent=0.05, side="long",
    ) == pytest.approx(95.0)


def test_fixed_percent_stop_short():
    assert sizing.fixed_percent_stop(
        entry_price=100.0, percent=0.05, side="short",
    ) == pytest.approx(105.0)


def test_fixed_percent_stop_disabled():
    assert sizing.fixed_percent_stop(
        entry_price=100.0, percent=0, side="long",
    ) is None
    assert sizing.fixed_percent_stop(
        entry_price=100.0, percent=None, side="long",
    ) is None
    assert sizing.fixed_percent_stop(
        entry_price=0, percent=0.05, side="long",
    ) is None


# ---------------------------------------------------------------------------
# resolve_initial_stop — fallback path
# ---------------------------------------------------------------------------

def test_resolve_initial_stop_atr_path():
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="winner", side="long",
        settings_stops={"atr_multiplier": 2.5},
    )
    assert out["method"] == "atr_initial"
    assert out["stop_price"] == pytest.approx(95.0)
    assert out["multiplier"] == 2.5
    assert out["fallback_percent"] is None


def test_resolve_initial_stop_falls_back_to_fixed_percent():
    # ATR is None (e.g. <14 bars of history) but fallback is configured.
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=None,
        strategy_id="winner", side="long",
        settings_stops={
            "atr_multiplier": 2.5,
            "fixed_percent_fallback": 0.05,
        },
    )
    assert out["method"] == "fixed_percent"
    assert out["stop_price"] == pytest.approx(95.0)
    assert out["fallback_percent"] == 0.05


def test_resolve_initial_stop_no_atr_no_fallback_returns_none_method():
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=None,
        strategy_id="winner", side="long",
        settings_stops={"atr_multiplier": 2.5},
    )
    assert out["method"] is None
    assert out["stop_price"] is None


def test_resolve_initial_stop_short_side():
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=2.0,
        strategy_id="winner", side="short",
        settings_stops={"atr_multiplier": 2.5},
    )
    assert out["method"] == "atr_initial"
    assert out["stop_price"] == pytest.approx(105.0)


def test_resolve_initial_stop_short_fixed_percent_fallback():
    out = sizing.resolve_initial_stop(
        entry_price=100.0, atr=None,
        strategy_id="winner", side="short",
        settings_stops={"fixed_percent_fallback": 0.04},
    )
    assert out["method"] == "fixed_percent"
    assert out["stop_price"] == pytest.approx(104.0)


# ---------------------------------------------------------------------------
# auto_trader integration
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "mr-strat"}})
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


def test_new_stops_block_enables_atr_initial(isolated_db):
    """Settings with a `stops.atr_multiplier` block (no legacy
    stop_loss_atr_multiple) enables ATR initial stops at 2.5 × ATR."""
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {
        **_winner_settings(),
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
    }
    # 15 bars (14 TRs) so the new 14-bar default has enough data.
    bars = _bars(*[(101, 99, 100)] * 16)
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    assert stop is not None
    assert stop["stop_method"] == "atr_initial"
    assert stop["requested_multiple"] == 2.5
    assert stop["stop_price"] == pytest.approx(95.0)  # 100 - 2.5 × 2


def test_per_strategy_multiplier_override(isolated_db):
    """`stops.per_strategy.{strategy_id}.atr_multiplier` overrides the
    global multiplier for that strategy only."""
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {
        **_winner_settings(),
        "stops": {
            "atr_multiplier": 2.5,
            "atr_period": 14,
            "per_strategy": {
                "winner": {"atr_multiplier": 2.0},
            },
        },
    }
    bars = _bars(*[(101, 99, 100)] * 16)
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    assert stop["requested_multiple"] == 2.0
    assert stop["stop_price"] == pytest.approx(96.0)  # 100 - 2.0 × 2


def test_fixed_percent_fallback_when_atr_unavailable(isolated_db):
    """When ATR can't be computed (<14 bars), falls back to fixed-percent
    stop if `stops.fixed_percent_fallback` is configured."""
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {
        **_winner_settings(),
        "stops": {
            "atr_multiplier": 2.5,
            "atr_period": 14,
            "fixed_percent_fallback": 0.05,
        },
    }
    # Only 5 bars → ATR(14) is None → falls back.
    bars = _bars(*[(101, 99, 100)] * 5)
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    assert stop is not None
    assert stop["atr"] is None
    assert stop["stop_method"] == "fixed_percent"
    assert stop["fallback_percent"] == 0.05
    assert stop["stop_price"] == pytest.approx(95.0)
    assert stop["status"] == "dry_run"


def test_no_stop_when_atr_missing_and_no_fallback(isolated_db):
    """ATR unavailable + no fallback configured → no stop attached."""
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {
        **_winner_settings(),
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
    }
    bars = _bars(*[(101, 99, 100)] * 5)  # too few for ATR(14)
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    assert stop["status"] == "no_stop"
    assert stop["stop_price"] is None
    assert stop["stop_method"] is None


def test_legacy_setting_still_works(isolated_db):
    """Phase 4.6 trend strategies use `stop_loss_atr_multiple` directly.
    That code path must keep working — same multiplier, same 20-bar
    ATR window."""
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = {**_winner_settings(), "stop_loss_atr_multiple": 2.0}
    bars = _bars(*[(101, 99, 100)] * 25)  # ≥ 21 bars for ATR(20)
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    stop = actions[0]["stop"]
    assert stop["requested_multiple"] == 2.0
    assert stop["stop_price"] == pytest.approx(96.0)
    assert stop["stop_method"] == "atr_initial"


def test_entry_stops_column_recorded_on_paper_trades(isolated_db, monkeypatch):
    """The new `entry_stops` column on paper_trades records which method
    was used for the initial stop on both the entry row and the stop row."""
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")

    def fake_submit_market(client, *, symbol, qty, side, client_order_id=None):
        order = MagicMock()
        order.id = "alpaca-entry-1"
        order.status = "filled"
        order.submitted_at = "2026-05-14T14:00:00Z"
        order.filled_avg_price = 100.0
        return order

    def fake_submit_stop(client, *, symbol, qty, stop_price, client_order_id=None):
        order = MagicMock()
        order.id = "alpaca-stop-1"
        order.status = "accepted"
        order.submitted_at = "2026-05-14T14:00:01Z"
        return order

    monkeypatch.setattr(at, "_submit_market_order", fake_submit_market)
    monkeypatch.setattr(stops_mod, "submit_atr_stop", fake_submit_stop)

    settings = {
        **_winner_settings(),
        "dry_run": False,
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
    }
    bars = _bars(*[(101, 99, 100)] * 16)
    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(),
        bars_fetcher=lambda sym: bars,
    )
    # Both entry and stop rows have entry_stops='atr_initial'.
    entry_row = conn.execute(
        "SELECT entry_stops FROM paper_trades "
        "WHERE alpaca_order_id='alpaca-entry-1'"
    ).fetchone()
    stop_row = conn.execute(
        "SELECT entry_stops FROM paper_trades "
        "WHERE alpaca_order_id='alpaca-stop-1'"
    ).fetchone()
    assert entry_row["entry_stops"] == "atr_initial"
    assert stop_row["entry_stops"] == "atr_initial"


def test_entry_stops_column_records_fallback_method(isolated_db, monkeypatch):
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")

    def fake_submit_market(client, *, symbol, qty, side, client_order_id=None):
        order = MagicMock()
        order.id = "alpaca-entry-2"
        order.status = "filled"
        order.submitted_at = "2026-05-14T14:00:00Z"
        order.filled_avg_price = 100.0
        return order

    def fake_submit_stop(client, *, symbol, qty, stop_price, client_order_id=None):
        order = MagicMock()
        order.id = "alpaca-stop-2"
        order.status = "accepted"
        order.submitted_at = "2026-05-14T14:00:01Z"
        return order

    monkeypatch.setattr(at, "_submit_market_order", fake_submit_market)
    monkeypatch.setattr(stops_mod, "submit_atr_stop", fake_submit_stop)

    settings = {
        **_winner_settings(),
        "dry_run": False,
        "stops": {
            "atr_multiplier": 2.5,
            "atr_period": 14,
            "fixed_percent_fallback": 0.05,
        },
    }
    # Too few bars for ATR(14) → fallback should fire.
    bars = _bars(*[(101, 99, 100)] * 5)
    at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=MagicMock(),
        bars_fetcher=lambda sym: bars,
    )
    entry_row = conn.execute(
        "SELECT entry_stops, stop_price FROM paper_trades "
        "WHERE alpaca_order_id='alpaca-entry-2'"
    ).fetchone()
    stop_row = conn.execute(
        "SELECT entry_stops, stop_price FROM paper_trades "
        "WHERE alpaca_order_id='alpaca-stop-2'"
    ).fetchone()
    assert entry_row["entry_stops"] == "fixed_percent"
    assert stop_row["entry_stops"] == "fixed_percent"
    assert stop_row["stop_price"] == pytest.approx(95.0)


def test_short_side_signal_uses_short_stop_math(isolated_db):
    """A signal whose signal_type starts with 'short' triggers the
    short-direction stop math (entry + k×ATR instead of entry - k×ATR).
    """
    # The auto-trader currently routes short signals (the buy() path
    # only handles long_entry signals). Even so, the stop helper itself
    # is what would compute the level — verify directly through
    # resolve_initial_stop and also via _maybe_attach_stop with a
    # short_entry signal_type so we exercise the side resolution in
    # auto_trader._maybe_attach_stop.
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    # Build a short signal row manually + run _maybe_attach_stop in dry-run.
    sid = db.record_signal(
        conn, strategy_id="winner", symbol="GDX",
        bar_ts="2026-05-14", signal_type="short_entry",
        close=100.0, bar_interval="1d",
    )
    sig = conn.execute(
        "SELECT * FROM signals WHERE id=?", (sid,),
    ).fetchone()
    sig_dict = dict(sig)
    settings = {"stops": {"atr_multiplier": 2.5, "atr_period": 14}}
    bars = _bars(*[(101, 99, 100)] * 16)
    info = at._maybe_attach_stop(
        conn, client=None, settings=settings, sig=sig_dict,
        entry_fill=100.0, qty=10, client_order_id="cid",
        bars_fetcher=lambda sym: bars, dry_run=True,
    )
    assert info["stop_method"] == "atr_initial"
    # Short: stop = entry + k×ATR = 100 + 2.5×2 = 105
    assert info["stop_price"] == pytest.approx(105.0)


def test_disabled_when_neither_legacy_nor_new_block_configured(isolated_db):
    """No `stop_loss_atr_multiple` AND no `stops` block → no stop info."""
    conn = _seed_for_buy("winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = _winner_settings()  # no stops config at all
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: _bars(*[(101, 99, 100)] * 16),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["stop"] is None
