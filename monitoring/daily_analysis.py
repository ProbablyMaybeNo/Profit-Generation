"""
daily_analysis.py — Deep LLM analysis of the trading day, delivered to Telegram.

Usage:
  python -m monitoring.daily_analysis            # today
  python -m monitoring.daily_analysis 2026-06-03 # specific date

Flow:
  1. Assemble a structured data packet (reuses _report_data helpers).
  2. Send to Claude via _anthropic_post (same pattern as codegen_claude).
  3. Deliver the analysis to Telegram (chunked, plain text).

Degrades gracefully if ANTHROPIC_API_KEY / credentials.json.anthropic.api_key
is absent or invalid — sends a note explaining the situation rather than
crashing.

Model: tries 'claude-opus-4-8' first; falls back to DEFAULT_CLAUDE_MODEL
from codegen_claude on a 404/model-not-found error.

Prompt caching: the static system prompt is flagged cache_control ephemeral
so repeated daily runs amortise the preamble cost.

MAX_OUTPUT_TOKENS = 3500 (generous for the 6-section analysis).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402
from monitoring import _report_data as rd  # noqa: E402
from monitoring import telegram_alerter  # noqa: E402
from monitoring.codegen_claude import (  # noqa: E402
    ANTHROPIC_API,
    ANTHROPIC_VERSION,
    DEFAULT_CLAUDE_MODEL,
    _load_api_key,
    _anthropic_post,
)
from monitoring.daily_brief import chunk_report, TELEGRAM_MAX  # noqa: E402

# Try the newest Opus first; fall back to whatever codegen_claude defaults to.
ANALYSIS_MODEL_PREFERRED = "claude-opus-4-8"
MAX_OUTPUT_TOKENS = 3500
CLAUDE_TIMEOUT_SEC = 120

SYSTEM_PROMPT = """\
You are an expert quantitative trading-systems analyst and site-reliability \
engineer reviewing one day of a live paper-trading system. Your job is to \
identify concrete improvements — both performance optimisations and bugs.

Return EXACTLY these six sections (use the labels verbatim):

(a) MARKET & SYSTEM OVERVIEW
Brief summary of market regime + what the system did today.

(b) WHAT WORKED
Strategies, setups, or mechanics that performed well. Be specific.

(c) WHAT UNDERPERFORMED / LOST MONEY AND WHY
Strategies or mechanics with poor outcomes today. Identify root causes where \
data allows.

(d) BUGS & ERRORS DETECTED
Log errors, NULL fields that should be populated, broker rejects, ledger drift, \
things that were expected to fire but did not. Cite specific evidence from the \
data packet.

(e) OPTIMIZATION NEXT-STEPS (ranked, most impactful first)
Concrete changes to improve edge, sizing, or execution. Each item: what to \
change and why.

(f) DEBUG NEXT-STEPS / FIXES (ranked, most urgent first)
Concrete fixes for detected bugs or data quality issues. Each item must have \
enough detail to become a task card: what is broken, where to look, what the \
fix should do.

Rules:
- Ground every claim in the data packet provided.
- Do not fabricate numbers not present in the data.
- Keep the entire response under 3500 tokens.
- Plain text only — no markdown fences, no bullet dashes that could be \
  misinterpreted, just the section labels and prose/numbered lists.
"""


def _build_data_packet(conn, as_of) -> str:
    """Serialise the day's data into a compact JSON string for the LLM."""
    h = rd.get_header(conn, as_of)
    a = rd.get_activity(conn, as_of)
    t = rd.get_trades(conn, as_of)
    i = rd.get_intraday_by_strategy(conn, as_of)
    r = rd.get_risk(conn, as_of)
    o = rd.get_outcomes(conn, as_of)
    n = rd.get_notable(conn, as_of)

    log_dir = ROOT / "logs"
    errors = rd.get_recent_errors(log_dir)
    skips = rd.get_skip_distribution(conn)
    perf = rd.get_strategy_performance(conn)
    drift = rd.get_open_vs_broker_note(conn)

    packet = {
        "as_of": rd._d(as_of),
        "header": h,
        "activity": a,
        "trades": t,
        "intraday_by_strategy": i,
        "risk": r,
        "outcomes": o,
        "notable": n,
        "recent_errors_from_logs": errors[-50:] if errors else [],
        "skip_distribution_5d": skips,
        "strategy_performance_30d": perf,
        "open_vs_broker": drift,
    }
    return json.dumps(packet, indent=2, default=str)


def _call_claude(data_packet: str, api_key: str, model: str) -> str:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": (
                    "Here is today's trading system data packet. "
                    "Please analyse it per your instructions.\n\n"
                    "DATA PACKET:\n" + data_packet
                ),
            }
        ],
    }
    resp = _anthropic_post(ANTHROPIC_API, headers, payload, float(CLAUDE_TIMEOUT_SEC))
    if resp.status_code != 200:
        raise RuntimeError(
            f"Anthropic API {resp.status_code}: {resp.text[:400]}"
        )
    body = resp.json()
    content = body.get("content") or []
    chunks = [c.get("text", "") for c in content if c.get("type") == "text"]
    text = "".join(chunks).strip()
    if not text:
        raise RuntimeError("Anthropic returned empty response content")
    usage = body.get("usage") or {}
    log(
        f"daily_analysis: Claude usage — "
        f"input={usage.get('input_tokens',0)} "
        f"output={usage.get('output_tokens',0)} "
        f"cache_read={usage.get('cache_read_input_tokens',0)} "
        f"cache_create={usage.get('cache_creation_input_tokens',0)}",
        "INFO",
    )
    return text


def _record_spend(conn, usage: dict) -> None:
    try:
        from monitoring import api_budget
        # Approximate cost at Opus 4 pricing: $15/$75 per M tokens input/output
        input_tok = int(usage.get("input_tokens", 0) or 0)
        output_tok = int(usage.get("output_tokens", 0) or 0)
        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_create = int(usage.get("cache_creation_input_tokens", 0) or 0)
        usd = (
            (input_tok / 1_000_000) * 15.0
            + (output_tok / 1_000_000) * 75.0
            + (cache_read / 1_000_000) * 1.5
            + (cache_create / 1_000_000) * 18.75
        )
        if usd > 0:
            api_budget.record_spend(conn, usd, provider="anthropic", calls=1)
    except Exception as e:
        log(f"daily_analysis: spend recording skipped: {e}", "WARNING")


def run_analysis(conn, as_of, *, prefix: str = "") -> bool:
    day = rd._d(as_of)

    # Build packet
    try:
        packet = _build_data_packet(conn, as_of)
    except Exception as exc:
        msg = f"=== DAILY ANALYSIS — {day} ===\nData assembly failed: {exc}"
        log(f"daily_analysis: data assembly error: {exc}", "ERROR")
        telegram_alerter.send_message((prefix + msg) if prefix else msg, parse_mode=None)
        return False

    # Load API key
    api_key = _load_api_key()
    if not api_key:
        msg = (
            f"=== DAILY ANALYSIS — {day} ===\n"
            "Analysis unavailable: ANTHROPIC_API_KEY is not set and "
            "credentials.json has no 'anthropic.api_key' entry. "
            "Add the key to enable LLM analysis."
        )
        log("daily_analysis: no Anthropic API key configured", "WARNING")
        ok = telegram_alerter.send_message((prefix + msg) if prefix else msg, parse_mode=None)
        return ok

    # Try preferred model, then fall back to default
    analysis_text: Optional[str] = None
    model_used = ANALYSIS_MODEL_PREFERRED
    last_error: Optional[str] = None

    for model in [ANALYSIS_MODEL_PREFERRED, DEFAULT_CLAUDE_MODEL]:
        try:
            analysis_text = _call_claude(packet, api_key, model)
            model_used = model
            break
        except RuntimeError as exc:
            last_error = str(exc)
            err_lower = last_error.lower()
            # model not found → try next; other errors → abort
            if "404" in last_error or "not_found" in err_lower or "invalid_model" in err_lower:
                log(f"daily_analysis: model {model} not available, trying fallback", "WARNING")
                continue
            log(f"daily_analysis: Claude call failed: {exc}", "ERROR")
            break
        except Exception as exc:
            last_error = str(exc)
            log(f"daily_analysis: unexpected error calling Claude: {exc}", "ERROR")
            break

    if analysis_text is None:
        msg = (
            f"=== DAILY ANALYSIS — {day} ===\n"
            f"Analysis unavailable: Claude API call failed.\n"
            f"Error: {last_error or 'unknown'}"
        )
        ok = telegram_alerter.send_message((prefix + msg) if prefix else msg, parse_mode=None)
        return ok

    # Compose header + analysis
    header = f"=== DAILY ANALYSIS — {day} (model: {model_used}) ===\n\n"
    full_text = header + analysis_text
    chunks = chunk_report(full_text, max_chars=TELEGRAM_MAX)

    success = True
    for chunk in chunks:
        out = (prefix + chunk) if (prefix and chunk == chunks[0]) else chunk
        ok = telegram_alerter.send_message(out, parse_mode=None)
        if not ok:
            log(f"daily_analysis: telegram send failed for chunk ({len(chunk)} chars)",
                "WARNING")
            success = False

    return success


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Send the LLM deep-analysis report to Telegram."
    )
    parser.add_argument("date", nargs="?", default=date.today().isoformat())
    parser.add_argument("--prefix", default="",
                        help="Optional prefix prepended to the first message chunk.")
    parser.add_argument("--no-send", action="store_true",
                        help="Print to stdout only; do not send.")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.date)
    conn = db.init_db()
    try:
        if args.no_send:
            # Build and print the data packet for inspection
            packet = _build_data_packet(conn, as_of)
            buf = getattr(sys.stdout, "buffer", None)
            if buf:
                buf.write((packet + "\n").encode("utf-8", errors="replace"))
                buf.flush()
            else:
                print(packet)
            return

        ok = run_analysis(conn, as_of, prefix=args.prefix)
        if ok:
            log(f"daily_analysis: delivered for {as_of}", "SUCCESS")
        else:
            log(f"daily_analysis: delivery had failures for {as_of}", "WARNING")
    except Exception as exc:
        log(f"daily_analysis: unhandled error: {exc}", "ERROR")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
