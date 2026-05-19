"""
test_dashboard_scanner_activity.py — milestone 5.5.6.1.

Covers the /research scanner-activity card:
  - /api/state surfaces a scanner_activity key with rows + summary
  - empty DB → rows=[] and a well-formed summary
  - only signals whose extra_json carries source=trend_scanner are surfaced
  - non-scanner signals (regular EOD or intraday fires) stay out
  - signals from a different day stay out
  - long_exit signals stay out (entries only — card surfaces entry decisions)
  - SUBMITTED action when a non-rejected paper_trade exists for the signal
  - SKIP_INELIGIBLE action when strategy has zero closed outcomes (default
    auto_trade settings require min_outcomes)
  - SKIP_CAPACITY action when eligibility passes but the daily cap is
    already hit by other paper trades
  - PENDING action when eligible, not capped, not yet submitted
  - rows sorted by score DESC (signal_ranker contract)
  - score_breakdown surfaces the four multipliers
  - card markup + renderer present in research.html
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    # Force a deterministic auto_trade settings read so SKIP_CAPACITY
    # tests can rely on a known cap value.
    monkeypatch.setattr(srv, "_read_auto_trade_settings",
                         lambda: {"max_new_entries_per_day": 1,
                                  "min_outcomes": 30,
                                  "min_mean_ret_pct": 0.0,
                                  "min_sharpe_ish": 0.10})
    yield test_db


@pytest.fixture()
def client(isolated_db):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def _seed_strategy(sid="trend-strat"):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    return conn


def _record_scanner_fire(conn, *, sid, symbol, close, bar_ts=None):
    """Insert a wide-universe trend-scanner signal with the canonical
    extra_json shape stamped by `monitoring.trend_scanner`."""
    bar_ts = bar_ts or date.today().isoformat()
    return db.record_signal(
        conn, strategy_id=sid, symbol=symbol,
        bar_ts=bar_ts, signal_type="long_entry",
        close=close, bar_interval="1d",
        extra={"source": "trend_scanner", "wide_universe": True},
    )


def _insert_paper_trade(conn, *, sig_id, sid, symbol, side, order_id,
                         submitted_at, status="filled"):
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " submitted_at, filled_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, sig_id, sid, symbol, side, 1.0,
         submitted_at, submitted_at, status),
    )
    conn.commit()


# ---------------- API shape ----------------

def test_state_includes_scanner_activity_key(client):
    rv = client.get("/api/state")
    s = rv.get_json()
    assert "scanner_activity" in s
    sa = s["scanner_activity"]
    assert "rows" in sa and sa["rows"] == []
    assert "summary" in sa
    assert sa["summary"]["n_fires"] == 0
    assert sa["summary"]["by_action"] == {}


def test_only_scanner_sourced_signals_surface(client, isolated_db):
    today = date.today().isoformat()
    conn = _seed_strategy("trend-strat")
    _record_scanner_fire(conn, sid="trend-strat", symbol="AAPL", close=180.0)
    # Regular EOD signal — no scanner source → excluded
    db.record_signal(conn, strategy_id="trend-strat", symbol="MSFT",
                     bar_ts=today, signal_type="long_entry", close=300.0,
                     bar_interval="1d")
    # Different-day scanner fire → excluded
    db.record_signal(conn, strategy_id="trend-strat", symbol="GOOG",
                     bar_ts="2024-01-15", signal_type="long_entry",
                     close=100.0, bar_interval="1d",
                     extra={"source": "trend_scanner",
                            "wide_universe": True})
    # long_exit scanner-tagged signal → excluded (card surfaces entries)
    db.record_signal(conn, strategy_id="trend-strat", symbol="NVDA",
                     bar_ts=today, signal_type="long_exit", close=500.0,
                     bar_interval="1d",
                     extra={"source": "trend_scanner",
                            "wide_universe": True})
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["scanner_activity"]["rows"]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"


def test_action_submitted_when_paper_trade_exists(client, isolated_db):
    today = date.today().isoformat()
    conn = _seed_strategy("trend-strat")
    sig_id = _record_scanner_fire(conn, sid="trend-strat", symbol="AAPL",
                                   close=180.0)
    _insert_paper_trade(conn, sig_id=sig_id, sid="trend-strat",
                         symbol="AAPL", side="buy", order_id="o-1",
                         submitted_at=f"{today}T16:30:00")
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["scanner_activity"]["rows"]
    assert len(rows) == 1
    assert rows[0]["action"] == "SUBMITTED"


def test_action_submitted_ignores_rejected_paper_trade(client, isolated_db):
    today = date.today().isoformat()
    conn = _seed_strategy("trend-strat")
    sig_id = _record_scanner_fire(conn, sid="trend-strat", symbol="AAPL",
                                   close=180.0)
    _insert_paper_trade(conn, sig_id=sig_id, sid="trend-strat",
                         symbol="AAPL", side="buy", order_id="o-rej",
                         submitted_at=f"{today}T16:30:00",
                         status="rejected")
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["scanner_activity"]["rows"]
    # Rejected paper trade shouldn't mark SUBMITTED — without outcomes
    # the strategy is ineligible.
    assert rows[0]["action"] == "SKIP_INELIGIBLE"


def test_action_skip_ineligible_when_no_closed_outcomes(client, isolated_db):
    conn = _seed_strategy("trend-strat")
    _record_scanner_fire(conn, sid="trend-strat", symbol="AAPL", close=180.0)
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["scanner_activity"]["rows"]
    # Default settings require min_outcomes=30 closed trades — no
    # outcomes seeded, so eligibility fails.
    assert rows[0]["action"] == "SKIP_INELIGIBLE"


def _seed_strong_strategy_outcomes(conn, sid, n=35):
    """Seed enough closed outcomes (avg +1%, low stdev) that
    _is_eligible returns True under the default thresholds."""
    from datetime import date as _date, timedelta as _td
    base = _date(2024, 1, 1)
    for i in range(n):
        d = (base + _td(days=i)).isoformat()
        s_id = db.record_signal(
            conn, strategy_id=sid, symbol="SEED",
            bar_ts=d,
            signal_type="long_entry", close=100.0, bar_interval="1d",
            ts=f"{d}T10:00:00",
        )
        assert s_id is not None, f"unexpected dupe at {d}"
        db.open_outcome(conn, signal_id=s_id,
                        entry_ts=d, entry_price=100.0)
        # Alternate +2% / +0.5% — mean positive, low stdev → high sharpe
        ret = 2.0 if i % 2 == 0 else 0.5
        exit_d = (base + _td(days=i + 30)).isoformat()
        db.close_outcome(conn, signal_id=s_id,
                          exit_ts=exit_d,
                          exit_price=100.0 * (1 + ret / 100),
                          exit_reason="long_exit_signal", bars_held=4)


def test_action_skip_capacity_when_cap_exhausted(client, isolated_db):
    """When cap=1 and someone else already filled today, an
    eligible unsubmitted scanner fire reports SKIP_CAPACITY."""
    today = date.today().isoformat()
    conn = _seed_strategy("trend-strat")
    _seed_strong_strategy_outcomes(conn, "trend-strat")
    # Today's cap is consumed by an unrelated paper trade for a
    # different signal — fill on a regular EOD signal.
    other_sig = db.record_signal(
        conn, strategy_id="trend-strat", symbol="OTHER",
        bar_ts=today, signal_type="long_entry", close=50.0,
        bar_interval="1d",
    )
    _insert_paper_trade(conn, sig_id=other_sig, sid="trend-strat",
                         symbol="OTHER", side="buy", order_id="o-other",
                         submitted_at=f"{today}T16:00:00")
    # Now the scanner fire that nobody submitted → SKIP_CAPACITY.
    _record_scanner_fire(conn, sid="trend-strat", symbol="AAPL",
                          close=180.0)
    conn.close()
    rv = client.get("/api/state")
    sa = rv.get_json()["scanner_activity"]
    rows = sa["rows"]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["action"] == "SKIP_CAPACITY"
    assert sa["summary"]["max_new_entries_per_day"] == 1


def test_action_pending_when_eligible_and_cap_open(client, isolated_db,
                                                     monkeypatch):
    """No cap (set to 0) + eligible + not submitted → PENDING."""
    monkeypatch.setattr(srv, "_read_auto_trade_settings",
                         lambda: {"max_new_entries_per_day": 0,
                                  "min_outcomes": 30,
                                  "min_mean_ret_pct": 0.0,
                                  "min_sharpe_ish": 0.10})
    conn = _seed_strategy("trend-strat")
    _seed_strong_strategy_outcomes(conn, "trend-strat")
    _record_scanner_fire(conn, sid="trend-strat", symbol="AAPL",
                          close=180.0)
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["scanner_activity"]["rows"]
    assert rows[0]["action"] == "PENDING"


def test_rows_sorted_by_score_desc(client, isolated_db):
    """Two fires with different liquidity tiers — higher dollar-volume
    symbol should rank first."""
    conn = _seed_strategy("trend-strat")
    _record_scanner_fire(conn, sid="trend-strat", symbol="AAA", close=10.0)
    _record_scanner_fire(conn, sid="trend-strat", symbol="BBB", close=10.0)
    # AAA: high liquidity ($600M) → ×1.2 liquidity bump
    # BBB: low liquidity ($50M) → ×1.0
    today = date.today().isoformat()
    db.upsert_liquidity_snapshot(
        conn, symbol="AAA", as_of_date=today,
        avg_dollar_volume_20d=600_000_000.0, last_close=600.0,
    )
    db.upsert_liquidity_snapshot(
        conn, symbol="BBB", as_of_date=today,
        avg_dollar_volume_20d=50_000_000.0, last_close=50.0,
    )
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["scanner_activity"]["rows"]
    assert [r["symbol"] for r in rows] == ["AAA", "BBB"]
    assert rows[0]["score"] >= rows[1]["score"]
    assert rows[0]["score_breakdown"]["liquidity"] >= rows[1]["score_breakdown"]["liquidity"]


def test_score_breakdown_keys(client, isolated_db):
    conn = _seed_strategy("trend-strat")
    _record_scanner_fire(conn, sid="trend-strat", symbol="AAPL", close=180.0)
    conn.close()
    rv = client.get("/api/state")
    rows = rv.get_json()["scanner_activity"]["rows"]
    bd = rows[0]["score_breakdown"]
    assert set(bd.keys()) == {"regime", "volume", "edge", "liquidity"}


def test_summary_by_action_tally(client, isolated_db):
    today = date.today().isoformat()
    conn = _seed_strategy("trend-strat")
    # Two scanner fires, one submitted, one not (no outcomes →
    # SKIP_INELIGIBLE).
    sig_a = _record_scanner_fire(conn, sid="trend-strat", symbol="AAPL",
                                  close=180.0)
    _insert_paper_trade(conn, sig_id=sig_a, sid="trend-strat",
                         symbol="AAPL", side="buy", order_id="o-a",
                         submitted_at=f"{today}T16:30:00")
    _record_scanner_fire(conn, sid="trend-strat", symbol="MSFT",
                          close=300.0)
    conn.close()
    rv = client.get("/api/state")
    sa = rv.get_json()["scanner_activity"]
    assert sa["summary"]["n_fires"] == 2
    by = sa["summary"]["by_action"]
    assert by.get("SUBMITTED") == 1
    assert by.get("SKIP_INELIGIBLE") == 1


# ---------------- markup present ----------------

def test_scanner_card_present_in_research_html():
    page = (ROOT / "dashboard" / "research.html").read_text(encoding="utf-8")
    assert 'id="scanner-activity-card"' in page
    assert 'id="scanner-activity"' in page
    assert 'id="scanner-count"' in page
    assert "renderScannerActivity" in page
    # Renderer wired into refresh loop
    assert "() => renderScannerActivity(s)" in page
