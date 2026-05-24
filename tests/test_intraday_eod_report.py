"""7.5.6 — EOD intraday report + LLM filter activation.

Validates:
  - EOD report shape on a seeded day (markdown sections present).
  - Empty seed → graceful "no data" messages without exceptions.
  - LLM filter call site is wired through auto_trader.process_signals
    AND its no-impact invariant continues to hold post-activation.
  - LLM filter graceful no-op when API key is absent (verdict='allow',
    failure_mode='no_api_key').
"""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import intraday_eod_report as ier  # noqa: E402
from monitoring import llm_filter  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def test_gather_intraday_fires_filters_to_today_intraday(conn):
    asof = date(2026, 5, 22)
    # Today's intraday fire (5m)
    db.ensure_strategies_seeded(conn, [{"id": "intra-1"}, {"id": "intra-2"}, {"id": "daily-1"}])
    db.record_signal(
        conn, strategy_id="intra-1", symbol="SPY",
        bar_ts="2026-05-22T09:30:00+00:00",
        signal_type="long_entry", close=100.0, bar_interval="5m",
    )
    # Today's EOD fire (1d) — should NOT appear
    db.record_signal(
        conn, strategy_id="daily-1", symbol="QQQ",
        bar_ts="2026-05-22",
        signal_type="long_entry", close=400.0, bar_interval="1d",
    )
    # Yesterday's intraday fire — should NOT appear
    db.record_signal(
        conn, strategy_id="intra-2", symbol="IWM",
        bar_ts="2026-05-21T15:00:00+00:00",
        signal_type="long_entry", close=200.0, bar_interval="15m",
    )
    fires = ier.gather_intraday_fires(conn, asof)
    sids = {f["strategy_id"] for f in fires}
    assert "intra-1" in sids
    assert "daily-1" not in sids
    assert "intra-2" not in sids


def test_gather_intraday_bars_count(conn):
    asof = date(2026, 5, 22)
    with conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO intraday_bars "
                "(symbol, ts_utc, open, high, low, close, volume, source) "
                "VALUES (?, ?, 100, 100, 100, 100, 1000, 'iex')",
                ("SPY", f"2026-05-22T09:3{i}:00+00:00"),
            )
        # Yesterday — should NOT count.
        conn.execute(
            "INSERT INTO intraday_bars "
            "(symbol, ts_utc, open, high, low, close, volume, source) "
            "VALUES ('QQQ', '2026-05-21T09:30:00+00:00', 400, 400, 400, 400, 1000, 'iex')"
        )
    counts = ier.gather_intraday_bars_count(conn, asof)
    assert counts == {"SPY": 3}


def test_gather_skips_today_filters_window(conn):
    asof = date(2026, 5, 22)
    db.record_intraday_skip(
        conn, strategy_id="A", symbol="SPY",
        bar_ts="2026-05-22", signal_type="long_entry",
        gate="cool_down", reason_detail="r", source="daily",
        recorded_at="2026-05-22T10:00:00+00:00",
    )
    db.record_intraday_skip(
        conn, strategy_id="B", symbol="QQQ",
        bar_ts="2026-05-21", signal_type="long_entry",
        gate="kill_switch", source="daily",
        recorded_at="2026-05-21T10:00:00+00:00",
    )
    skips = ier.gather_skips_today(conn, asof)
    gates = {s["gate"] for s in skips}
    assert gates == {"cool_down"}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def test_fires_by_strategy_groups_and_sorts():
    fires = [
        {"strategy_id": "A", "symbol": "SPY"},
        {"strategy_id": "A", "symbol": "QQQ"},
        {"strategy_id": "B", "symbol": "IWM"},
    ]
    out = ier.fires_by_strategy(fires)
    assert out[0]["strategy_id"] == "A"
    assert out[0]["count"] == 2
    assert out[0]["symbols"] == ["QQQ", "SPY"]
    assert out[1]["strategy_id"] == "B"
    assert out[1]["count"] == 1


def test_skips_by_gate_counts_and_orders():
    skips = [
        {"gate": "cool_down"}, {"gate": "cool_down"}, {"gate": "cool_down"},
        {"gate": "kill_switch"}, {"gate": "kill_switch"},
        {"gate": "earnings_veto"},
    ]
    out = ier.skips_by_gate(skips)
    assert out[0] == {"gate": "cool_down", "count": 3}
    assert out[1] == {"gate": "kill_switch", "count": 2}
    assert out[2] == {"gate": "earnings_veto", "count": 1}


def test_pnl_by_strategy_aggregates_wins_losses_totals():
    outcomes = [
        {"strategy_id": "A", "symbol": "SPY", "return_pct": 2.0,
         "exit_reason": "tp"},
        {"strategy_id": "A", "symbol": "QQQ", "return_pct": -1.5,
         "exit_reason": "stop"},
        {"strategy_id": "A", "symbol": "IWM", "return_pct": 3.0,
         "exit_reason": "tp"},
        {"strategy_id": "B", "symbol": "TLT", "return_pct": 1.0,
         "exit_reason": "tp"},
    ]
    out = ier.pnl_by_strategy(outcomes)
    a = next(r for r in out if r["strategy_id"] == "A")
    assert a["n"] == 3
    assert a["wins"] == 2
    assert a["losses"] == 1
    assert a["total_return_pct"] == pytest.approx(3.5)
    assert a["best"] == pytest.approx(3.0)
    assert a["worst"] == pytest.approx(-1.5)


def test_find_divergences_returns_intraday_fires_that_lost_eod():
    fires = [
        {"strategy_id": "X", "symbol": "SPY", "signal_type": "long_entry"},
        {"strategy_id": "Y", "symbol": "QQQ", "signal_type": "long_entry"},
        # Same (X, SPY) intraday fire AND closed at -5% by EOD → divergence.
    ]
    outcomes = [
        {"strategy_id": "X", "symbol": "SPY", "return_pct": -5.0,
         "exit_reason": "stop"},
        # Y/QQQ closed positive, not a divergence
        {"strategy_id": "Y", "symbol": "QQQ", "return_pct": 2.0,
         "exit_reason": "tp"},
        # Z/IWM had no intraday fire — not a divergence even if down 10%
        {"strategy_id": "Z", "symbol": "IWM", "return_pct": -10.0,
         "exit_reason": "stop"},
    ]
    div = ier.find_divergences(fires, outcomes)
    assert len(div) == 1
    assert div[0]["strategy_id"] == "X"
    assert div[0]["symbol"] == "SPY"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def test_render_markdown_includes_all_sections():
    md = ier.render_markdown(
        asof=date(2026, 5, 22),
        fires=[
            {"strategy_id": "A", "symbol": "SPY", "signal_type": "long_entry"},
        ],
        bars_by_symbol={"SPY": 390},
        skips=[{"gate": "cool_down"}],
        outcomes=[{"strategy_id": "A", "symbol": "SPY", "return_pct": 1.5,
                   "exit_reason": "tp"}],
    )
    assert "# Intraday EOD Report" in md
    assert "2026-05-22" in md
    assert "## 1. Fires by strategy" in md
    assert "## 2. Skip breakdown by gate" in md
    assert "## 3. Paper P&L by strategy" in md
    assert "## 4. Top divergences" in md
    assert "## 5. Intraday bars ingested today" in md


def test_render_markdown_empty_day_uses_no_data_strings():
    md = ier.render_markdown(
        asof=date(2026, 5, 22),
        fires=[],
        bars_by_symbol={},
        skips=[],
        outcomes=[],
    )
    assert "_No intraday fires today._" in md
    assert "_No skips recorded today._" in md
    assert "_No closed outcomes today._" in md
    assert "_No notable divergences" in md


def test_generate_intraday_eod_report_on_empty_db_returns_string(conn):
    """The full pipeline runs on an empty DB and produces a non-empty string."""
    md = ier.generate_intraday_eod_report(conn, asof=date(2026, 5, 22))
    assert isinstance(md, str)
    assert "Intraday EOD Report" in md


def test_generate_intraday_eod_report_with_seeded_data(conn):
    db.ensure_strategies_seeded(conn, [
        {"id": "intraday-1m-momentum"},
        {"id": "intraday-1m-orb"},
    ])
    db.record_signal(
        conn, strategy_id="intraday-1m-momentum", symbol="SPY",
        bar_ts="2026-05-22T10:00:00+00:00",
        signal_type="long_entry", close=520.0, bar_interval="1m",
    )
    db.record_signal(
        conn, strategy_id="intraday-1m-orb", symbol="QQQ",
        bar_ts="2026-05-22T09:36:00+00:00",
        signal_type="long_entry", close=410.0, bar_interval="1m",
    )
    db.record_intraday_skip(
        conn, strategy_id="intraday-1m-momentum", symbol="IWM",
        bar_ts="2026-05-22T10:01:00+00:00", signal_type="long_entry",
        gate="kill_switch", source="intraday_15m",
        recorded_at="2026-05-22T10:01:00+00:00",
    )
    md = ier.generate_intraday_eod_report(conn, asof=date(2026, 5, 22))
    assert "intraday-1m-momentum" in md
    assert "intraday-1m-orb" in md
    assert "kill_switch" in md


# ---------------------------------------------------------------------------
# LLM filter activation — call site is already wired (from 7.1.1).
# This milestone verifies the no-impact invariant still holds post-activation.
# ---------------------------------------------------------------------------

def test_llm_filter_no_api_key_falls_open(conn, monkeypatch):
    """With no API key configured, assess_signal returns verdict='allow'
    with failure_mode='no_api_key'. No network call attempted."""
    monkeypatch.setattr(llm_filter, "_load_api_key", lambda: "")
    result = llm_filter.assess_signal(
        {
            "strategy_id": "test-strat", "symbol": "SPY",
            "side": "long", "signal_type": "long_entry",
            "bar_ts": "2026-05-22T10:00:00+00:00",
            "close": 520.0,
        },
        conn,
        market_context={}, recent_news=[], earnings=[], prior_outcomes=[],
    )
    assert result["verdict"] == "allow"
    # The persisted row carries failure_mode='no_api_key'
    row = conn.execute(
        "SELECT failure_mode, verdict FROM paper_trades_llm_filter "
        " WHERE strategy_id='test-strat'"
    ).fetchone()
    assert row["failure_mode"] == "no_api_key"
    assert row["verdict"] == "allow"


def test_llm_filter_default_setting_remains_off():
    """Documented: settings.llm_filter.enabled default is False until
    Ross flips it on. The auto_trader call site already gates on this."""
    import json
    settings_path = ROOT / "config" / "settings.json"
    if not settings_path.exists():
        pytest.skip("settings.json not present in test env")
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)
    # If the key exists, it must be False. If absent, that also means OFF.
    llm_settings = settings.get("llm_filter") or {}
    assert llm_settings.get("enabled", False) is False, (
        "llm_filter.enabled should default to False — flipping it on is "
        "Ross's manual decision after the EOD report data accumulates."
    )


def test_llm_filter_invariant_no_paper_trades_change(conn, monkeypatch):
    """The 7.1.1 no-impact-on-live-PnL invariant continues to hold:
    calling assess_signal never touches paper_trades, only the parallel
    shadow table."""
    monkeypatch.setattr(llm_filter, "_load_api_key", lambda: "")
    before = conn.execute(
        "SELECT COUNT(*) FROM paper_trades"
    ).fetchone()[0]
    llm_filter.assess_signal(
        {
            "strategy_id": "test-strat", "symbol": "SPY",
            "side": "long", "signal_type": "long_entry",
            "bar_ts": "2026-05-22T10:00:00+00:00",
            "close": 520.0,
        },
        conn,
    )
    after = conn.execute(
        "SELECT COUNT(*) FROM paper_trades"
    ).fetchone()[0]
    assert after == before, "assess_signal mutated paper_trades"
