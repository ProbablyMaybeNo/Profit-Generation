"""M10 (Sprint 3) — trend loser cap.

The live Donchian book had no hard per-position max-loss floor. The 2.5x ATR
trailing stop only ratchets DOWN from the running high, so a name that gaps or
bleeds straight off entry never engages it and bled past any sane single-name
loss (ENPH -16%, AVGO -16% the week of 2026-06-03; today 3 exits at -6.6%).

M10 adds a HARD per-position max-loss cap (default -8% from entry) for the trend
book. When an open position's latest close is at/below entry*(1-cap), it is
force-closed via the existing _process_exit close path with
exit_reason='max_loss_cap'. A winner or a loser still inside the cap is left
completely untouched.

BEHAVIORAL test on the PROD path: drives the real
_check_max_loss_caps_for_open_positions and proves a trend position breaching
the cap is force-closed (a sell is recorded AND the outcome closes with
exit_reason='max_loss_cap'), while a normal small-loser stays OPEN. On the old
code the function (and the per-strategy config) did not exist, so the position
would be left to bleed -> this FAILS pre-M10.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from strategies.trend import TREND_DECLARATIONS  # noqa: E402


TREND_SID = "trend-donchian-breakout-20"


class _FakeFilledOrder:
    def __init__(self, oid, fill_price):
        self.id = oid
        self.status = "filled"
        self.submitted_at = "2026-06-03T20:00:00Z"
        self.filled_at = "2026-06-03T20:00:01Z"
        self.filled_avg_price = fill_price


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_open_trend_position(conn, *, strategy_id=TREND_SID, symbol,
                              entry_date, entry_price=100.0):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    sig_id = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=entry_date, signal_type="long_entry",
        close=entry_price, bar_interval="1d",
    )
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at, fill_price) "
        "VALUES (?, ?, ?, ?, 'buy', 10, ?, 'filled', ?, ?)",
        (f"buy-{symbol}", sig_id, strategy_id, symbol, entry_date,
         entry_date, entry_price),
    )
    db.open_outcome(conn, signal_id=sig_id, entry_ts=entry_date,
                    entry_price=entry_price)
    conn.commit()
    return sig_id


def test_trend_declarations_carry_max_loss_cap():
    """The fix is config-backed, not invented per-call. Both live-relevant
    trend strategies (donchian + ma-cross) declare a positive cap."""
    by_id = {d["id"]: d for d in TREND_DECLARATIONS}
    for sid in ("trend-donchian-breakout-20", "trend-ma-cross-20-50"):
        mlc = by_id[sid].get("max_loss_cap")
        assert isinstance(mlc, dict), f"{sid} missing max_loss_cap"
        assert float(mlc["max_loss_pct"]) > 0


def test_max_loss_cap_force_closes_blown_out_trend_position(isolated_db,
                                                            monkeypatch):
    """A trend position down past -8% from entry is force-closed with
    exit_reason='max_loss_cap'. This is the ENPH/AVGO -16% tail being capped."""
    conn = db.init_db()
    entry_price = 100.0
    sig_id = _seed_open_trend_position(
        conn, symbol="ENPH", entry_date="2026-06-01", entry_price=entry_price,
    )

    # Latest close at $88 = -12% from entry -> past the -8% cap.
    fill_price = 88.0
    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder(
            f"sell-{symbol}", fill_price),
    )
    bars = {"ENPH": [{"ts": "2026-06-03", "high": 90, "low": 87,
                      "close": fill_price}]}

    actions = at._check_max_loss_caps_for_open_positions(
        conn, settings={}, client=object(), dry_run=False,
        bars_fetcher=lambda s: bars.get(s, []),
        tracked_strategies=TREND_DECLARATIONS,
        asof=date(2026, 6, 3),
    )
    assert len(actions) == 1
    assert actions[0]["action"] == "SELL"
    assert actions[0]["exit_reason"] == "max_loss_cap"
    assert actions[0]["loss_pct"] <= -8.0
    assert actions[0]["max_loss_pct"] == 8.0

    # Position closed: a sell was recorded.
    sells = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE side='sell' AND symbol='ENPH'",
    ).fetchone()[0]
    assert sells == 1

    # Outcome closed with the max_loss_cap reason.
    o = conn.execute(
        "SELECT status, exit_reason, exit_price FROM outcomes WHERE signal_id=?",
        (sig_id,),
    ).fetchone()
    assert o["status"] == "closed", \
        "M10 regression: blown-out trend position left OPEN to bleed past -8%"
    assert o["exit_reason"] == "max_loss_cap"
    assert o["exit_price"] == pytest.approx(88.0)


def test_max_loss_cap_leaves_small_loser_untouched(isolated_db, monkeypatch):
    """A position down only -3% (inside the -8% cap) is NOT closed — the cap
    only catches the tail, it doesn't trim normal small losers."""
    conn = db.init_db()
    sig_id = _seed_open_trend_position(
        conn, symbol="SPY", entry_date="2026-06-01", entry_price=100.0,
    )
    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder("x", 97.0),
    )
    # -3% from entry -> inside the cap.
    actions = at._check_max_loss_caps_for_open_positions(
        conn, settings={}, client=object(), dry_run=False,
        bars_fetcher=lambda s: [{"high": 98, "low": 96, "close": 97.0}],
        tracked_strategies=TREND_DECLARATIONS,
        asof=date(2026, 6, 3),
    )
    assert actions == []
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (sig_id,),
    ).fetchone()
    assert o["status"] == "open"


def test_max_loss_cap_leaves_winner_untouched(isolated_db, monkeypatch):
    """A winner is never force-closed by the loser cap."""
    conn = db.init_db()
    sig_id = _seed_open_trend_position(
        conn, symbol="QQQ", entry_date="2026-06-01", entry_price=100.0,
    )
    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder("x", 115.0),
    )
    actions = at._check_max_loss_caps_for_open_positions(
        conn, settings={}, client=object(), dry_run=False,
        bars_fetcher=lambda s: [{"high": 116, "low": 113, "close": 115.0}],
        tracked_strategies=TREND_DECLARATIONS,
        asof=date(2026, 6, 3),
    )
    assert actions == []
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (sig_id,),
    ).fetchone()
    assert o["status"] == "open"


def test_max_loss_cap_skips_strategy_without_cap_config(isolated_db,
                                                        monkeypatch):
    """A strategy that doesn't declare a max_loss_cap is never capped, even
    when blown out — M10 only touches the trend book that opts in."""
    conn = db.init_db()
    sig_id = _seed_open_trend_position(
        conn, strategy_id="mr-no-cap", symbol="GDX",
        entry_date="2026-06-01", entry_price=50.0,
    )
    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder("x", 30.0),
    )
    # -40% from entry, but the strategy declares no cap (and there's no global
    # block in this settings dict) -> untouched.
    actions = at._check_max_loss_caps_for_open_positions(
        conn, settings={}, client=object(), dry_run=False,
        bars_fetcher=lambda s: [{"close": 30.0}],
        tracked_strategies=[{"id": "mr-no-cap", "strategy_class": "mr"}],
        asof=date(2026, 6, 3),
    )
    assert actions == []
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (sig_id,),
    ).fetchone()
    assert o["status"] == "open"


def test_max_loss_cap_global_block_applies_to_undeclared_strategy(isolated_db,
                                                                  monkeypatch):
    """The global settings.max_loss_cap block caps ANY strategy when no
    per-strategy declaration exists (config-driven, disablable)."""
    conn = db.init_db()
    sig_id = _seed_open_trend_position(
        conn, strategy_id="some-other-strat", symbol="AVGO",
        entry_date="2026-06-01", entry_price=200.0,
    )
    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder("s", 168.0),
    )
    # -16% from entry -> past a -8% global cap.
    actions = at._check_max_loss_caps_for_open_positions(
        conn, settings={"max_loss_cap": {"max_loss_pct": 8.0}},
        client=object(), dry_run=False,
        bars_fetcher=lambda s: [{"close": 168.0}],
        tracked_strategies=[{"id": "some-other-strat"}],
        asof=date(2026, 6, 3),
    )
    assert len(actions) == 1
    assert actions[0]["exit_reason"] == "max_loss_cap"
    o = conn.execute(
        "SELECT status, exit_reason FROM outcomes WHERE signal_id=?",
        (sig_id,),
    ).fetchone()
    assert o["status"] == "closed"
    assert o["exit_reason"] == "max_loss_cap"


def test_max_loss_cap_disabled_when_pct_zero(isolated_db, monkeypatch):
    """max_loss_pct=0 disables the cap (the documented off switch)."""
    conn = db.init_db()
    sig_id = _seed_open_trend_position(
        conn, strategy_id="off-strat", symbol="TSLA",
        entry_date="2026-06-01", entry_price=100.0,
    )
    monkeypatch.setattr(
        at, "_submit_market_order",
        lambda client, symbol, qty, side: _FakeFilledOrder("s", 50.0),
    )
    actions = at._check_max_loss_caps_for_open_positions(
        conn, settings={"max_loss_cap": {"max_loss_pct": 0}},
        client=object(), dry_run=False,
        bars_fetcher=lambda s: [{"close": 50.0}],
        tracked_strategies=[{"id": "off-strat"}],
        asof=date(2026, 6, 3),
    )
    assert actions == []
    o = conn.execute(
        "SELECT status FROM outcomes WHERE signal_id=?", (sig_id,),
    ).fetchone()
    assert o["status"] == "open"
