"""6.4.2 — SAR overlay opt-in for trend strategies (30-day A/B).

Validates:
  - Trend strategy declarations opt in via sar_overlay: "shadow".
  - paper_trades_sar_overlay table created with the expected columns.
  - strategy_has_sar_overlay vs strategy_has_sar_shadow semantics.
  - record_shadow_exit writes the expected row + computes pnl.
  - **no-impact-on-live-PnL invariant** — running the auto_trader
    trailing-exit walker on a shadow-enabled strategy adds rows to
    paper_trades_sar_overlay but does NOT modify paper_trades or change
    the live exit decision.
  - A/B aggregation math (pnl_delta, win_rate_delta, per-strategy).
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader  # noqa: E402
from monitoring import sar_overlay as so  # noqa: E402
from strategies.trend import TREND_DECLARATIONS  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


def _bars(*hlc):
    return [{"high": h, "low": l, "close": c} for (h, l, c) in hlc]


# ---------------------------------------------------------------------------
# 1. Declarations opt-in
# ---------------------------------------------------------------------------

def test_three_trend_strategies_opt_into_shadow():
    """All three Phase 4.6.3 trend strategies declare shadow mode."""
    expected_ids = {
        "trend-donchian-breakout-20",
        "trend-ma-cross-20-50",
        "trend-new-high-volume",
    }
    matched = {
        d["id"]: d for d in TREND_DECLARATIONS if d["id"] in expected_ids
    }
    assert set(matched) == expected_ids, "missing one of the three trend strats"
    for sid, decl in matched.items():
        assert decl.get("sar_overlay") == "shadow", (
            f"{sid} should declare sar_overlay='shadow', got "
            f"{decl.get('sar_overlay')!r}"
        )


def test_shadow_predicate_does_not_imply_live_overlay():
    """sar_overlay='shadow' is observe-only. It must return:
      - strategy_has_sar_overlay() → False (no live exit change)
      - strategy_has_sar_shadow()  → True  (do record the shadow)
    """
    meta = {"sar_overlay": "shadow"}
    assert so.strategy_has_sar_overlay(meta) is False
    assert so.strategy_has_sar_shadow(meta) is True


def test_live_overlay_predicate_also_records_shadow():
    """Live opt-in is a superset of shadow — shadow records remain useful
    even when SAR fires for real."""
    assert so.strategy_has_sar_overlay({"sar_overlay": True}) is True
    assert so.strategy_has_sar_shadow({"sar_overlay": True}) is True


def test_no_opt_in_means_no_shadow():
    assert so.strategy_has_sar_shadow({}) is False
    assert so.strategy_has_sar_shadow(None) is False
    assert so.strategy_has_sar_shadow({"sar_overlay": False}) is False


# ---------------------------------------------------------------------------
# 2. Parallel-record shape
# ---------------------------------------------------------------------------

def test_paper_trades_sar_overlay_table_exists(conn):
    """init_db() must create the parallel shadow table."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type='table' AND name='paper_trades_sar_overlay'"
    ).fetchone()
    assert cur is not None, "paper_trades_sar_overlay table missing"


def test_paper_trades_sar_overlay_has_expected_columns(conn):
    """Schema check — the columns documented in the milestone spec."""
    cols = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(paper_trades_sar_overlay)"
        ).fetchall()
    }
    expected = {
        "id", "recorded_at", "strategy_id", "symbol", "side",
        "entry_order_id", "entry_price", "qty",
        "shadow_exit_price", "shadow_sar", "shadow_reason",
        "real_exit_price", "real_exit_reason",
        "shadow_pnl", "real_pnl", "notes",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_record_shadow_exit_writes_row_and_computes_pnl(conn):
    rowid = so.record_shadow_exit(
        conn,
        strategy_id="trend-donchian-breakout-20", symbol="SPY",
        side="long",
        entry_order_id="ord-1",
        entry_price=400.0, qty=10,
        shadow_exit_price=410.0, shadow_sar=409.5,
        real_exit_price=405.0, real_exit_reason="trailing_stop_hit",
        now_iso="2026-05-20T15:00:00+00:00",
    )
    assert rowid is not None
    row = conn.execute(
        "SELECT * FROM paper_trades_sar_overlay WHERE id=?", (rowid,),
    ).fetchone()
    assert row["strategy_id"] == "trend-donchian-breakout-20"
    assert row["symbol"] == "SPY"
    assert row["shadow_exit_price"] == pytest.approx(410.0)
    assert row["shadow_sar"] == pytest.approx(409.5)
    assert row["shadow_reason"] == "sar_flip"
    # Pnl math: long with (410 - 400) × 10 = 100. Real: (405 - 400) × 10 = 50.
    assert row["shadow_pnl"] == pytest.approx(100.0)
    assert row["real_pnl"] == pytest.approx(50.0)


def test_record_shadow_exit_short_side_pnl_inverts(conn):
    so.record_shadow_exit(
        conn,
        strategy_id="trend-short", symbol="QQQ",
        side="short",
        entry_price=400.0, qty=5,
        shadow_exit_price=390.0, real_exit_price=395.0,
    )
    row = conn.execute(
        "SELECT shadow_pnl, real_pnl FROM paper_trades_sar_overlay "
        " WHERE strategy_id='trend-short' AND symbol='QQQ'"
    ).fetchone()
    # short: (entry - exit) × qty.  shadow: (400 - 390) × 5 = 50
    assert row["shadow_pnl"] == pytest.approx(50.0)
    assert row["real_pnl"] == pytest.approx(25.0)


def test_record_shadow_exit_handles_missing_inputs(conn):
    rowid = so.record_shadow_exit(
        conn,
        strategy_id="x", symbol="X",
        shadow_exit_price=100.0,
    )
    assert rowid is not None
    row = conn.execute(
        "SELECT shadow_pnl, real_pnl FROM paper_trades_sar_overlay "
        " WHERE id=?", (rowid,),
    ).fetchone()
    # No entry_price + qty → pnl is None.
    assert row["shadow_pnl"] is None
    assert row["real_pnl"] is None


# ---------------------------------------------------------------------------
# 3. No-impact-on-live-PnL invariant
# ---------------------------------------------------------------------------

def _seed_open_position(conn, *, strategy_id, symbol, order_id, qty, price):
    """Insert a single OPEN buy into paper_trades (no matching sell)."""
    db.record_paper_trade(conn, {
        "alpaca_order_id": order_id,
        "signal_id": None,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": "buy",
        "qty": qty,
        "order_type": "market",
        "fill_price": price,
        "limit_price": price,
        "status": "filled",
        "submitted_at": "2026-05-19T15:00:00+00:00",
        "filled_at": "2026-05-19T15:00:00+00:00",
    })


def _row_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _snapshot_paper_trades(conn):
    """Capture the full paper_trades state as comparable tuples."""
    return [
        tuple(r) for r in conn.execute(
            "SELECT alpaca_order_id, strategy_id, symbol, side, qty, "
            "       status, fill_price, stop_price, notes "
            "  FROM paper_trades ORDER BY id"
        ).fetchall()
    ]


def test_shadow_does_not_affect_paper_trades_when_sar_flips(conn, monkeypatch):
    """The invariant: when SAR overlay (shadow mode) would have fired,
    NO new row is added to paper_trades, the existing paper_trades row
    is unchanged, AND a shadow row IS written.
    """
    sid = "trend-donchian-breakout-20"
    sym = "SPY"
    _seed_open_position(
        conn, strategy_id=sid, symbol=sym,
        order_id="ord-shadow-1", qty=10, price=400.0,
    )

    # Seed SAR state so the next bar triggers a flip (SAR sits above
    # bar.low for a long → flip).
    so.init_sar(
        conn, strategy_id=sid, symbol=sym,
        bars=_bars(*[(395 + i, 393 + i, 394 + i) for i in range(10)]),
        direction="long",
    )

    # Bars fetcher returns a final bar whose LOW (380) crosses the SAR
    # but whose close (382) does NOT cross the trailing stop (no trailing
    # stop is set on this position, so trailing_hit is False).
    def bars_fetcher(_sym):
        return _bars(*[(400 + i, 399 + i, 400 + i) for i in range(5)]) + [
            {"high": 385.0, "low": 380.0, "close": 382.0},
        ]

    before = _snapshot_paper_trades(conn)
    before_n = _row_count(conn, "paper_trades")
    shadow_n_before = _row_count(conn, "paper_trades_sar_overlay")

    actions = auto_trader._check_trailing_exits_for_open_positions(
        conn, settings={},
        client=None, dry_run=True,
        bars_fetcher=bars_fetcher,
        tracked_strategies=TREND_DECLARATIONS,
    )

    after = _snapshot_paper_trades(conn)
    after_n = _row_count(conn, "paper_trades")
    shadow_n_after = _row_count(conn, "paper_trades_sar_overlay")

    # INVARIANT: paper_trades unchanged.
    assert after_n == before_n, (
        "paper_trades row count changed — shadow mode altered live PnL"
    )
    assert after == before, (
        "paper_trades row contents changed — shadow mode altered live PnL"
    )
    # And no SELL action was emitted (no trailing stop, only a SAR flip
    # in shadow — must not produce a live exit).
    assert all(
        a.get("action") not in ("SELL", "DRY_SELL") for a in actions
    ), f"shadow mode emitted a SELL/DRY_SELL action: {actions}"
    # A shadow row WAS written.
    assert shadow_n_after == shadow_n_before + 1, (
        "expected exactly one new paper_trades_sar_overlay row"
    )


def test_shadow_records_alongside_real_trailing_exit(conn):
    """When BOTH real trailing stop AND SAR fire, the live exit goes
    through paper_trades (normal behavior) AND a shadow row is written
    with the real exit data populated for the A/B comparison."""
    sid = "trend-donchian-breakout-20"
    sym = "QQQ"
    _seed_open_position(
        conn, strategy_id=sid, symbol=sym,
        order_id="ord-both-1", qty=5, price=300.0,
    )
    # SAR state — flip will trigger.
    so.init_sar(
        conn, strategy_id=sid, symbol=sym,
        bars=_bars(*[(295 + i, 293 + i, 294 + i) for i in range(10)]),
        direction="long",
    )
    # Trailing stop high above the next bar's close → trailing also hits.
    from monitoring import trailing_stops as ts
    ts.upsert_stop(
        conn, strategy_id=sid, symbol=sym,
        method="atr_trail", stop_price=290.0, extreme_price=310.0,
        side="long", now_iso="2026-05-19T15:00:00+00:00",
    )

    def bars_fetcher(_sym):
        return _bars(*[(300 + i, 299 + i, 300 + i) for i in range(5)]) + [
            {"high": 290.0, "low": 285.0, "close": 285.0},
        ]

    shadow_before = _row_count(conn, "paper_trades_sar_overlay")
    actions = auto_trader._check_trailing_exits_for_open_positions(
        conn, settings={},
        client=None, dry_run=True,
        bars_fetcher=bars_fetcher,
        tracked_strategies=TREND_DECLARATIONS,
    )
    shadow_after = _row_count(conn, "paper_trades_sar_overlay")

    # Shadow row written.
    assert shadow_after == shadow_before + 1
    shadow_row = conn.execute(
        "SELECT * FROM paper_trades_sar_overlay "
        " WHERE strategy_id=? AND symbol=? ORDER BY id DESC LIMIT 1",
        (sid, sym),
    ).fetchone()
    assert shadow_row["real_exit_reason"] is not None
    assert shadow_row["real_exit_price"] == pytest.approx(285.0)
    # Real trailing exit also fired (DRY_SELL since dry_run=True).
    sell_actions = [
        a for a in actions if a.get("action") in ("SELL", "DRY_SELL")
    ]
    assert sell_actions, "trailing stop didn't trigger live exit"


def test_no_shadow_row_when_sar_does_not_flip(conn):
    """If the bar's range doesn't cross the SAR, no shadow row."""
    sid = "trend-ma-cross-20-50"
    sym = "IWM"
    _seed_open_position(
        conn, strategy_id=sid, symbol=sym,
        order_id="ord-no-flip", qty=8, price=200.0,
    )
    so.init_sar(
        conn, strategy_id=sid, symbol=sym,
        bars=_bars(*[(195 + i, 193 + i, 194 + i) for i in range(10)]),
        direction="long",
    )

    def bars_fetcher(_sym):
        # New bar with high above SAR and low also above SAR → no flip.
        return _bars(*[(205 + i, 204 + i, 205 + i) for i in range(5)])

    auto_trader._check_trailing_exits_for_open_positions(
        conn, settings={},
        client=None, dry_run=True,
        bars_fetcher=bars_fetcher,
        tracked_strategies=TREND_DECLARATIONS,
    )
    assert _row_count(conn, "paper_trades_sar_overlay") == 0


def test_no_shadow_row_when_strategy_not_opt_in(conn):
    """Strategies without sar_overlay declared get no shadow rows even
    when SAR state exists for the symbol."""
    sid = "mean-rev-rsi2"  # no sar_overlay
    sym = "TLT"
    _seed_open_position(
        conn, strategy_id=sid, symbol=sym,
        order_id="ord-mr-1", qty=4, price=100.0,
    )
    so.init_sar(
        conn, strategy_id=sid, symbol=sym,
        bars=_bars(*[(95 + i, 93 + i, 94 + i) for i in range(10)]),
        direction="long",
    )

    def bars_fetcher(_sym):
        # Bar that would cross SAR if the strategy were enrolled.
        return _bars(*[(110 + i, 109 + i, 110 + i) for i in range(5)]) + [
            {"high": 85.0, "low": 80.0, "close": 82.0},
        ]

    # tracked_strategies has no entry for mean-rev-rsi2 → no opt-in.
    auto_trader._check_trailing_exits_for_open_positions(
        conn, settings={},
        client=None, dry_run=True,
        bars_fetcher=bars_fetcher,
        tracked_strategies=TREND_DECLARATIONS,
    )
    assert _row_count(conn, "paper_trades_sar_overlay") == 0


# ---------------------------------------------------------------------------
# 4. A/B aggregation math
# ---------------------------------------------------------------------------

def test_aggregate_ab_pnl_delta_and_win_rate_delta(conn):
    """Given known shadow vs real outcomes, the aggregator returns the
    correct totals, deltas, win counts, and win-rate deltas."""
    # Strategy A — 3 paired events.
    # Shadow:  +10, +20, -5   → total +25, 2 wins / 3 = 0.6667
    # Real:    +5,  -10, -5   → total -10, 1 win / 3 = 0.3333
    # Delta pnl: +25 - -10 = +35
    # Delta wr:  0.6667 - 0.3333 = 0.3333
    for shadow, real in [(10.0, 5.0), (20.0, -10.0), (-5.0, -5.0)]:
        so.record_shadow_exit(
            conn, strategy_id="A", symbol="SPY",
            entry_price=100.0, qty=1,
            shadow_exit_price=100.0 + shadow,
            real_exit_price=100.0 + real,
            real_exit_reason="trailing_stop_hit",
        )
    # Strategy B — 2 paired events, both shadow loses.
    # Shadow: -3, -7  → -10, 0 wins
    # Real:   +4, +6  → +10, 2 wins
    # Delta pnl: -10 - 10 = -20
    # Delta wr:  0 - 1.0 = -1.0
    for shadow, real in [(-3.0, 4.0), (-7.0, 6.0)]:
        so.record_shadow_exit(
            conn, strategy_id="B", symbol="QQQ",
            entry_price=200.0, qty=1,
            shadow_exit_price=200.0 + shadow,
            real_exit_price=200.0 + real,
            real_exit_reason="trailing_stop_hit",
        )

    agg = so.aggregate_ab(conn)
    # Overall: 5 paired events.
    assert agg["count"] == 5
    assert agg["count_with_both_pnl"] == 5
    # Shadow total: 25 + (-10) = 15.  Real total: -10 + 10 = 0.
    assert agg["shadow_total_pnl"] == pytest.approx(15.0)
    assert agg["real_total_pnl"] == pytest.approx(0.0)
    assert agg["pnl_delta"] == pytest.approx(15.0)
    # Shadow wins: 2 + 0 = 2.  Real wins: 1 + 2 = 3.
    assert agg["shadow_wins"] == 2
    assert agg["real_wins"] == 3
    assert agg["shadow_win_rate"] == pytest.approx(0.4)
    assert agg["real_win_rate"] == pytest.approx(0.6)
    assert agg["win_rate_delta"] == pytest.approx(-0.2)

    # Per-strategy breakdown.
    by_s = agg["by_strategy"]
    assert set(by_s) == {"A", "B"}
    a = by_s["A"]
    assert a["count"] == 3
    assert a["shadow_total_pnl"] == pytest.approx(25.0)
    assert a["real_total_pnl"] == pytest.approx(-10.0)
    assert a["pnl_delta"] == pytest.approx(35.0)
    assert a["shadow_wins"] == 2
    assert a["real_wins"] == 1
    assert a["shadow_win_rate"] == pytest.approx(2 / 3)
    assert a["real_win_rate"] == pytest.approx(1 / 3)
    assert a["win_rate_delta"] == pytest.approx(1 / 3)

    b = by_s["B"]
    assert b["count"] == 2
    assert b["shadow_total_pnl"] == pytest.approx(-10.0)
    assert b["real_total_pnl"] == pytest.approx(10.0)
    assert b["pnl_delta"] == pytest.approx(-20.0)
    assert b["shadow_wins"] == 0
    assert b["real_wins"] == 2
    assert b["win_rate_delta"] == pytest.approx(-1.0)


def test_aggregate_ab_skips_rows_missing_real_pnl(conn):
    """Rows where the real exit hasn't fired yet (real_pnl is NULL) are
    excluded from the delta math but still counted in `count`."""
    # One paired row.
    so.record_shadow_exit(
        conn, strategy_id="A", symbol="SPY",
        entry_price=100.0, qty=1,
        shadow_exit_price=105.0, real_exit_price=102.0,
    )
    # One unpaired row (no real exit yet).
    so.record_shadow_exit(
        conn, strategy_id="A", symbol="SPY",
        entry_price=100.0, qty=1,
        shadow_exit_price=110.0,
        # no real_exit_price → real_pnl is NULL
    )
    agg = so.aggregate_ab(conn, strategy_id="A")
    assert agg["count"] == 2
    assert agg["count_with_both_pnl"] == 1
    assert agg["shadow_total_pnl"] == pytest.approx(5.0)
    assert agg["real_total_pnl"] == pytest.approx(2.0)
    assert agg["pnl_delta"] == pytest.approx(3.0)


def test_aggregate_ab_empty_returns_zero(conn):
    agg = so.aggregate_ab(conn)
    assert agg["count"] == 0
    assert agg["pnl_delta"] == 0.0
    assert agg["win_rate_delta"] == 0.0
    assert agg["by_strategy"] == {}


def test_aggregate_ab_strategy_filter(conn):
    """Filtering by strategy_id returns only that strategy's aggregate
    (no by_strategy field on the per-strategy form)."""
    so.record_shadow_exit(
        conn, strategy_id="A", symbol="SPY",
        entry_price=100.0, qty=1,
        shadow_exit_price=110.0, real_exit_price=105.0,
    )
    so.record_shadow_exit(
        conn, strategy_id="B", symbol="QQQ",
        entry_price=200.0, qty=1,
        shadow_exit_price=195.0, real_exit_price=190.0,
    )
    a = so.aggregate_ab(conn, strategy_id="A")
    assert a["count"] == 1
    assert "by_strategy" not in a
    assert a["shadow_total_pnl"] == pytest.approx(10.0)
    assert a["real_total_pnl"] == pytest.approx(5.0)
