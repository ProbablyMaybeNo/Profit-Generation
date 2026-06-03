"""7.5.2 — Skip-reason logging retrofitted to the existing risk gate.

Validates:
  - `intraday_skips` table exists after init_db() with expected columns and
    is idempotent across re-init.
  - `db.record_intraday_skip` appends one row per call (NOT deduped) so the
    same gate firing twice on the same signal yields two rows.
  - Every risk gate currently in monitoring.auto_trader.process_signals
    writes an `intraday_skips` row when it blocks a fire, with the gate
    short-name and a reason_detail.
  - **No-impact-on-paper_trades invariant** — for every gate, the
    `paper_trades` table is byte-identical before and after the retrofit
    fires. The block decision is unchanged.
  - `source='daily'` when the input signal is bar_interval='1d',
    `source='intraday_15m'` otherwise.
  - `/api/skip_reasons` route shape (top_5 by gate count + recent rows)
    and loopback-only enforcement.
"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "loser"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "untested"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


@pytest.fixture()
def base_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000, "skip_intraday_signals": False,
        # Disable opt-in gates by default — each test re-enables what it
        # cares about so other gates don't shadow it.
        "cool_down_losers": 0,
        "earnings_veto_days": 0,
        "veto_negative_sentiment": False,
    }


def _seed_outcomes(strat, returns, *, symbol="X"):
    conn = db.init_db()
    for i, ret in enumerate(returns):
        sid = db.record_signal(conn, strategy_id=strat, symbol=symbol,
                               bar_ts=f"2024-01-{i+1:02d}",
                               signal_type="long_entry", close=100.0,
                               bar_interval="1d")
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        exit_price = 100.0 * (1 + ret / 100)
        db.close_outcome(conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
                         exit_price=exit_price, exit_reason="long_exit_signal",
                         bars_held=1)
    return conn


def _snapshot_paper_trades(conn):
    return [
        tuple(r) for r in conn.execute(
            "SELECT alpaca_order_id, signal_id, strategy_id, symbol, side, "
            "       qty, order_type, limit_price, stop_price, submitted_at, "
            "       filled_at, fill_price, status, notes, pyramid_tier, "
            "       entry_stops "
            "  FROM paper_trades ORDER BY id"
        ).fetchall()
    ]


def _skip_rows(conn, *, gate=None):
    sql = "SELECT * FROM intraday_skips"
    params = ()
    if gate is not None:
        sql += " WHERE gate=?"
        params = (gate,)
    sql += " ORDER BY id ASC"
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------

def test_intraday_skips_table_exists(isolated_db):
    conn = db.init_db()
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type='table' AND name='intraday_skips'"
    ).fetchone()
    assert row is not None


def test_intraday_skips_has_expected_columns(isolated_db):
    conn = db.init_db()
    cols = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(intraday_skips)"
        ).fetchall()
    }
    expected = {
        "id", "recorded_at", "strategy_id", "symbol", "bar_ts",
        "signal_type", "gate", "reason_detail", "source",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_init_db_is_idempotent_on_intraday_skips(isolated_db):
    db.init_db()
    db.init_db()
    conn = db.init_db()
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type='table' AND name='intraday_skips'"
    ).fetchone()
    assert row is not None


def test_intraday_skips_has_expected_indexes(isolated_db):
    conn = db.init_db()
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type='index' AND tbl_name='intraday_skips'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_intraday_skips_recorded" in names
    assert "idx_intraday_skips_gate_recorded" in names


# ---------------------------------------------------------------------------
# 2. Helper behavior — append-only, one row per call
# ---------------------------------------------------------------------------

def test_record_intraday_skip_writes_row(isolated_db):
    conn = db.init_db()
    rowid = db.record_intraday_skip(
        conn,
        strategy_id="winner", symbol="GDX",
        bar_ts="2026-05-22", signal_type="long_entry",
        gate="cool_down",
        reason_detail="last 3 losers; paused 5d",
        source="daily",
    )
    assert rowid is not None
    rows = _skip_rows(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["strategy_id"] == "winner"
    assert r["symbol"] == "GDX"
    assert r["gate"] == "cool_down"
    assert r["reason_detail"] == "last 3 losers; paused 5d"
    assert r["source"] == "daily"
    assert r["recorded_at"]  # auto-stamped


def test_record_intraday_skip_appends_two_rows_on_repeated_call(isolated_db):
    conn = db.init_db()
    db.record_intraday_skip(
        conn, strategy_id="s", symbol="X", bar_ts="2026-05-22",
        signal_type="long_entry", gate="kill_switch",
        reason_detail="halted", source="daily",
    )
    db.record_intraday_skip(
        conn, strategy_id="s", symbol="X", bar_ts="2026-05-22",
        signal_type="long_entry", gate="kill_switch",
        reason_detail="halted", source="daily",
    )
    assert len(_skip_rows(conn, gate="kill_switch")) == 2


def test_record_intraday_skip_missing_gate_is_noop(isolated_db):
    conn = db.init_db()
    rowid = db.record_intraday_skip(
        conn, strategy_id="s", symbol="X", bar_ts="2026-05-22",
        signal_type="long_entry", gate="", reason_detail="x",
    )
    assert rowid is None
    assert _skip_rows(conn) == []


# ---------------------------------------------------------------------------
# 3. Gate coverage — every gate writes the right row
# ---------------------------------------------------------------------------

def test_gate_kill_switch_writes_skip_row(isolated_db, base_settings,
                                            monkeypatch):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    monkeypatch.setattr(
        "monitoring.kill_switch.load_state",
        lambda: {"live_trading_halted": True, "reason": "manual",
                  "set_at": "2026-05-14"},
    )
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=base_settings)
    after = _snapshot_paper_trades(conn)
    assert before == after, "kill_switch must not touch paper_trades"
    assert any(a["action"] == "KILL_SWITCH_HALT" for a in res["actions"])
    rows = _skip_rows(conn, gate="kill_switch")
    assert len(rows) == 1
    assert rows[0]["strategy_id"] == "winner"
    assert rows[0]["symbol"] == "GDX"
    assert rows[0]["source"] == "daily"


def test_gate_drawdown_breaker_writes_skip_row(isolated_db, base_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {
        **base_settings,
        "risk": {"max_daily_loss_pct": 2.0},
    }

    def acct():
        return {"portfolio_value": 95.0, "last_equity": 100.0}

    before = _snapshot_paper_trades(conn)
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=acct,
    )
    after = _snapshot_paper_trades(conn)
    assert before == after
    assert any(a["action"] == "SKIP_DAILY_DRAWDOWN" for a in res["actions"])
    rows = _skip_rows(conn, gate="drawdown_breaker")
    assert len(rows) >= 1


def test_gate_cool_down_writes_skip_row(isolated_db, base_settings):
    # 3 consecutive losers in early 2024 → losers exit on 2024-01-31
    # asof 2024-02-05 is inside the 5-day cool-down window.
    conn = _seed_outcomes("loser", [-1.0] * 30)
    db.record_signal(conn, strategy_id="loser", symbol="GDX",
                     bar_ts="2024-02-05", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {
        **base_settings,
        "cool_down_losers": 3, "cool_down_days": 5,
    }
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2024, 2, 5), settings=settings)
    after = _snapshot_paper_trades(conn)
    assert before == after
    assert any(a["action"] == "SKIP_COOL_DOWN" for a in res["actions"])
    rows = _skip_rows(conn, gate="cool_down")
    assert len(rows) == 1


def test_gate_earnings_veto_writes_skip_row(isolated_db, base_settings,
                                              monkeypatch):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18, symbol="GDX")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**base_settings, "earnings_veto_days": 2}
    # Stub earnings_calendar to force a veto.
    monkeypatch.setattr(
        "monitoring.earnings_calendar.is_within_earnings_window",
        lambda conn, sym, *, asof, window_trading_days: {
            "symbol": sym, "earnings_date": "2026-05-15",
            "window_trading_days": window_trading_days,
            "reason": f"{sym} reports earnings within ±2d",
        },
    )
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    after = _snapshot_paper_trades(conn)
    assert before == after
    assert any(a["action"] == "SKIP_EARNINGS_WEEK" for a in res["actions"])
    rows = _skip_rows(conn, gate="earnings_veto")
    assert len(rows) == 1


def test_gate_negative_sentiment_writes_skip_row(isolated_db, base_settings,
                                                   monkeypatch):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18, symbol="GDX")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {
        **base_settings,
        "veto_negative_sentiment": True,
        "negative_sentiment_threshold": 1,
        "negative_sentiment_window_hours": 24,
    }
    # Stub the negative-count helper to force a veto.
    monkeypatch.setattr(
        at, "_count_negative_news_for_symbol",
        lambda conn, sym, *, asof_dt, window_hours: 5,
    )
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    after = _snapshot_paper_trades(conn)
    assert before == after
    assert any(a["action"] == "SKIP_NEGATIVE_SENTIMENT" for a in res["actions"])
    rows = _skip_rows(conn, gate="negative_sentiment_veto")
    assert len(rows) == 1


def test_gate_paused_strategy_writes_skip_row(isolated_db, base_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18, symbol="GDX")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    # Pause the strategy.
    with conn:
        conn.execute(
            "INSERT INTO paused_strategies "
            "(strategy_id, reason, paused_at, expires_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("winner", "live-divergence", "2026-05-13",
             "2026-06-13", "divergence_checker"),
        )
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=base_settings)
    after = _snapshot_paper_trades(conn)
    assert before == after
    assert any(a["action"] == "SKIP_PAUSED_STRATEGY" for a in res["actions"])
    rows = _skip_rows(conn, gate="paused_strategy")
    assert len(rows) == 1


def test_gate_max_open_per_strategy_writes_skip_row(isolated_db, base_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18, symbol="GDX")
    # Seed 3 open positions on this strategy.
    for i, sym in enumerate(("A", "B", "C")):
        db.record_paper_trade(conn, {
            "alpaca_order_id": f"o{i}",
            "signal_id": None,
            "strategy_id": "winner",
            "symbol": sym, "side": "buy", "qty": 1,
            "order_type": "market", "fill_price": 50.0,
            "submitted_at": "2026-05-10T15:00:00+00:00",
            "filled_at": "2026-05-10T15:00:00+00:00",
            "status": "filled",
        })
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {
        **base_settings,
        "risk": {"max_open_per_strategy": 3},
    }
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    after = _snapshot_paper_trades(conn)
    assert before == after
    assert any(a["action"] == "SKIP_MAX_OPEN_PER_STRATEGY"
               for a in res["actions"])
    rows = _skip_rows(conn, gate="max_open_per_strategy")
    assert len(rows) == 1


def test_gate_ineligible_writes_skip_row(isolated_db, base_settings):
    # Strategy with negative edge → ineligible
    conn = _seed_outcomes("loser", [-1.0, 0.0] * 18)
    db.record_signal(conn, strategy_id="loser", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=base_settings)
    after = _snapshot_paper_trades(conn)
    assert before == after
    assert any(a["action"] == "SKIP_INELIGIBLE" for a in res["actions"])
    rows = _skip_rows(conn, gate="ineligible")
    assert len(rows) == 1


def test_gate_already_submitted_writes_skip_row(isolated_db, base_settings,
                                                  monkeypatch):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18, symbol="GDX")
    sig_id = db.record_signal(conn, strategy_id="winner", symbol="GDX",
                               bar_ts="2026-05-14",
                               signal_type="long_entry",
                               close=70.0, bar_interval="1d")
    # Pre-seed a buy for this signal so the dedupe gate fires.
    db.record_paper_trade(conn, {
        "alpaca_order_id": "prior-buy",
        "signal_id": sig_id,
        "strategy_id": "winner", "symbol": "GDX", "side": "buy",
        "qty": 1, "order_type": "market", "fill_price": 70.0,
        "submitted_at": "2026-05-13T15:00:00+00:00",
        "filled_at": "2026-05-13T15:00:00+00:00",
        "status": "filled",
    })
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=base_settings)
    after = _snapshot_paper_trades(conn)
    assert before == after, (
        "already_submitted gate must not modify paper_trades"
    )
    assert any(a["action"] == "SKIP_DUPLICATE" for a in res["actions"])
    rows = _skip_rows(conn, gate="already_submitted")
    assert len(rows) == 1


def test_gate_price_too_high_writes_skip_row(isolated_db, base_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18, symbol="GDX")
    # Price (5000) is way above max_position_usd (1000) → SKIP_PRICE
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=5000.0, bar_interval="1d")
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=base_settings)
    after = _snapshot_paper_trades(conn)
    assert before == after
    assert any(a["action"] == "SKIP_PRICE" for a in res["actions"])
    rows = _skip_rows(conn, gate="price_too_high")
    assert len(rows) == 1


def test_gate_no_open_position_skips_without_writing_row(isolated_db, base_settings):
    # F7 (audit 2026-06-03): a long_exit with no open position still returns
    # SKIP_NO_POSITION (decision unchanged) but must NOT persist a skip row —
    # being flat is the normal case and was bloating intraday_skips with
    # 187,814 noise rows.
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18, symbol="GDX")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_exit",
                     close=70.0, bar_interval="1d")
    before = _snapshot_paper_trades(conn)
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                              settings=base_settings)
    after = _snapshot_paper_trades(conn)
    assert before == after
    assert any(a["action"] == "SKIP_NO_POSITION" for a in res["actions"])
    rows = _skip_rows(conn, gate="no_open_position")
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# 4. Source attribution (daily vs intraday_15m)
# ---------------------------------------------------------------------------

def test_source_daily_for_1d_bar_interval(isolated_db, base_settings):
    conn = _seed_outcomes("loser", [-1.0, 0.0] * 18)
    db.record_signal(conn, strategy_id="loser", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    at.process_signals(conn, asof=date(2026, 5, 14), settings=base_settings)
    rows = _skip_rows(conn, gate="ineligible")
    assert rows[0]["source"] == "daily"


def test_source_intraday_for_15m_bar_interval(isolated_db, base_settings):
    conn = _seed_outcomes("loser", [-1.0, 0.0] * 18)
    db.record_signal(conn, strategy_id="loser", symbol="GDX",
                     bar_ts="2026-05-14T15:30:00",
                     signal_type="long_entry",
                     close=70.0, bar_interval="15m")
    at.process_signals(conn, asof=date(2026, 5, 14),
                       settings=base_settings, bar_interval="15m")
    rows = _skip_rows(conn, gate="ineligible")
    assert len(rows) == 1
    assert rows[0]["source"] == "intraday_15m"


# ---------------------------------------------------------------------------
# 5. Source helper unit tests
# ---------------------------------------------------------------------------

def test_skip_source_for_bar_interval_daily():
    assert at._skip_source_for_bar_interval("1d") == "daily"
    assert at._skip_source_for_bar_interval(None) == "daily"


def test_skip_source_for_bar_interval_intraday():
    assert at._skip_source_for_bar_interval("15m") == "intraday_15m"
    assert at._skip_source_for_bar_interval("5m") == "intraday_15m"
    assert at._skip_source_for_bar_interval("1h") == "intraday_15m"


# ---------------------------------------------------------------------------
# 6. Aggregate no-impact invariant — running a multi-gate scenario doesn't
#    modify paper_trades at all
# ---------------------------------------------------------------------------

def test_aggregate_no_impact_on_paper_trades(isolated_db, base_settings,
                                                monkeypatch):
    """Construct a signal that trips multiple gates in sequence; assert
    paper_trades is byte-identical before and after process_signals."""
    conn = _seed_outcomes("loser", [-1.0] * 30, symbol="GDX")
    db.record_signal(conn, strategy_id="loser", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    # Engage kill switch (first-line gate).
    monkeypatch.setattr(
        "monitoring.kill_switch.load_state",
        lambda: {"live_trading_halted": True, "reason": "manual",
                  "set_at": "2026-05-14"},
    )
    before = _snapshot_paper_trades(conn)
    at.process_signals(conn, asof=date(2026, 5, 14),
                       settings=base_settings)
    after = _snapshot_paper_trades(conn)
    assert before == after
    # And a kill_switch skip row was written.
    assert len(_skip_rows(conn, gate="kill_switch")) == 1
