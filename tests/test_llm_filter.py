"""7.1.1 — LLM filter call + shadow table tests.

Validates the same invariants 6.4.2 enforced for the SAR overlay shadow
record, applied to every auto_trader fire:

  - Schema: paper_trades_llm_filter exists after init_db with the
    documented columns; init_db is idempotent (safe to re-run).
  - Structured output: a well-formed Anthropic response parses correctly;
    malformed JSON / missing fields / out-of-range values fall open with
    verdict="allow" and confidence=0.0.
  - Network failure isolation: requests.Timeout / RequestException →
    fail-open, NO exception escapes assess_signal.
  - Daily cap: 200 calls/day enforced via the api_spend table; once the
    cap is hit, further calls fail open. Resets at UTC midnight.
  - Prompt cache marker: the static system prefix is sent with
    cache_control={"type": "ephemeral"} on every request.
  - **No-impact invariant**: when the LLM filter runs alongside the
    auto_trader signal loop in shadow mode, paper_trades is unchanged
    byte-for-byte before vs. after.
"""
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import llm_filter as llmf  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


@pytest.fixture()
def env_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-do-not-leak")
    yield


def _ok_response(text='{"verdict":"allow","confidence":0.8,'
                       '"rationale":"clean fire","factors":["base_rate"]}',
                  *, cache_read=0, cache_creation=0, input_tokens=120,
                  output_tokens=35):
    class _Resp:
        status_code = 200

        def json(self):
            return {
                "content": [{"type": "text", "text": text}],
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                    "output_tokens": output_tokens,
                },
            }
    return _Resp()


def _signal(strategy_id="s1", symbol="SPY", bar_ts="2026-05-22",
            signal_type="long_entry", close=400.0, side="long"):
    return {
        "strategy_id": strategy_id, "symbol": symbol, "bar_ts": bar_ts,
        "signal_type": signal_type, "close": close, "side": side,
    }


# ---------------------------------------------------------------------------
# 1. Schema + idempotent re-init
# ---------------------------------------------------------------------------

def test_paper_trades_llm_filter_table_exists(conn):
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type='table' AND name='paper_trades_llm_filter'"
    ).fetchone()
    assert cur is not None, "paper_trades_llm_filter table missing"


def test_paper_trades_llm_filter_has_expected_columns(conn):
    cols = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(paper_trades_llm_filter)"
        ).fetchall()
    }
    expected = {
        "id", "recorded_at", "strategy_id", "symbol", "bar_ts",
        "signal_type", "side", "close",
        "verdict", "confidence", "rationale", "factors_json",
        "model", "prompt_tokens", "cached_tokens", "output_tokens",
        "latency_ms", "failure_mode",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_init_db_idempotent_for_llm_filter_table(tmp_path, monkeypatch):
    """Re-running init_db on the same path must not raise — confirms the
    CREATE TABLE IF NOT EXISTS is well-formed across runs."""
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c1 = db.init_db(test_db)
    c1.close()
    c2 = db.init_db(test_db)
    c2.execute("PRAGMA table_info(paper_trades_llm_filter)").fetchall()
    c2.close()


def test_uniqueness_per_fire(conn):
    """One row per (strategy_id, symbol, bar_ts, signal_type) — a second
    INSERT for the same fire is a no-op via INSERT OR IGNORE."""
    sig = _signal()
    verdict = {
        "verdict": "allow", "confidence": 0.7,
        "rationale": "ok", "factors": [],
    }
    rowid1 = llmf._persist_verdict(
        conn, signal=sig, verdict=verdict, usage={},
        latency_ms=10, failure_mode=None, model="claude-sonnet-4-6",
    )
    rowid2 = llmf._persist_verdict(
        conn, signal=sig, verdict=verdict, usage={},
        latency_ms=12, failure_mode=None, model="claude-sonnet-4-6",
    )
    assert rowid1 is not None
    assert rowid2 is None, "second insert for same fire must be no-op"
    n = conn.execute(
        "SELECT COUNT(*) FROM paper_trades_llm_filter "
        " WHERE strategy_id=? AND symbol=? AND bar_ts=? AND signal_type=?",
        (sig["strategy_id"], sig["symbol"], sig["bar_ts"], sig["signal_type"]),
    ).fetchone()[0]
    assert n == 1


# ---------------------------------------------------------------------------
# 2. Structured output — well-formed + malformed
# ---------------------------------------------------------------------------

def test_well_formed_response_parses(conn, env_key):
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response()):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    assert v["confidence"] == pytest.approx(0.8)
    assert v["rationale"] == "clean fire"
    assert v["factors"] == ["base_rate"]
    row = conn.execute(
        "SELECT verdict, confidence, factors_json, failure_mode, model "
        "  FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["verdict"] == "allow"
    assert row["failure_mode"] is None
    assert row["model"] == "claude-sonnet-4-6"
    assert json.loads(row["factors_json"]) == ["base_rate"]


def test_skip_verdict_with_three_factors(conn, env_key):
    payload = ('{"verdict":"skip","confidence":0.92,'
                '"rationale":"earnings tomorrow, Fed minutes, intraday halt",'
                '"factors":["earnings_in_1d","fed_minutes_today","halted"]}')
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response(payload)):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "skip"
    assert v["factors"] == ["earnings_in_1d", "fed_minutes_today", "halted"]


def test_downsize_verdict_parses(conn, env_key):
    payload = ('{"verdict":"downsize","confidence":0.55,'
                '"rationale":"thin liquidity","factors":["thin_liquidity"]}')
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response(payload)):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "downsize"


def test_malformed_json_falls_open_and_persists_failure(conn, env_key):
    with patch.object(llmf, "_anthropic_post",
                      return_value=_ok_response("not actually json")):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    assert v["confidence"] == 0.0
    row = conn.execute(
        "SELECT verdict, confidence, failure_mode "
        "  FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["verdict"] == "allow"
    assert row["confidence"] == pytest.approx(0.0)
    assert row["failure_mode"] == "malformed_json"


def test_invalid_verdict_value_falls_open(conn, env_key):
    payload = ('{"verdict":"maybe","confidence":0.5,'
                '"rationale":"x","factors":[]}')
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response(payload)):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    row = conn.execute(
        "SELECT failure_mode FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["failure_mode"] == "malformed_json"


def test_confidence_out_of_range_falls_open(conn, env_key):
    payload = ('{"verdict":"allow","confidence":1.5,'
                '"rationale":"x","factors":[]}')
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response(payload)):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    assert v["confidence"] == 0.0  # fail-open default
    row = conn.execute(
        "SELECT failure_mode FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["failure_mode"] == "malformed_json"


def test_markdown_fenced_json_still_parses(conn, env_key):
    """Defensive — if the model returns ```json {...} ``` we still parse."""
    payload = (
        '```json\n'
        '{"verdict":"allow","confidence":0.6,'
        '"rationale":"ok","factors":["base"]}\n'
        '```'
    )
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response(payload)):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    assert v["confidence"] == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# 3. Network failure isolation
# ---------------------------------------------------------------------------

def test_timeout_falls_open_no_exception(conn, env_key):
    with patch.object(llmf, "_anthropic_post",
                      side_effect=requests.Timeout("timed out")):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    assert v["confidence"] == 0.0
    row = conn.execute(
        "SELECT failure_mode FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["failure_mode"] == "timeout"


def test_connection_error_falls_open(conn, env_key):
    with patch.object(llmf, "_anthropic_post",
                      side_effect=requests.ConnectionError("EOF")):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    row = conn.execute(
        "SELECT failure_mode FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["failure_mode"] == "network_error"


def test_500_response_falls_open(conn, env_key):
    class _500:
        status_code = 500

        def json(self):
            return {"error": "boom"}
    with patch.object(llmf, "_anthropic_post", return_value=_500()):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    row = conn.execute(
        "SELECT failure_mode FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["failure_mode"] == "http_500"


def test_429_response_falls_open(conn, env_key):
    class _429:
        status_code = 429

        def json(self):
            return {"error": "rate"}
    with patch.object(llmf, "_anthropic_post", return_value=_429()):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    row = conn.execute(
        "SELECT failure_mode FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["failure_mode"] == "http_429"


def test_missing_api_key_falls_open(conn, monkeypatch):
    """No env var, no credentials → fail-open without ever calling the API."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        llmf, "load_credentials",
        lambda *a, **kw: (_ for _ in ()).throw(KeyError("anthropic")),
    )
    calls = []
    with patch.object(llmf, "_anthropic_post",
                      side_effect=lambda *a, **kw: calls.append(1)):
        v = llmf.assess_signal(_signal(), conn)
    assert v["verdict"] == "allow"
    assert calls == [], "no API call should be made without a key"
    row = conn.execute(
        "SELECT failure_mode FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["failure_mode"] == "no_api_key"


# ---------------------------------------------------------------------------
# 4. Daily cap
# ---------------------------------------------------------------------------

def test_daily_cap_blocks_further_calls(conn, env_key):
    """Once the cap is hit, subsequent calls fail open without hitting
    the network; the failure_mode is 'daily_cap_exceeded'."""
    today = "2026-05-22"
    with conn:
        conn.execute(
            "INSERT INTO api_spend(provider, spend_date, spend_usd, "
            "  calls, updated_at) VALUES(?, ?, 0.0, ?, ?)",
            (llmf.LLM_FILTER_PROVIDER, today, llmf.DAILY_CALL_CAP,
             today + "T00:00:00+00:00"),
        )
    calls = []
    with patch.object(llmf, "_anthropic_post",
                      side_effect=lambda *a, **kw: calls.append(1)):
        v = llmf.assess_signal(
            _signal(), conn,
            now_iso=today + "T12:00:00+00:00",
        )
    assert v["verdict"] == "allow"
    assert v["confidence"] == 0.0
    assert calls == [], "cap exceeded must NOT issue network call"
    row = conn.execute(
        "SELECT failure_mode FROM paper_trades_llm_filter"
    ).fetchone()
    assert row["failure_mode"] == "daily_cap_exceeded"


def test_daily_cap_resets_at_utc_midnight(conn, env_key):
    """Yesterday's exhausted counter does not block today's calls."""
    yesterday = "2026-05-21"
    today = "2026-05-22"
    with conn:
        conn.execute(
            "INSERT INTO api_spend(provider, spend_date, spend_usd, "
            "  calls, updated_at) VALUES(?, ?, 0.0, ?, ?)",
            (llmf.LLM_FILTER_PROVIDER, yesterday, llmf.DAILY_CALL_CAP,
             yesterday + "T00:00:00+00:00"),
        )
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response()):
        v = llmf.assess_signal(
            _signal(), conn,
            now_iso=today + "T08:00:00+00:00",
        )
    assert v["verdict"] == "allow"
    assert v["confidence"] == pytest.approx(0.8), (
        "today's call should succeed — yesterday's cap is irrelevant"
    )
    assert llmf.calls_today(conn, today=today) == 1
    assert llmf.calls_today(conn, today=yesterday) == llmf.DAILY_CALL_CAP


def test_each_call_records_against_cap(conn, env_key):
    """Successful calls bump the counter so the next call sees it."""
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response()):
        llmf.assess_signal(_signal(symbol="SPY"), conn)
        llmf.assess_signal(_signal(symbol="QQQ"), conn)
        llmf.assess_signal(_signal(symbol="IWM"), conn)
    today = llmf._utc_today()
    assert llmf.calls_today(conn, today=today) == 3


# ---------------------------------------------------------------------------
# 5. Prompt cache marker
# ---------------------------------------------------------------------------

def test_system_prefix_sent_with_cache_control(conn, env_key):
    """Every request must include a cache_control={"type":"ephemeral"}
    breakpoint on the static system prefix — that's the lever that drops
    repeated-call input cost ~90%."""
    captured = {}

    def _capture(url, headers, payload, timeout):
        captured["payload"] = payload
        return _ok_response()

    with patch.object(llmf, "_anthropic_post", side_effect=_capture):
        llmf.assess_signal(_signal(), conn)

    sys_blocks = captured["payload"]["system"]
    assert isinstance(sys_blocks, list) and sys_blocks
    last = sys_blocks[-1]
    assert last.get("cache_control") == {"type": "ephemeral"}, (
        "system prefix must carry cache_control ephemeral breakpoint"
    )
    # Static prefix should be substantially the same on every call —
    # one easy invariant is bytes-equality across two assessments.
    captured_first_bytes = json.dumps(last["text"])

    def _capture2(url, headers, payload, timeout):
        captured["payload2"] = payload
        return _ok_response()

    with patch.object(llmf, "_anthropic_post", side_effect=_capture2):
        llmf.assess_signal(_signal(symbol="QQQ"), conn)
    second_block = captured["payload2"]["system"][-1]
    assert json.dumps(second_block["text"]) == captured_first_bytes, (
        "static system prefix must be byte-identical across calls"
    )


def test_request_uses_sonnet_4_6_by_default(conn, env_key):
    captured = {}

    def _capture(url, headers, payload, timeout):
        captured["model"] = payload["model"]
        return _ok_response()

    with patch.object(llmf, "_anthropic_post", side_effect=_capture):
        llmf.assess_signal(_signal(), conn)
    assert captured["model"] == "claude-sonnet-4-6"


def test_api_key_never_appears_in_persisted_row(conn, env_key):
    """Defensive — the key must not leak via rationale, factors_json, or
    notes columns. Tests an edge-case where a model echoes its prompt."""
    payload = ('{"verdict":"allow","confidence":0.6,'
                '"rationale":"ok","factors":[]}')
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response(payload)):
        llmf.assess_signal(_signal(), conn)
    row = conn.execute(
        "SELECT * FROM paper_trades_llm_filter"
    ).fetchone()
    for col in row.keys():
        val = row[col]
        if isinstance(val, str):
            assert "test-key-do-not-leak" not in val, (
                f"API key leaked into column {col}"
            )


# ---------------------------------------------------------------------------
# 6. No-impact-on-live-PnL invariant
# ---------------------------------------------------------------------------

def _seed_open_position(conn, *, strategy_id, symbol, order_id, qty, price):
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


def _snapshot_paper_trades(conn):
    return [
        tuple(r) for r in conn.execute(
            "SELECT alpaca_order_id, strategy_id, symbol, side, qty, "
            "       status, fill_price, stop_price, notes "
            "  FROM paper_trades ORDER BY id"
        ).fetchall()
    ]


def test_assess_signal_writes_no_paper_trades_row(conn, env_key):
    """The single most important invariant: calling assess_signal MUST
    NOT add, remove, or modify any row in paper_trades. Mirrors
    test_sar_overlay_ab.py:test_shadow_does_not_affect_paper_trades_when_sar_flips.
    """
    _seed_open_position(
        conn, strategy_id="s1", symbol="SPY",
        order_id="ord-llm-1", qty=10, price=400.0,
    )
    before_snapshot = _snapshot_paper_trades(conn)
    before_count = conn.execute(
        "SELECT COUNT(*) FROM paper_trades"
    ).fetchone()[0]

    # All three verdict types in sequence — none of them touch paper_trades.
    responses = [
        '{"verdict":"allow","confidence":0.9,"rationale":"clean","factors":[]}',
        '{"verdict":"skip","confidence":0.85,"rationale":"earnings","factors":["earnings"]}',
        '{"verdict":"downsize","confidence":0.4,"rationale":"thin","factors":["thin"]}',
    ]
    for i, text in enumerate(responses):
        with patch.object(llmf, "_anthropic_post",
                          return_value=_ok_response(text)):
            llmf.assess_signal(
                _signal(symbol="SPY", bar_ts=f"2026-05-{20+i:02d}"), conn,
            )

    after_snapshot = _snapshot_paper_trades(conn)
    after_count = conn.execute(
        "SELECT COUNT(*) FROM paper_trades"
    ).fetchone()[0]
    assert after_count == before_count, (
        "paper_trades row count changed — LLM filter altered live PnL"
    )
    assert after_snapshot == before_snapshot, (
        "paper_trades row contents changed — LLM filter altered live PnL"
    )
    # Shadow rows DID land.
    shadow_n = conn.execute(
        "SELECT COUNT(*) FROM paper_trades_llm_filter"
    ).fetchone()[0]
    assert shadow_n == 3


def test_assess_signal_no_impact_on_failure_paths(conn, env_key):
    """Same invariant under failure conditions: timeout, malformed JSON,
    daily-cap-exceeded all still leave paper_trades untouched."""
    _seed_open_position(
        conn, strategy_id="s1", symbol="QQQ",
        order_id="ord-llm-2", qty=5, price=300.0,
    )
    before = _snapshot_paper_trades(conn)

    # 1. Timeout
    with patch.object(llmf, "_anthropic_post",
                      side_effect=requests.Timeout("t")):
        llmf.assess_signal(_signal(symbol="QQQ", bar_ts="2026-05-22"), conn)
    assert _snapshot_paper_trades(conn) == before

    # 2. Malformed
    with patch.object(llmf, "_anthropic_post",
                      return_value=_ok_response("garbage")):
        llmf.assess_signal(_signal(symbol="QQQ", bar_ts="2026-05-23"), conn)
    assert _snapshot_paper_trades(conn) == before

    # 3. Cap exceeded — pre-load the counter to the cap
    today = "2026-05-24"
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_spend(provider, spend_date, "
            "  spend_usd, calls, updated_at) VALUES(?, ?, 0.0, ?, ?)",
            (llmf.LLM_FILTER_PROVIDER, today, llmf.DAILY_CALL_CAP,
             today + "T00:00:00+00:00"),
        )
    with patch.object(llmf, "_anthropic_post",
                      side_effect=AssertionError("should not be called")):
        llmf.assess_signal(
            _signal(symbol="QQQ", bar_ts="2026-05-24"), conn,
            now_iso=today + "T12:00:00+00:00",
        )
    assert _snapshot_paper_trades(conn) == before


def test_recent_verdicts_returns_rows_in_descending_order(conn, env_key):
    with patch.object(llmf, "_anthropic_post", return_value=_ok_response()):
        for i in range(3):
            llmf.assess_signal(
                _signal(symbol=["SPY", "QQQ", "IWM"][i],
                        bar_ts=f"2026-05-{20+i:02d}"),
                conn,
            )
    rows = llmf.recent_verdicts(conn, limit=10)
    assert len(rows) == 3
    # Newest first.
    assert rows[0]["symbol"] == "IWM"
    assert rows[-1]["symbol"] == "SPY"
    # factors decoded from JSON.
    assert isinstance(rows[0]["factors"], list)


# ---------------------------------------------------------------------------
# 7. Context-gathering helpers — quick sanity
# ---------------------------------------------------------------------------

def test_gather_market_context_returns_safe_default_on_empty_db(conn):
    ctx = llmf.gather_market_context(conn)
    assert ctx["regime"] is None
    assert ctx["macro_strip"] == {}
    assert ctx["notable_movers"] == []


def test_gather_recent_news_returns_empty_on_no_news(conn):
    items = llmf.gather_recent_news(conn, "SPY")
    assert items == []


def test_gather_prior_outcomes_returns_empty_on_no_outcomes(conn):
    out = llmf.gather_prior_outcomes(conn, "s1")
    assert out == []


def test_build_user_message_truncates_lists():
    msg = llmf.build_user_message(
        signal=_signal(),
        market_context={"regime": "trending_up", "macro_strip": {}},
        recent_news=[
            {"title": f"t{i}", "publisher": "p", "published_utc": "x",
             "sentiment": None}
            for i in range(20)
        ],
        earnings=[],
        prior_outcomes=[
            {"return_pct": 1.0, "exit_reason": "tp"} for _ in range(20)
        ],
    )
    parsed = json.loads(msg.split("Return ONLY the JSON object.", 1)[1].strip()
                        if "Return ONLY the JSON object." in msg
                        else msg[msg.index("{"):])
    assert len(parsed["recent_news_24h"]) == 5
    assert len(parsed["prior_5_closed_outcomes"]) == 5
