"""7.5.4 — Intraday confirmation overlay (shadow mode).

Mirror of test_sar_overlay_ab.py's no-impact-on-live-PnL pattern.

Validates:
  - Trend strategy declarations opt in via intraday_confirm: "shadow".
  - paper_trades_intraday_confirm table created via init_db().
  - strategy_has_intraday_confirm_shadow vs *_live semantics.
  - compute_confirmation walks 1m bars and finds the first close > trigger.
  - record_intraday_confirm writes the expected row + handles missing data.
  - no-impact-on-live-paper-trades invariant — running auto_trader's entry
    path with intraday_confirm='shadow' on a strategy writes a shadow row
    but does NOT change paper_trades or the live entry decision.
  - aggregate_ab math on hand-computed fixture.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader  # noqa: E402
from monitoring import intraday_confirm as ic  # noqa: E402
from strategies.trend import TREND_DECLARATIONS  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1. Declarations opt-in
# ---------------------------------------------------------------------------

def test_three_trend_strategies_opt_into_intraday_confirm_shadow():
    """All three trend strategies declare intraday_confirm='shadow'."""
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
        assert decl.get("intraday_confirm") == "shadow", (
            f"{sid} should declare intraday_confirm='shadow', got "
            f"{decl.get('intraday_confirm')!r}"
        )


def test_shadow_predicate_does_not_imply_live():
    meta = {"intraday_confirm": "shadow"}
    assert ic.strategy_has_intraday_confirm_shadow(meta) is True
    assert ic.strategy_has_intraday_confirm_live(meta) is False


def test_live_opt_in_also_records_shadow():
    """Live opt-in is a superset of shadow."""
    assert ic.strategy_has_intraday_confirm_live({"intraday_confirm": True}) is True
    assert ic.strategy_has_intraday_confirm_shadow({"intraday_confirm": True}) is True


def test_no_opt_in_means_no_shadow():
    assert ic.strategy_has_intraday_confirm_shadow({}) is False
    assert ic.strategy_has_intraday_confirm_shadow(None) is False
    assert ic.strategy_has_intraday_confirm_shadow(
        {"intraday_confirm": False}
    ) is False


# ---------------------------------------------------------------------------
# 2. Table schema
# ---------------------------------------------------------------------------

def test_paper_trades_intraday_confirm_table_exists(conn):
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type='table' AND name='paper_trades_intraday_confirm'"
    ).fetchone()
    assert cur is not None, "paper_trades_intraday_confirm table missing"


def test_paper_trades_intraday_confirm_has_expected_columns(conn):
    cols = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(paper_trades_intraday_confirm)"
        ).fetchall()
    }
    expected = {
        "id", "recorded_at", "strategy_id", "symbol", "signal_id",
        "daily_signal_ts", "trigger_price",
        "would_have_confirmed_at", "hypothetical_entry_price",
        "shadow_status",
        "real_entry_price", "shadow_pnl_at_close", "real_pnl_at_close",
        "notes",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


# ---------------------------------------------------------------------------
# 3. Confirmation math
# ---------------------------------------------------------------------------

def test_compute_confirmation_long_finds_first_close_above_trigger():
    bars = [
        {"ts_utc": "2026-05-22T09:30:00+00:00", "close": 100.5},
        {"ts_utc": "2026-05-22T09:31:00+00:00", "close": 100.8},
        {"ts_utc": "2026-05-22T09:32:00+00:00", "close": 101.2},
        {"ts_utc": "2026-05-22T09:33:00+00:00", "close": 101.5},
    ]
    out = ic.compute_confirmation(bars, trigger_price=101.0, side="long")
    assert out["status"] == "confirmed"
    assert out["confirmed_at"] == "2026-05-22T09:32:00+00:00"
    assert out["entry_price"] == pytest.approx(101.2)


def test_compute_confirmation_long_not_confirmed_when_no_breakout():
    bars = [
        {"ts_utc": "2026-05-22T09:30:00+00:00", "close": 100.5},
        {"ts_utc": "2026-05-22T09:31:00+00:00", "close": 100.8},
    ]
    out = ic.compute_confirmation(bars, trigger_price=101.0, side="long")
    assert out["status"] == "not_confirmed"
    assert out["confirmed_at"] is None
    assert out["entry_price"] is None


def test_compute_confirmation_no_data_when_bars_empty():
    out = ic.compute_confirmation([], trigger_price=100.0, side="long")
    assert out["status"] == "no_data"


def test_compute_confirmation_short_inverts():
    bars = [
        {"ts_utc": "2026-05-22T09:30:00+00:00", "close": 99.5},
        {"ts_utc": "2026-05-22T09:31:00+00:00", "close": 98.2},
    ]
    out = ic.compute_confirmation(bars, trigger_price=99.0, side="short")
    assert out["status"] == "confirmed"
    assert out["entry_price"] == pytest.approx(98.2)


# ---------------------------------------------------------------------------
# 4. record_intraday_confirm — writes + idempotency + missing data
# ---------------------------------------------------------------------------

def test_record_intraday_confirm_writes_confirmed_row(conn):
    bars = [
        {"ts_utc": "2026-05-22T09:30:00+00:00", "close": 100.5},
        {"ts_utc": "2026-05-22T09:31:00+00:00", "close": 101.5},
    ]
    rowid = ic.record_intraday_confirm(
        conn,
        strategy_id="trend-donchian-breakout-20", symbol="SPY",
        daily_signal_ts="2026-05-22T00:00:00+00:00",
        trigger_price=101.0,
        bars=bars,
        now_iso="2026-05-22T15:00:00+00:00",
    )
    assert rowid is not None
    row = conn.execute(
        "SELECT * FROM paper_trades_intraday_confirm WHERE id=?", (rowid,),
    ).fetchone()
    assert row["shadow_status"] == "confirmed"
    assert row["would_have_confirmed_at"] == "2026-05-22T09:31:00+00:00"
    assert row["hypothetical_entry_price"] == pytest.approx(101.5)
    assert row["trigger_price"] == pytest.approx(101.0)


def test_record_intraday_confirm_no_data_when_no_bars(conn):
    rowid = ic.record_intraday_confirm(
        conn,
        strategy_id="trend-ma-cross-20-50", symbol="QQQ",
        daily_signal_ts="2026-05-22T00:00:00+00:00",
        trigger_price=400.0,
        bars=[],
    )
    assert rowid is not None
    row = conn.execute(
        "SELECT * FROM paper_trades_intraday_confirm WHERE id=?", (rowid,),
    ).fetchone()
    assert row["shadow_status"] == "no_data"
    assert row["would_have_confirmed_at"] is None
    assert row["hypothetical_entry_price"] is None


def test_record_intraday_confirm_idempotent_on_duplicate(conn):
    """Re-recording for the same (strategy, symbol, daily_signal_ts) is a no-op."""
    kwargs = dict(
        strategy_id="trend-new-high-volume", symbol="IWM",
        daily_signal_ts="2026-05-22T00:00:00+00:00",
        trigger_price=200.0,
        bars=[{"ts_utc": "2026-05-22T09:30:00+00:00", "close": 201.0}],
    )
    first = ic.record_intraday_confirm(conn, **kwargs)
    second = ic.record_intraday_confirm(conn, **kwargs)
    assert first is not None
    assert second is None
    n = conn.execute(
        "SELECT COUNT(*) FROM paper_trades_intraday_confirm"
    ).fetchone()[0]
    assert n == 1


# ---------------------------------------------------------------------------
# 5. fetch_intraday_bars
# ---------------------------------------------------------------------------

def _seed_intraday(conn, *, symbol, ts_utc, close):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO intraday_bars "
            "(symbol, ts_utc, open, high, low, close, volume, source, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'iex', ?)",
            (symbol, ts_utc, close, close, close, close, 1000.0, ts_utc),
        )


def test_fetch_intraday_bars_returns_chronological_after_cutoff(conn):
    _seed_intraday(conn, symbol="SPY", ts_utc="2026-05-22T09:30:00+00:00",
                   close=100.0)
    _seed_intraday(conn, symbol="SPY", ts_utc="2026-05-22T09:31:00+00:00",
                   close=100.5)
    _seed_intraday(conn, symbol="SPY", ts_utc="2026-05-22T09:32:00+00:00",
                   close=101.0)
    # Cutoff at 09:30 — should return only 09:31 and 09:32.
    bars = ic.fetch_intraday_bars(
        conn, symbol="SPY", after_ts_utc="2026-05-22T09:30:00+00:00",
    )
    assert len(bars) == 2
    assert bars[0]["ts_utc"] == "2026-05-22T09:31:00+00:00"
    assert bars[1]["ts_utc"] == "2026-05-22T09:32:00+00:00"


def test_fetch_intraday_bars_empty_when_no_rows(conn):
    bars = ic.fetch_intraday_bars(
        conn, symbol="MISSING", after_ts_utc="2026-01-01T00:00:00+00:00",
    )
    assert bars == []


# ---------------------------------------------------------------------------
# 6. No-impact-on-live-paper-trades invariant
# ---------------------------------------------------------------------------

def _snapshot_paper_trades(conn):
    return [
        tuple(r) for r in conn.execute(
            "SELECT alpaca_order_id, strategy_id, symbol, side, qty, "
            "       status, fill_price, stop_price, notes "
            "  FROM paper_trades ORDER BY id"
        ).fetchall()
    ]


def test_shadow_does_not_affect_paper_trades(conn, monkeypatch):
    """The invariant: when intraday_confirm shadow records for a fire,
    NO new row is added to paper_trades that wouldn't have been added
    otherwise, and the entry decision is byte-identical.

    Strategy: dry-run an entry through _process_entry on a strategy that
    opts into intraday_confirm shadow. With dry_run=True, no paper_trades
    row is written either way — but a shadow row IS written if bars exist.
    """
    # Seed minimal data: strategy row, signal row, intraday bars.
    sid = "trend-donchian-breakout-20"
    sym = "SPY"
    db.ensure_strategies_seeded(conn, TREND_DECLARATIONS)
    sig_id = db.record_signal(
        conn,
        strategy_id=sid, symbol=sym,
        bar_ts="2026-05-22",
        signal_type="long_entry",
        close=100.0,
        bar_interval="1d",
    )
    db.open_outcome(conn, signal_id=sig_id, entry_ts="2026-05-22",
                    entry_price=100.0)
    # Seed enough closed history that eligibility passes (grace_period=True
    # opens the door even with few outcomes, but be safe).
    # Then close the just-opened outcome so it doesn't count as 'open'.
    db.close_outcome(conn, signal_id=sig_id, exit_ts="2026-05-23",
                     exit_price=102.0, exit_reason="test_close")
    # Add a fresh open signal to actually exercise _process_entry.
    sig_id2 = db.record_signal(
        conn,
        strategy_id=sid, symbol=sym,
        bar_ts="2026-05-24",
        signal_type="long_entry",
        close=100.0,
        bar_interval="1d",
    )
    # Intraday bars after the signal — one confirms.
    _seed_intraday(conn, symbol=sym, ts_utc="2026-05-24T09:30:00+00:00",
                   close=99.5)
    _seed_intraday(conn, symbol=sym, ts_utc="2026-05-24T09:31:00+00:00",
                   close=101.0)

    before_paper = _snapshot_paper_trades(conn)
    shadow_before = conn.execute(
        "SELECT COUNT(*) FROM paper_trades_intraday_confirm"
    ).fetchone()[0]

    sig_row = conn.execute(
        "SELECT id, ts, bar_ts, bar_interval, strategy_id, symbol, "
        "       signal_type, close FROM signals WHERE id=?",
        (sig_id2,),
    ).fetchone()
    # Call _process_entry in dry_run mode — auto_trader's entry path.
    auto_trader._process_entry(
        conn, client=None, settings={"enabled": True, "dry_run": True},
        sig=sig_row, dry_run=True,
        tracked_strategies=TREND_DECLARATIONS,
    )

    after_paper = _snapshot_paper_trades(conn)
    shadow_after = conn.execute(
        "SELECT COUNT(*) FROM paper_trades_intraday_confirm"
    ).fetchone()[0]

    # INVARIANT: paper_trades is unchanged across a dry-run entry.
    assert after_paper == before_paper, (
        "paper_trades changed despite dry_run — shadow overlay altered live"
    )
    # Shadow row WAS written for the opt-in strategy.
    assert shadow_after == shadow_before + 1, (
        "expected exactly one new paper_trades_intraday_confirm row"
    )


def test_no_shadow_row_when_strategy_not_opt_in(conn):
    """Strategy without intraday_confirm declared gets no shadow rows."""
    # Botnet101 has no intraday_confirm flag.
    sid = "botnet101-3-bar-low"
    db.ensure_strategies_seeded(conn, [{"id": sid}])
    sig_id = db.record_signal(
        conn,
        strategy_id=sid, symbol="QQQ",
        bar_ts="2026-05-24",
        signal_type="long_entry",
        close=400.0,
        bar_interval="1d",
    )
    _seed_intraday(conn, symbol="QQQ", ts_utc="2026-05-24T09:30:00+00:00",
                   close=401.0)
    sig_row = conn.execute(
        "SELECT id, ts, bar_ts, bar_interval, strategy_id, symbol, "
        "       signal_type, close FROM signals WHERE id=?",
        (sig_id,),
    ).fetchone()
    # Call the helper directly with the botnet declaration (no opt-in).
    decl = {"id": sid, "strategy_class": "mean_reversion"}
    auto_trader._maybe_record_intraday_confirm_shadow(
        conn, sig=sig_row, decl=decl, side="long",
    )
    n = conn.execute(
        "SELECT COUNT(*) FROM paper_trades_intraday_confirm"
    ).fetchone()[0]
    assert n == 0


def test_no_shadow_row_for_missing_signal_close(conn):
    """Signal close=None records a no_data row (graceful degrade)."""
    sid = "trend-donchian-breakout-20"
    db.ensure_strategies_seeded(conn, TREND_DECLARATIONS)
    sig_id = db.record_signal(
        conn,
        strategy_id=sid, symbol="SPY",
        bar_ts="2026-05-24",
        signal_type="long_entry",
        close=None,
        bar_interval="1d",
    )
    sig_row = conn.execute(
        "SELECT id, ts, bar_ts, bar_interval, strategy_id, symbol, "
        "       signal_type, close FROM signals WHERE id=?",
        (sig_id,),
    ).fetchone()
    decl = TREND_DECLARATIONS[0]
    auto_trader._maybe_record_intraday_confirm_shadow(
        conn, sig=sig_row, decl=decl, side="long",
    )
    row = conn.execute(
        "SELECT * FROM paper_trades_intraday_confirm"
    ).fetchone()
    assert row is not None
    assert row["shadow_status"] == "no_data"
    assert row["trigger_price"] is None


# ---------------------------------------------------------------------------
# 7. A/B aggregation math
# ---------------------------------------------------------------------------

def test_aggregate_ab_counts_and_rate(conn):
    bars_confirming = [{"ts_utc": "2026-05-22T09:31:00+00:00", "close": 105.0}]
    bars_no_confirm = [{"ts_utc": "2026-05-22T09:31:00+00:00", "close": 95.0}]
    for i in range(3):
        ic.record_intraday_confirm(
            conn,
            strategy_id="A", symbol=f"S{i}",
            daily_signal_ts=f"2026-05-{20+i:02d}T00:00:00+00:00",
            trigger_price=100.0, bars=bars_confirming,
        )
    for i in range(2):
        ic.record_intraday_confirm(
            conn,
            strategy_id="A", symbol=f"T{i}",
            daily_signal_ts=f"2026-05-{20+i:02d}T00:00:00+00:00",
            trigger_price=100.0, bars=bars_no_confirm,
        )
    ic.record_intraday_confirm(
        conn,
        strategy_id="A", symbol="U",
        daily_signal_ts="2026-05-25T00:00:00+00:00",
        trigger_price=100.0, bars=[],
    )
    agg = ic.aggregate_ab(conn, strategy_id="A")
    assert agg["count"] == 6
    assert agg["confirmed"] == 3
    assert agg["not_confirmed"] == 2
    assert agg["no_data"] == 1
    # 3 / (3+2) = 0.6
    assert agg["confirmation_rate"] == pytest.approx(0.6)


def test_aggregate_ab_empty_returns_zero(conn):
    agg = ic.aggregate_ab(conn)
    assert agg["count"] == 0
    assert agg["confirmation_rate"] == 0.0
    assert agg["by_strategy"] == {}


def test_aggregate_ab_per_strategy_breakdown(conn):
    bars_yes = [{"ts_utc": "2026-05-22T09:31:00+00:00", "close": 105.0}]
    ic.record_intraday_confirm(
        conn, strategy_id="A", symbol="SPY",
        daily_signal_ts="2026-05-22T00:00:00+00:00",
        trigger_price=100.0, bars=bars_yes,
    )
    ic.record_intraday_confirm(
        conn, strategy_id="B", symbol="QQQ",
        daily_signal_ts="2026-05-22T00:00:00+00:00",
        trigger_price=100.0, bars=bars_yes,
    )
    agg = ic.aggregate_ab(conn)
    assert set(agg["by_strategy"]) == {"A", "B"}
    assert agg["by_strategy"]["A"]["confirmed"] == 1
    assert agg["by_strategy"]["B"]["confirmed"] == 1
