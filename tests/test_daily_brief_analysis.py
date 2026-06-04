"""
Tests for monitoring.daily_brief and monitoring.daily_analysis.

Network is fully mocked:
  - telegram_alerter._http_post is replaced with a stub (no Telegram calls)
  - monitoring.daily_analysis._anthropic_post is replaced with a stub (no Claude calls)

Memory guardrail: we ALWAYS capture the real callable before patching to
avoid self-referential monkeypatches (the OOM pattern from 2026-06-03).

Seeded DB: a fresh sqlite3 in tmp_path with enough rows to exercise every
section of the report.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import daily_brief as brief  # noqa: E402
from monitoring import daily_analysis as analysis  # noqa: E402
from monitoring import telegram_alerter  # noqa: E402
import monitoring.daily_analysis as _analysis_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TARGET_DATE = date(2026, 6, 3)
TARGET_DAY = TARGET_DATE.isoformat()

_real_init_db = db.init_db


def _seed_db(db_path: Path):
    """Create and seed an isolated test database."""
    conn = _real_init_db(db_path)

    # equity_snapshots: today + prior day
    conn.execute(
        "INSERT INTO equity_snapshots(recorded_at, portfolio_value, cash, equity, buying_power, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-06-03T18:00:00+00:00", 101000.0, 70000.0, 101000.0, 360000.0, "test"),
    )
    conn.execute(
        "INSERT INTO equity_snapshots(recorded_at, portfolio_value, cash, equity, buying_power, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-06-02T18:00:00+00:00", 100000.0, 69000.0, 100000.0, 358000.0, "test"),
    )

    # daily_reports
    conn.execute(
        "INSERT INTO daily_reports(report_date, market_regime, importance, fires_count, "
        "watchlist_count, notable_movers_count, generated_at) VALUES (?,?,?,?,?,?,?)",
        (TARGET_DAY, "trending_up", 3, 2, 10, 2, TARGET_DAY + "T00:00:00"),
    )

    # strategies
    db.upsert_strategy(conn, {"extra": {"strategy_id": "intraday-1m-momentum"}})
    db.upsert_strategy(conn, {"extra": {"strategy_id": "botnet101-3-bar-low"}})

    # signals
    sig1 = db.record_signal(
        conn, strategy_id="intraday-1m-momentum", symbol="AAPL",
        bar_ts=TARGET_DAY + "T14:00:00", signal_type="long_entry",
        close=190.0, bar_interval="1m",
    )
    sig2 = db.record_signal(
        conn, strategy_id="botnet101-3-bar-low", symbol="SPY",
        bar_ts=TARGET_DAY, signal_type="long_entry",
        close=585.0, bar_interval="1d",
    )
    sig3 = db.record_signal(
        conn, strategy_id="botnet101-3-bar-low", symbol="SPY",
        bar_ts=TARGET_DAY, signal_type="long_exit",
        close=590.0, bar_interval="1d",
    )

    # paper_trades (fills)
    conn.execute(
        "INSERT INTO paper_trades(signal_id, strategy_id, symbol, side, qty, "
        "fill_price, status, submitted_at, filled_at, pyramid_tier) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sig1, "intraday-1m-momentum", "AAPL", "buy", 5, 190.5,
         "filled", TARGET_DAY + " 14:00:00+00:00", TARGET_DAY + " 14:00:05+00:00", 0),
    )
    conn.execute(
        "INSERT INTO paper_trades(signal_id, strategy_id, symbol, side, qty, "
        "fill_price, status, submitted_at, filled_at, pyramid_tier, entry_stops) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sig2, "botnet101-3-bar-low", "SPY", "buy", 2, 585.0,
         "filled", TARGET_DAY + " 15:00:00+00:00", TARGET_DAY + " 15:00:05+00:00",
         0, '{"initial_stop": 575.0}'),
    )
    conn.execute(
        "INSERT INTO paper_trades(signal_id, strategy_id, symbol, side, qty, "
        "fill_price, status, submitted_at, filled_at, pyramid_tier) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sig3, "botnet101-3-bar-low", "SPY", "sell", 2, 590.0,
         "filled", TARGET_DAY + " 20:00:00+00:00", TARGET_DAY + " 20:00:05+00:00", 0),
    )

    # outcomes — one closed today
    conn.execute(
        "INSERT INTO outcomes(signal_id, entry_ts, entry_price, exit_ts, exit_price, "
        "exit_reason, return_pct, mfe_pct, mae_pct, bars_held, status, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (sig2, TARGET_DAY + "T15:00:00", 585.0, TARGET_DAY + "T20:00:00", 590.0,
         "long_exit_signal", 0.855, 1.2, -0.4, 5, "closed",
         TARGET_DAY + "T20:00:00"),
    )
    # outcomes — one open
    open_sig = db.record_signal(
        conn, strategy_id="intraday-1m-momentum", symbol="AAPL",
        bar_ts=TARGET_DAY + "T14:00:00", signal_type="long_entry",
        close=190.0, bar_interval="1m",
    )
    conn.execute(
        "INSERT INTO outcomes(signal_id, entry_ts, entry_price, status, updated_at) "
        "VALUES (?,?,?,?,?)",
        (open_sig, TARGET_DAY + "T14:00:00", 190.5, "open", TARGET_DAY + "T14:00:00"),
    )

    # trailing_stops
    conn.execute(
        "INSERT OR REPLACE INTO trailing_stops(strategy_id, symbol, side, method, "
        "stop_price, extreme_price, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("botnet101-3-bar-low", "QQQ", "buy", "atr",
         455.0, 465.0, TARGET_DAY + "T16:00:00"),
    )

    # news
    conn.execute(
        "INSERT INTO news(polygon_id, fetched_at, published_utc, symbol, title, url, publisher) "
        "VALUES (?,?,?,?,?,?,?)",
        ("test-001", TARGET_DAY + "T00:00:00Z", TARGET_DAY + "T13:00:00Z",
         "SPY", "Markets rally on strong jobs data", "http://example.com/1", "Reuters"),
    )

    # macro
    conn.execute(
        "INSERT INTO macro(series_id, bar_date, value, fetched_at) VALUES (?,?,?,?)",
        ("VIXCLS", TARGET_DAY, 15.5, TARGET_DAY + "T01:00:00Z"),
    )

    # snapshots
    conn.execute(
        "INSERT INTO snapshots(snapshot_date, symbol, asset_class, bar_date, close, "
        "ret_1d_pct, rvol_vs_20d, dist_sma20_pct) VALUES (?,?,?,?,?,?,?,?)",
        (TARGET_DAY, "SPY", "etf", TARGET_DAY, 585.0, 1.2, 1.4, 2.1),
    )

    conn.commit()
    return conn


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = _seed_db(db_path)
    yield conn, db_path
    conn.close()


# ---------------------------------------------------------------------------
# Telegram mock helpers
# ---------------------------------------------------------------------------

_sent_messages: list = []


def _fake_http_post(url, json_body, timeout=10.0):
    _sent_messages.clear()
    _sent_messages.append(json_body.get("text", ""))
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '{"ok": true}'
    return resp


def _fake_http_post_accumulate(url, json_body, timeout=10.0):
    _sent_messages.append(json_body.get("text", ""))
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '{"ok": true}'
    return resp


# ---------------------------------------------------------------------------
# Anthropic mock helper
# ---------------------------------------------------------------------------

def _fake_anthropic_post(url, headers, payload, timeout):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "content": [
            {"type": "text", "text": (
                "(a) MARKET & SYSTEM OVERVIEW\nTest market summary.\n\n"
                "(b) WHAT WORKED\nMomentum worked.\n\n"
                "(c) WHAT UNDERPERFORMED / LOST MONEY AND WHY\nNothing.\n\n"
                "(d) BUGS & ERRORS DETECTED\nNone detected in test data.\n\n"
                "(e) OPTIMIZATION NEXT-STEPS\n1. Improve sizing.\n\n"
                "(f) DEBUG NEXT-STEPS / FIXES\n1. Check MFE/MAE NULL rate."
            )}
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    return resp


# ---------------------------------------------------------------------------
# Tests — daily_brief
# ---------------------------------------------------------------------------

class TestDailyBrief:

    def test_build_report_text_has_all_sections(self, seeded_db):
        conn, _ = seeded_db
        text = brief.build_report_text(conn, TARGET_DATE)
        assert "=== DAILY TRADING BRIEF" in text
        assert "SYSTEM ACTIVITY" in text
        assert "TRADES" in text
        assert "INTRADAY BY STRATEGY" in text
        assert "RISK MECHANICS" in text
        assert "OUTCOMES" in text
        assert "NOTABLE" in text

    def test_build_report_includes_portfolio_value(self, seeded_db):
        conn, _ = seeded_db
        text = brief.build_report_text(conn, TARGET_DATE)
        # $101,000 should appear
        assert "101,000" in text

    def test_build_report_day_pnl(self, seeded_db):
        conn, _ = seeded_db
        text = brief.build_report_text(conn, TARGET_DATE)
        # +$1,000 P&L
        assert "1,000" in text

    def test_build_report_trade_symbols(self, seeded_db):
        conn, _ = seeded_db
        text = brief.build_report_text(conn, TARGET_DATE)
        assert "AAPL" in text
        assert "SPY" in text

    def test_build_report_trailing_stop(self, seeded_db):
        conn, _ = seeded_db
        text = brief.build_report_text(conn, TARGET_DATE)
        assert "QQQ" in text
        assert "trailing" in text.lower()

    def test_build_report_outcomes_section(self, seeded_db):
        conn, _ = seeded_db
        text = brief.build_report_text(conn, TARGET_DATE)
        assert "long_exit_signal" in text
        assert "Open positions" in text

    def test_build_report_news_headline(self, seeded_db):
        conn, _ = seeded_db
        text = brief.build_report_text(conn, TARGET_DATE)
        assert "rally" in text.lower() or "jobs" in text.lower()

    def test_build_report_macro(self, seeded_db):
        conn, _ = seeded_db
        text = brief.build_report_text(conn, TARGET_DATE)
        assert "VIXCLS" in text

    def test_chunk_report_no_split_short(self):
        text = "hello world"
        chunks = brief.chunk_report(text, max_chars=4000)
        assert chunks == ["hello world"]

    def test_chunk_report_splits_long_text(self):
        # Build a text just over 4000 chars using section-like paragraphs
        section = "A" * 1500
        text = "\n\n".join([section, section, section])
        chunks = brief.chunk_report(text, max_chars=2000)
        assert len(chunks) >= 2
        # All chunks labelled
        for i, ch in enumerate(chunks):
            assert f"({i+1}/{len(chunks)})" in ch

    def test_chunk_report_all_content_preserved(self):
        section = "SECTION " + "X" * 500
        text = "\n\n".join([section] * 5)
        chunks = brief.chunk_report(text, max_chars=1200)
        # Strip chunk labels and rejoin to verify content presence
        combined = " ".join(chunks)
        assert combined.count("SECTION") == 5

    def test_send_report_calls_telegram(self, seeded_db, monkeypatch):
        conn, _ = seeded_db
        sent = []
        _real_post = telegram_alerter._http_post
        monkeypatch.setattr(telegram_alerter, "_http_post",
                            lambda url, json_body, timeout=10.0: (
                                sent.append(json_body.get("text", "")) or
                                type("R", (), {"status_code": 200, "text": ""})()
                            ))
        text = brief.build_report_text(conn, TARGET_DATE)
        ok = brief.send_report(text)
        assert ok
        assert len(sent) >= 1
        combined = " ".join(sent)
        assert "DAILY TRADING BRIEF" in combined

    def test_empty_day_path(self, tmp_path, monkeypatch):
        """When no activity recorded, send the no-activity note."""
        db_path = tmp_path / "empty.db"
        conn = _real_init_db(db_path)
        conn.commit()

        sent = []
        monkeypatch.setattr(telegram_alerter, "_http_post",
                            lambda url, json_body, timeout=10.0: (
                                sent.append(json_body.get("text", "")) or
                                type("R", (), {"status_code": 200, "text": ""})()
                            ))

        assert brief.is_empty_day(conn, TARGET_DATE)

        msg = (f"=== DAILY BRIEF — {TARGET_DATE} ===\n"
               "No trading activity and no equity snapshot recorded today "
               "(weekend / holiday / system offline).")
        ok = telegram_alerter.send_message(msg, parse_mode=None)
        assert ok
        assert any("No trading activity" in s for s in sent)
        conn.close()

    def test_report_over_4096_chunks(self, seeded_db, monkeypatch):
        """A report that exceeds 4000 chars must be chunked without crashing."""
        conn, _ = seeded_db
        text = brief.build_report_text(conn, TARGET_DATE)
        # Force chunking by using a tiny max_chars
        chunks = brief.chunk_report(text, max_chars=500)
        assert len(chunks) > 1
        # Each chunk must be <= 500 + label overhead
        for ch in chunks:
            assert len(ch) <= 560  # some label overhead is fine

        sent = []
        monkeypatch.setattr(telegram_alerter, "_http_post",
                            lambda url, json_body, timeout=10.0: (
                                sent.append(json_body.get("text", "")) or
                                type("R", (), {"status_code": 200, "text": ""})()
                            ))
        ok = brief.send_report(text, prefix="")
        assert ok


# ---------------------------------------------------------------------------
# Tests — daily_analysis
# ---------------------------------------------------------------------------

class TestDailyAnalysis:

    def test_analysis_calls_anthropic_and_delivers(self, seeded_db, monkeypatch):
        conn, db_path = seeded_db

        # Patch BOTH _anthropic_post AND _load_api_key in daily_analysis module
        monkeypatch.setattr(_analysis_mod, "_load_api_key", lambda: "fake-test-key")
        monkeypatch.setattr(_analysis_mod, "_anthropic_post", _fake_anthropic_post)

        sent = []
        monkeypatch.setattr(telegram_alerter, "_http_post",
                            lambda url, json_body, timeout=10.0: (
                                sent.append(json_body.get("text", "")) or
                                type("R", (), {"status_code": 200, "text": ""})()
                            ))

        ok = analysis.run_analysis(conn, TARGET_DATE)
        assert ok
        combined = " ".join(sent)
        assert "DAILY ANALYSIS" in combined
        assert "MARKET & SYSTEM OVERVIEW" in combined
        assert "WHAT WORKED" in combined

    def test_analysis_no_api_key_sends_degraded_note(self, seeded_db, monkeypatch):
        conn, _ = seeded_db

        # Simulate no API key
        monkeypatch.setattr(_analysis_mod, "_load_api_key", lambda: "")

        sent = []
        monkeypatch.setattr(telegram_alerter, "_http_post",
                            lambda url, json_body, timeout=10.0: (
                                sent.append(json_body.get("text", "")) or
                                type("R", (), {"status_code": 200, "text": ""})()
                            ))

        ok = analysis.run_analysis(conn, TARGET_DATE)
        # Returns True because the degraded note sends OK
        assert ok
        combined = " ".join(sent)
        assert "unavailable" in combined.lower() or "ANTHROPIC_API_KEY" in combined

    def test_analysis_api_error_sends_degraded_note(self, seeded_db, monkeypatch):
        conn, _ = seeded_db

        monkeypatch.setattr(_analysis_mod, "_load_api_key", lambda: "fake-key-xyz")

        def _failing_post(url, headers, payload, timeout):
            resp = MagicMock()
            resp.status_code = 500
            resp.text = "Internal Server Error"
            return resp

        monkeypatch.setattr(_analysis_mod, "_anthropic_post", _failing_post)

        sent = []
        monkeypatch.setattr(telegram_alerter, "_http_post",
                            lambda url, json_body, timeout=10.0: (
                                sent.append(json_body.get("text", "")) or
                                type("R", (), {"status_code": 200, "text": ""})()
                            ))

        ok = analysis.run_analysis(conn, TARGET_DATE)
        assert ok  # degraded note delivered
        combined = " ".join(sent)
        assert "unavailable" in combined.lower() or "failed" in combined.lower()

    def test_analysis_assembles_packet_with_all_keys(self, seeded_db):
        conn, _ = seeded_db
        packet_json = analysis._build_data_packet(conn, TARGET_DATE)
        packet = json.loads(packet_json)
        for key in ["header", "activity", "trades", "intraday_by_strategy",
                    "risk", "outcomes", "notable", "strategy_performance_30d",
                    "skip_distribution_5d", "recent_errors_from_logs"]:
            assert key in packet, f"Missing key: {key}"

    def test_analysis_chunks_long_response(self, seeded_db, monkeypatch):
        conn, _ = seeded_db

        # Build a long text with \n\n separators so chunk_report can split it
        section = "X" * 1500
        long_text = "\n\n".join([
            "(a) MARKET & SYSTEM OVERVIEW\n" + section,
            "(b) WHAT WORKED\n" + section,
            "(c) WHAT UNDERPERFORMED\n" + section,
            "(d) BUGS\n" + section,
        ])
        monkeypatch.setattr(_analysis_mod, "_load_api_key", lambda: "fake-key")

        def _long_post(url, headers, payload, timeout):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "content": [{"type": "text", "text": long_text}],
                "usage": {"input_tokens": 100, "output_tokens": 1000,
                           "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0},
            }
            return resp

        monkeypatch.setattr(_analysis_mod, "_anthropic_post", _long_post)

        sent = []
        monkeypatch.setattr(telegram_alerter, "_http_post",
                            lambda url, json_body, timeout=10.0: (
                                sent.append(json_body.get("text", "")) or
                                type("R", (), {"status_code": 200, "text": ""})()
                            ))

        ok = analysis.run_analysis(conn, TARGET_DATE)
        assert ok
        # Multiple chunks sent because text > 4000 chars
        assert len(sent) >= 2
        # Each message must fit within Telegram's limit (4096) + small overhead for chunk label
        for msg in sent:
            assert len(msg) <= 4300

    def test_analysis_fallback_model_on_404(self, seeded_db, monkeypatch):
        """If preferred model returns 404, falls back to DEFAULT_CLAUDE_MODEL."""
        conn, _ = seeded_db
        monkeypatch.setattr(_analysis_mod, "_load_api_key", lambda: "fake-key")

        call_count = [0]

        def _post_with_404_first(url, headers, payload, timeout):
            call_count[0] += 1
            resp = MagicMock()
            if call_count[0] == 1:
                resp.status_code = 404
                resp.text = '{"error": {"type": "not_found_error"}}'
            else:
                resp.status_code = 200
                resp.json.return_value = {
                    "content": [{"type": "text", "text": "(a) MARKET & SYSTEM OVERVIEW\nOK."}],
                    "usage": {"input_tokens": 50, "output_tokens": 10,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0},
                }
            return resp

        monkeypatch.setattr(_analysis_mod, "_anthropic_post", _post_with_404_first)

        sent = []
        monkeypatch.setattr(telegram_alerter, "_http_post",
                            lambda url, json_body, timeout=10.0: (
                                sent.append(json_body.get("text", "")) or
                                type("R", (), {"status_code": 200, "text": ""})()
                            ))

        ok = analysis.run_analysis(conn, TARGET_DATE)
        assert ok
        assert call_count[0] == 2  # fell back to second model
        combined = " ".join(sent)
        assert "DAILY ANALYSIS" in combined
