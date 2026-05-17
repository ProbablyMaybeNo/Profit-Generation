"""Tests for monitoring.api_budget + the budget-gated codegen_claude
path (milestone 4.3.3).

Covers:
  - Config loading + per-provider cap resolution
  - todays_spend_usd / record_spend round-trips through the api_spend table
  - can_spend / assert_can_spend gating
  - The daily reset at UTC midnight (different date = zero spend)
  - generate_with_budget_gate fallback to Ollama on exhaustion
  - The once-per-day Telegram alert dedupe
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import api_budget as bud  # noqa: E402
from monitoring import codegen_claude as cc  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def test_load_budget_config_missing_returns_empty(tmp_path):
    p = tmp_path / "nope.json"
    assert bud.load_budget_config(p) == {}


def test_load_budget_config_unparseable_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not-json{", encoding="utf-8")
    assert bud.load_budget_config(p) == {}


def test_load_budget_config_valid(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(
        json.dumps({"anthropic": {"daily_usd_cap": 7.5, "enabled": True}}),
        encoding="utf-8",
    )
    out = bud.load_budget_config(p)
    assert out["anthropic"]["daily_usd_cap"] == 7.5


def test_daily_cap_uses_default_when_missing():
    assert bud.daily_cap_usd({}) == bud.DEFAULT_DAILY_CAP_USD


def test_daily_cap_uses_configured_value():
    cap = bud.daily_cap_usd({"anthropic": {"daily_usd_cap": 9.99}})
    assert cap == pytest.approx(9.99)


def test_daily_cap_zero_when_disabled():
    cap = bud.daily_cap_usd({"anthropic": {"daily_usd_cap": 5.0,
                                            "enabled": False}})
    assert cap == 0.0


def test_daily_cap_unparseable_falls_back_to_default():
    cap = bud.daily_cap_usd({"anthropic": {"daily_usd_cap": "abc"}})
    assert cap == bud.DEFAULT_DAILY_CAP_USD


# ---------------------------------------------------------------------------
# Spend round-trip
# ---------------------------------------------------------------------------

def test_todays_spend_zero_when_no_rows(isolated_db):
    conn = db.init_db()
    try:
        assert bud.todays_spend_usd(conn) == 0.0
    finally:
        conn.close()


def test_record_spend_accumulates(isolated_db):
    conn = db.init_db()
    try:
        state1 = bud.record_spend(conn, 1.0, calls=1)
        assert state1["spend_usd"] == pytest.approx(1.0)
        assert state1["calls"] == 1
        state2 = bud.record_spend(conn, 0.5, calls=2)
        assert state2["spend_usd"] == pytest.approx(1.5)
        assert state2["calls"] == 3
        assert bud.todays_spend_usd(conn) == pytest.approx(1.5)
    finally:
        conn.close()


def test_record_spend_negative_rejected(isolated_db):
    conn = db.init_db()
    try:
        with pytest.raises(ValueError):
            bud.record_spend(conn, -0.01)
    finally:
        conn.close()


def test_daily_reset_at_utc_midnight(isolated_db):
    """A row dated 2026-05-16 should not contribute to a 2026-05-17 query."""
    conn = db.init_db()
    try:
        with conn:
            conn.execute(
                "INSERT INTO api_spend(provider, spend_date, spend_usd, "
                " calls, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("anthropic", "2026-05-16", 4.99, 1,
                 "2026-05-16T23:59:00+00:00"),
            )
        tomorrow = datetime(2026, 5, 17, 0, 0, 1, tzinfo=timezone.utc)
        check = bud.can_spend(conn, cap_usd=5.0, now_fn=lambda: tomorrow)
        assert check["ok"] is True
        assert check["spent_usd"] == 0.0
        assert check["today"] == "2026-05-17"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# can_spend / assert_can_spend
# ---------------------------------------------------------------------------

def test_can_spend_below_cap(isolated_db):
    conn = db.init_db()
    try:
        bud.record_spend(conn, 1.0)
        check = bud.can_spend(conn, cap_usd=5.0)
        assert check["ok"] is True
        assert check["spent_usd"] == pytest.approx(1.0)
    finally:
        conn.close()


def test_can_spend_at_cap_returns_false(isolated_db):
    """Strictly < cap. Exactly == cap is exhausted."""
    conn = db.init_db()
    try:
        bud.record_spend(conn, 5.0)
        check = bud.can_spend(conn, cap_usd=5.0)
        assert check["ok"] is False
    finally:
        conn.close()


def test_assert_can_spend_raises_on_exhaustion(isolated_db):
    conn = db.init_db()
    try:
        bud.record_spend(conn, 5.5)
        with pytest.raises(bud.BudgetExhausted) as exc:
            bud.assert_can_spend(conn, cap_usd=5.0)
        assert exc.value.spent_usd == pytest.approx(5.5)
        assert exc.value.cap_usd == pytest.approx(5.0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# make_usage_recorder — converts usage dicts to spend rows
# ---------------------------------------------------------------------------

def test_usage_recorder_writes_spend_row(isolated_db):
    conn = db.init_db()
    try:
        rec = bud.make_usage_recorder(conn,
                                       pricing_fn=lambda u: 0.25)
        rec({"input_tokens": 100})
        rec({"input_tokens": 100})
        assert bud.todays_spend_usd(conn) == pytest.approx(0.50)
    finally:
        conn.close()


def test_usage_recorder_pricing_failure_non_fatal(isolated_db):
    conn = db.init_db()
    try:
        def boom(_u):
            raise RuntimeError("pricing broke")
        rec = bud.make_usage_recorder(conn, pricing_fn=boom)
        rec({"input_tokens": 100})  # must not raise
        assert bud.todays_spend_usd(conn) == 0.0
    finally:
        conn.close()


def test_usage_recorder_skips_zero_spend(isolated_db):
    conn = db.init_db()
    try:
        rec = bud.make_usage_recorder(conn, pricing_fn=lambda u: 0.0)
        rec({})
        # No row written when spend was zero.
        row = conn.execute(
            "SELECT COUNT(*) FROM api_spend"
        ).fetchone()
        assert row[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# generate_with_budget_gate — wired path
# ---------------------------------------------------------------------------

_GOOD_FN = """\
def compute_widget(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["long_entry"] = (df["close"] < df["close"].shift(1)).fillna(False)
    out["long_exit"] = (df["close"] > df["close"].shift(1)).fillna(False)
    return out
"""


def test_budget_gate_calls_claude_when_under_cap(isolated_db, monkeypatch):
    conn = db.init_db()
    try:
        # Stub the network call.
        def fake_post(url, headers, payload, timeout):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "content": [{"type": "text", "text": _GOOD_FN}],
                "usage": {"input_tokens": 100, "output_tokens": 100},
            }
            return r
        monkeypatch.setattr(cc, "_anthropic_post", fake_post)
        out = cc.generate_with_budget_gate(
            "compute_widget",
            entry_rules="e", exit_rules="x",
            conn=conn, cap_usd=5.0, api_key="test",
        )
        assert out["provider"] == "claude"
        assert "def compute_widget" in out["code"]
        # Spend was recorded.
        assert bud.todays_spend_usd(conn) > 0
    finally:
        conn.close()


def test_budget_gate_falls_back_to_ollama_when_exhausted(isolated_db,
                                                          monkeypatch):
    conn = db.init_db()
    try:
        bud.record_spend(conn, 6.0)  # blows past the $5 cap

        def fallback():
            return _GOOD_FN

        sent = []
        out = cc.generate_with_budget_gate(
            "compute_widget",
            entry_rules="e", exit_rules="x",
            conn=conn, cap_usd=5.0,
            fallback_fn=fallback,
            alert_fn=lambda text: (sent.append(text), True)[1],
        )
        assert out["provider"] == "ollama"
        assert "def compute_widget" in out["code"]
        # Alert fired once.
        assert len(sent) == 1
        assert "budget exhausted" in sent[0].lower()
    finally:
        conn.close()


def test_budget_gate_alert_dedupe_within_day(isolated_db):
    conn = db.init_db()
    try:
        bud.record_spend(conn, 6.0)
        sent = []
        # First call fires alert.
        cc.generate_with_budget_gate(
            "compute_widget", entry_rules="e", exit_rules="x",
            conn=conn, cap_usd=5.0,
            fallback_fn=lambda: _GOOD_FN,
            alert_fn=lambda t: (sent.append(t), True)[1],
        )
        # Second call same day must NOT re-alert.
        cc.generate_with_budget_gate(
            "compute_widget", entry_rules="e", exit_rules="x",
            conn=conn, cap_usd=5.0,
            fallback_fn=lambda: _GOOD_FN,
            alert_fn=lambda t: (sent.append(t), True)[1],
        )
        assert len(sent) == 1
    finally:
        conn.close()


def test_budget_gate_no_alert_when_under_cap(isolated_db, monkeypatch):
    conn = db.init_db()
    try:
        def fake_post(url, headers, payload, timeout):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "content": [{"type": "text", "text": _GOOD_FN}],
                "usage": {"input_tokens": 100, "output_tokens": 100},
            }
            return r
        monkeypatch.setattr(cc, "_anthropic_post", fake_post)
        sent = []
        out = cc.generate_with_budget_gate(
            "compute_widget", entry_rules="e", exit_rules="x",
            conn=conn, cap_usd=5.0, api_key="test",
            alert_fn=lambda t: (sent.append(t), True)[1],
        )
        assert out["provider"] == "claude"
        assert sent == []
    finally:
        conn.close()
