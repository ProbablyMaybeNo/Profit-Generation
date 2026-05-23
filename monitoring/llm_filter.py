"""
llm_filter.py — 7.1.1 LLM contextual filter on every auto_trader fire
(strict shadow mode).

Every fire that auto_trader receives — EOD or intraday — gets shipped to
Claude as a structured prompt and the verdict is written to
`paper_trades_llm_filter`. **The verdict is NOT consumed by auto_trader
in this milestone** — it's recorded for 30 days of A/B comparison before
the filter earns its place in the live decision path (7.1.3).

Architecture mirrors monitoring/sar_overlay.py's shadow record:
  - parallel table, never touches paper_trades
  - integration is a side effect on the fire-detection point
  - safe-by-default: any failure (network, API error, malformed JSON,
    daily cap exceeded) defaults to verdict="allow" with confidence=0.0
    so the strategy fires unchanged

Prompt design:
  - System prefix (instructions + JSON schema) is cache_control flagged so
    repeated calls cost ~0.1× input price after the first.
  - User message carries the variable per-fire context: signal, market
    regime, recent news, earnings calendar, prior 5 outcomes.

Network seam: `_anthropic_post` mirrors codegen_claude's pattern — the
real call uses the `requests` library against the Messages API directly
(no SDK dependency). Tests mock `_anthropic_post` to inject responses.

Locked decisions (see docs/PHASE7_PLAN CURRENT.md Decisions log):
  1. Fail-open — any failure → verdict="allow", confidence=0.0
  2. Model — claude-sonnet-4-6 (chosen for the shadow phase)
  3. 200 calls/day cap; resets at UTC midnight; fail-open on cap
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import textwrap
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.utils import load_credentials, log  # noqa: E402


ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_OUTPUT_TOKENS = 400
DEFAULT_TIMEOUT_SEC = 20
DAILY_CALL_CAP = 200

VALID_VERDICTS = ("allow", "skip", "downsize")
FAIL_OPEN_VERDICT = "allow"
FAIL_OPEN_CONFIDENCE = 0.0


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = textwrap.dedent("""\
    You are a contextual filter on trading signals from rule-based
    strategies. A strategy has just generated a signal. Your job is to
    decide — given the broader market context, news, earnings calendar,
    and the strategy's recent track record — whether this specific fire
    is one the strategy should take, skip, or downsize.

    Decision criteria (apply in order):
      1. ALLOW unless something explicitly argues against the fire. The
         rule-based strategy already cleared its own filters.
      2. SKIP when a known event makes the fire materially worse than
         the rule alone implies — earnings within ±2 days the rule
         missed, intraday halt, broad-market shock not yet priced in,
         strategy on a documented losing streak in this regime.
      3. DOWNSIZE when context is ambiguous-but-leaning-bad — softer
         signals like a thin liquidity day, sector-wide correlated
         fires, or moderate prior-period drawdown.

    Output ONLY a single JSON object matching this schema, nothing
    else — no markdown fences, no preamble:

    {
      "verdict":    "allow" | "skip" | "downsize",
      "confidence": <float 0.0-1.0>,
      "rationale":  "<one sentence, plain English, ≤ 200 chars>",
      "factors":    ["<short_tag>", ...]  // up to 3 short tags like
                                          // "fed_minutes_today",
                                          // "earnings_in_2d",
                                          // "halted_intraday",
                                          // "thin_liquidity"
    }
    """)


def build_system_blocks() -> List[Dict[str, Any]]:
    """The static system prefix (instructions + schema) as a single
    cache_control-flagged block. Identical bytes across every call so
    prompt caching reads it back at ~0.1× input price after first use.
    """
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def build_user_message(
    *,
    signal: Dict[str, Any],
    market_context: Dict[str, Any],
    recent_news: List[Dict[str, Any]],
    earnings: List[Dict[str, Any]],
    prior_outcomes: List[Dict[str, Any]],
) -> str:
    """Render the per-fire variable context as the user message.

    Keys deliberately sorted so a same-input call produces same-bytes
    output (cache stability if a request is retried with the exact
    same context). Lists are truncated to the documented caps.
    """
    payload = {
        "signal": {
            "strategy_id": signal.get("strategy_id"),
            "symbol": signal.get("symbol"),
            "side": signal.get("side", "long"),
            "signal_type": signal.get("signal_type"),
            "bar_ts": signal.get("bar_ts"),
            "close": signal.get("close"),
        },
        "market_context": {
            "regime": market_context.get("regime"),
            "macro_strip": market_context.get("macro_strip") or {},
            "notable_movers": (market_context.get("notable_movers") or [])[:10],
        },
        "recent_news_24h": [
            {
                "published_utc": item.get("published_utc"),
                "title": item.get("title"),
                "publisher": item.get("publisher"),
                "sentiment": item.get("sentiment"),
            }
            for item in (recent_news or [])[:5]
        ],
        "earnings_within_5d": [
            {
                "symbol": item.get("symbol"),
                "earnings_date": item.get("earnings_date"),
                "days_until": item.get("days_until"),
            }
            for item in (earnings or [])[:5]
        ],
        "prior_5_closed_outcomes": [
            {
                "exit_ts": item.get("exit_ts"),
                "return_pct": item.get("return_pct"),
                "exit_reason": item.get("exit_reason"),
                "bars_held": item.get("bars_held"),
            }
            for item in (prior_outcomes or [])[:5]
        ],
    }
    return (
        "Decide on this signal. Return ONLY the JSON object.\n\n"
        + json.dumps(payload, sort_keys=True, indent=2)
    )


# ---------------------------------------------------------------------------
# Network seam — raw Anthropic Messages API call
# ---------------------------------------------------------------------------

def _load_api_key() -> str:
    """Env var > credentials.json.anthropic.api_key. Never logged."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        section = load_credentials("anthropic")
        if isinstance(section, dict):
            return section.get("api_key") or ""
    except Exception:
        return ""
    return ""


def _anthropic_post(
    url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float,
):
    """Indirection seam — tests mock this to inject responses."""
    return requests.post(url, headers=headers, json=payload, timeout=timeout)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Daily call cap — backed by the existing api_spend table
# ---------------------------------------------------------------------------

LLM_FILTER_PROVIDER = "anthropic_llm_filter"


def calls_today(conn: sqlite3.Connection, today: Optional[str] = None) -> int:
    """How many llm_filter calls have been recorded today (UTC). Reading
    a future date returns 0; the cap implicitly resets at UTC midnight."""
    row = conn.execute(
        "SELECT calls FROM api_spend "
        " WHERE provider=? AND spend_date=?",
        (LLM_FILTER_PROVIDER, today or _utc_today()),
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row["calls"] if hasattr(row, "keys") else row[0]) or 0
    except (TypeError, ValueError):
        return 0


def _record_call(conn: sqlite3.Connection, today: Optional[str] = None) -> None:
    """Bump today's counter by 1."""
    d = today or _utc_today()
    with conn:
        conn.execute(
            "INSERT INTO api_spend(provider, spend_date, spend_usd, calls, "
            "  updated_at) "
            "VALUES(?, ?, 0.0, 1, ?) "
            "ON CONFLICT(provider, spend_date) DO UPDATE SET "
            "  calls = api_spend.calls + 1, "
            "  updated_at = excluded.updated_at",
            (LLM_FILTER_PROVIDER, d, _utc_now_iso()),
        )


# ---------------------------------------------------------------------------
# Shadow table — schema + insert
# ---------------------------------------------------------------------------

def _ensure_shadow_table(conn: sqlite3.Connection) -> None:
    """Idempotent — creates paper_trades_llm_filter if absent.

    Matches the canonical DDL in data/db.py (kept in sync). Present here
    so tests / standalone callers don't have to import data.db just to
    write a shadow row.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades_llm_filter (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at     TEXT NOT NULL,
            strategy_id     TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            bar_ts          TEXT NOT NULL,
            signal_type     TEXT NOT NULL,
            side            TEXT,
            close           REAL,
            verdict         TEXT NOT NULL,
            confidence      REAL,
            rationale       TEXT,
            factors_json    TEXT,
            model           TEXT,
            prompt_tokens   INTEGER,
            cached_tokens   INTEGER,
            output_tokens   INTEGER,
            latency_ms      INTEGER,
            failure_mode    TEXT,
            UNIQUE(strategy_id, symbol, bar_ts, signal_type)
        )
    """)


def _persist_verdict(
    conn: sqlite3.Connection,
    *,
    signal: Dict[str, Any],
    verdict: Dict[str, Any],
    usage: Dict[str, int],
    latency_ms: Optional[int],
    failure_mode: Optional[str],
    model: str,
    now_iso: Optional[str] = None,
) -> Optional[int]:
    _ensure_shadow_table(conn)
    factors = verdict.get("factors") or []
    if not isinstance(factors, list):
        factors = []
    factors_json = json.dumps(factors, sort_keys=False, ensure_ascii=False)
    with conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_trades_llm_filter
                (recorded_at, strategy_id, symbol, bar_ts, signal_type,
                 side, close,
                 verdict, confidence, rationale, factors_json,
                 model, prompt_tokens, cached_tokens, output_tokens,
                 latency_ms, failure_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso or _utc_now_iso(),
                signal.get("strategy_id"),
                signal.get("symbol"),
                signal.get("bar_ts"),
                signal.get("signal_type"),
                signal.get("side", "long"),
                _safe_float(signal.get("close")),
                verdict.get("verdict"),
                _safe_float(verdict.get("confidence")),
                (verdict.get("rationale") or "")[:1000],
                factors_json,
                model,
                int(usage.get("input_tokens", 0) or 0),
                int(usage.get("cache_read_input_tokens", 0) or 0),
                int(usage.get("output_tokens", 0) or 0),
                int(latency_ms) if latency_ms is not None else None,
                failure_mode,
            ),
        )
        return cur.lastrowid if cur.rowcount else None


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_verdict_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract a verdict JSON object from a model response. Returns None
    on any parse failure — caller falls open.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    # The model is instructed to return a bare JSON object. If it wraps
    # in markdown anyway, strip the fence.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # ```json ... ```  or  ```  ... ```
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:-1]).strip()
    # Tolerate leading prose: find the first { and balance braces.
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        if start == -1:
            return None
        cleaned = cleaned[start:]
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # One retry: cut at the last closing brace.
        last = cleaned.rfind("}")
        if last == -1:
            return None
        try:
            obj = json.loads(cleaned[: last + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    verdict = obj.get("verdict")
    if verdict not in VALID_VERDICTS:
        return None
    confidence = obj.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= confidence <= 1.0):
        return None
    rationale = obj.get("rationale") or ""
    if not isinstance(rationale, str):
        return None
    factors = obj.get("factors") or []
    if not isinstance(factors, list):
        return None
    factors = [str(f)[:64] for f in factors[:3]]
    return {
        "verdict": verdict,
        "confidence": confidence,
        "rationale": rationale,
        "factors": factors,
    }


def _fail_open(reason: str) -> Dict[str, Any]:
    return {
        "verdict": FAIL_OPEN_VERDICT,
        "confidence": FAIL_OPEN_CONFIDENCE,
        "rationale": f"fail-open: {reason}",
        "factors": [reason[:64]],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assess_signal(
    signal: Dict[str, Any],
    conn: sqlite3.Connection,
    *,
    market_context: Optional[Dict[str, Any]] = None,
    recent_news: Optional[List[Dict[str, Any]]] = None,
    earnings: Optional[List[Dict[str, Any]]] = None,
    prior_outcomes: Optional[List[Dict[str, Any]]] = None,
    model: Optional[str] = None,
    daily_cap: int = DAILY_CALL_CAP,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    api_key: Optional[str] = None,
    now_iso: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Assess a single signal with the LLM filter. Returns a verdict
    dict ``{verdict, confidence, rationale, factors}`` and writes a row
    to paper_trades_llm_filter when ``persist`` is True.

    **Fail-open** on every error path: malformed JSON, timeout, network
    error, HTTP 4xx/5xx, missing API key, daily cap exceeded — all
    return ``verdict="allow"`` with ``confidence=0.0`` and a rationale
    that names the failure mode. The caller (auto_trader) treats this
    exactly like any other allow verdict; the strategy fires unchanged.

    The persisted ``failure_mode`` column makes it possible to audit
    how often the fail-open path was taken vs. genuine allow verdicts.
    """
    model_id = model or DEFAULT_MODEL
    market_context = market_context or {}
    recent_news = recent_news or []
    earnings = earnings or []
    prior_outcomes = prior_outcomes or []

    # Daily-cap gate (fail-open when over).
    today = (now_iso or _utc_now_iso())[:10]
    if daily_cap > 0 and calls_today(conn, today=today) >= daily_cap:
        verdict = _fail_open("daily_cap_exceeded")
        if persist:
            _persist_verdict(
                conn, signal=signal, verdict=verdict,
                usage={}, latency_ms=None,
                failure_mode="daily_cap_exceeded",
                model=model_id, now_iso=now_iso,
            )
        return verdict

    key = api_key or _load_api_key()
    if not key:
        verdict = _fail_open("no_api_key")
        if persist:
            _persist_verdict(
                conn, signal=signal, verdict=verdict,
                usage={}, latency_ms=None,
                failure_mode="no_api_key",
                model=model_id, now_iso=now_iso,
            )
        return verdict

    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    user_msg = build_user_message(
        signal=signal,
        market_context=market_context,
        recent_news=recent_news,
        earnings=earnings,
        prior_outcomes=prior_outcomes,
    )
    payload = {
        "model": model_id,
        "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "system": build_system_blocks(),
        "messages": [{"role": "user", "content": user_msg}],
    }

    started = time.monotonic()
    failure_mode: Optional[str] = None
    usage: Dict[str, int] = {}
    text: str = ""

    try:
        # Record the call attempt against the daily cap BEFORE issuing
        # it, so transient network errors still burn against the cap and
        # we don't get stuck retrying the same broken endpoint.
        _record_call(conn, today=today)
        resp = _anthropic_post(
            ANTHROPIC_API, headers, payload, timeout_sec,
        )
    except requests.Timeout:
        failure_mode = "timeout"
        verdict = _fail_open(failure_mode)
        if persist:
            _persist_verdict(
                conn, signal=signal, verdict=verdict, usage={},
                latency_ms=int((time.monotonic() - started) * 1000),
                failure_mode=failure_mode,
                model=model_id, now_iso=now_iso,
            )
        return verdict
    except requests.RequestException as e:
        failure_mode = "network_error"
        log(f"llm_filter network error: {type(e).__name__}", "WARNING")
        verdict = _fail_open(failure_mode)
        if persist:
            _persist_verdict(
                conn, signal=signal, verdict=verdict, usage={},
                latency_ms=int((time.monotonic() - started) * 1000),
                failure_mode=failure_mode,
                model=model_id, now_iso=now_iso,
            )
        return verdict
    except Exception as e:
        failure_mode = "exception"
        log(f"llm_filter unexpected error: {type(e).__name__}", "WARNING")
        verdict = _fail_open(failure_mode)
        if persist:
            _persist_verdict(
                conn, signal=signal, verdict=verdict, usage={},
                latency_ms=int((time.monotonic() - started) * 1000),
                failure_mode=failure_mode,
                model=model_id, now_iso=now_iso,
            )
        return verdict

    latency_ms = int((time.monotonic() - started) * 1000)

    if getattr(resp, "status_code", 0) != 200:
        failure_mode = f"http_{resp.status_code}"
        log(f"llm_filter http {resp.status_code}", "WARNING")
        verdict = _fail_open(failure_mode)
        if persist:
            _persist_verdict(
                conn, signal=signal, verdict=verdict, usage={},
                latency_ms=latency_ms,
                failure_mode=failure_mode,
                model=model_id, now_iso=now_iso,
            )
        return verdict

    try:
        body = resp.json()
    except (ValueError, AttributeError):
        failure_mode = "json_decode_response"
        verdict = _fail_open(failure_mode)
        if persist:
            _persist_verdict(
                conn, signal=signal, verdict=verdict, usage={},
                latency_ms=latency_ms,
                failure_mode=failure_mode,
                model=model_id, now_iso=now_iso,
            )
        return verdict

    content = body.get("content") or []
    if isinstance(content, list):
        chunks = [c.get("text", "") for c in content
                  if isinstance(c, dict) and c.get("type") == "text"]
        text = "".join(chunks).strip()
    usage_block = body.get("usage") or {}
    usage = {
        "input_tokens": int(usage_block.get("input_tokens", 0) or 0),
        "cache_read_input_tokens": int(
            usage_block.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_input_tokens": int(
            usage_block.get("cache_creation_input_tokens", 0) or 0),
        "output_tokens": int(usage_block.get("output_tokens", 0) or 0),
    }

    parsed = _parse_verdict_text(text)
    if parsed is None:
        failure_mode = "malformed_json"
        verdict = _fail_open(failure_mode)
        if persist:
            _persist_verdict(
                conn, signal=signal, verdict=verdict, usage=usage,
                latency_ms=latency_ms,
                failure_mode=failure_mode,
                model=model_id, now_iso=now_iso,
            )
        return verdict

    if persist:
        _persist_verdict(
            conn, signal=signal, verdict=parsed, usage=usage,
            latency_ms=latency_ms, failure_mode=None,
            model=model_id, now_iso=now_iso,
        )
    return parsed


# ---------------------------------------------------------------------------
# Context-gathering helpers — keep auto_trader's call site small.
# These are best-effort: each helper returns an empty default on any error
# rather than propagate, since the LLM filter is itself fail-open.
# ---------------------------------------------------------------------------

def gather_recent_news(
    conn: sqlite3.Connection, symbol: str, *, hours: int = 24, limit: int = 5,
) -> List[Dict[str, Any]]:
    """Top-N news rows for `symbol` published in the last `hours`."""
    try:
        cutoff_dt = (datetime.now(timezone.utc).timestamp() - hours * 3600)
        cutoff = datetime.fromtimestamp(cutoff_dt, tz=timezone.utc).isoformat(
            timespec="seconds")
        rows = conn.execute(
            "SELECT title, publisher, published_utc, sentiment "
            "  FROM news WHERE symbol=? AND published_utc>=? "
            " ORDER BY published_utc DESC LIMIT ?",
            (symbol, cutoff, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def gather_earnings(
    conn: sqlite3.Connection, symbol: str, *, asof: Optional[date] = None,
    window_days: int = 5,
) -> List[Dict[str, Any]]:
    """Earnings rows within ±window_days of asof for symbol."""
    try:
        if asof is None:
            asof = datetime.now(timezone.utc).date()
        rows = conn.execute(
            "SELECT symbol, earnings_date FROM earnings "
            " WHERE symbol=? AND ABS(julianday(earnings_date) - "
            "       julianday(?)) <= ? "
            " ORDER BY earnings_date ASC",
            (symbol, asof.isoformat(), int(window_days)),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                dt = date.fromisoformat(d["earnings_date"])
                d["days_until"] = (dt - asof).days
            except (TypeError, ValueError):
                d["days_until"] = None
            out.append(d)
        return out
    except Exception:
        return []


def gather_prior_outcomes(
    conn: sqlite3.Connection, strategy_id: str, *, n: int = 5,
) -> List[Dict[str, Any]]:
    """Most recent `n` closed outcomes for the strategy."""
    try:
        rows = conn.execute(
            "SELECT o.exit_ts, o.exit_reason, o.return_pct, o.bars_held "
            "  FROM outcomes o "
            "  JOIN signals s ON o.signal_id = s.id "
            " WHERE s.strategy_id=? AND o.status='closed' "
            " ORDER BY o.exit_ts DESC LIMIT ?",
            (strategy_id, int(n)),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def gather_market_context(
    conn: sqlite3.Connection, *, asof: Optional[date] = None,
) -> Dict[str, Any]:
    """Best-effort market context: latest daily_reports.market_regime,
    latest macro snapshot, top notable movers for the day. All
    individual lookups are wrapped — anything missing degrades to an
    empty payload."""
    out: Dict[str, Any] = {"regime": None, "macro_strip": {}, "notable_movers": []}
    try:
        if asof is not None:
            row = conn.execute(
                "SELECT market_regime FROM daily_reports "
                " WHERE report_date <= ? ORDER BY report_date DESC LIMIT 1",
                (asof.isoformat(),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT market_regime FROM daily_reports "
                " ORDER BY report_date DESC LIMIT 1"
            ).fetchone()
        if row is not None and row["market_regime"]:
            out["regime"] = row["market_regime"]
    except Exception:
        pass
    try:
        for series in ("VIX", "DGS10", "DFF"):
            r = conn.execute(
                "SELECT value, bar_date FROM macro "
                " WHERE series_id=? AND value IS NOT NULL "
                " ORDER BY bar_date DESC LIMIT 1",
                (series,),
            ).fetchone()
            if r is not None:
                out["macro_strip"][series] = {
                    "value": r["value"], "bar_date": r["bar_date"],
                }
    except Exception:
        pass
    try:
        if asof is not None:
            rows = conn.execute(
                "SELECT symbol, ret_1d_pct, rvol_vs_20d FROM snapshots "
                " WHERE snapshot_date=? AND ret_1d_pct IS NOT NULL "
                " ORDER BY ABS(ret_1d_pct) DESC LIMIT 5",
                (asof.isoformat(),),
            ).fetchall()
            out["notable_movers"] = [dict(r) for r in rows]
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Recent verdicts — backs the dashboard route
# ---------------------------------------------------------------------------

def recent_verdicts(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return the N most-recent shadow rows for the dashboard card.

    Joins back to `signals` so the UI can show the signal's close + bar_ts
    alongside the verdict. Rows where the signal isn't in the signals
    table (rare — e.g. a shadow row written before the signal insert)
    are still returned, just with the signal-side fields empty.
    """
    _ensure_shadow_table(conn)
    rows = conn.execute(
        "SELECT f.id, f.recorded_at, f.strategy_id, f.symbol, f.bar_ts, "
        "       f.signal_type, f.side, f.close, f.verdict, f.confidence, "
        "       f.rationale, f.factors_json, f.model, f.failure_mode, "
        "       f.latency_ms, "
        "       s.close AS signal_close, s.bar_interval "
        "  FROM paper_trades_llm_filter f "
        "  LEFT JOIN signals s "
        "    ON s.strategy_id = f.strategy_id "
        "   AND s.symbol      = f.symbol "
        "   AND s.bar_ts      = f.bar_ts "
        "   AND s.signal_type = f.signal_type "
        " ORDER BY f.id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["factors"] = json.loads(d.get("factors_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            d["factors"] = []
        d.pop("factors_json", None)
        out.append(d)
    return out
