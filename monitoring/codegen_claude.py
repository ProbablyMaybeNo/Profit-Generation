"""
codegen_claude.py — Claude-API counterpart to llm_codegen (Ollama path).

Drop-in replacement for the Ollama codegen path. Same input contract
(entry / exit / risk rules from an UNTESTED record) and same output
contract (a single Python function body that passes the AST validator
and smoke-test from `llm_codegen`).

The system prompt + few-shot examples are emitted as a SINGLE cache-
controlled block so Anthropic's prompt-caching can amortise the
~3-4k token preamble across many codegen calls. Per the global
CLAUDE.md, prompt caching is mandatory.

Network seam: `_anthropic_post` is the indirection tests mock. The
real call uses the `requests` library against the Anthropic Messages
API directly (no SDK dependency — same pattern as notion_writer).

CLI:
  py -3.13 -m monitoring.codegen_claude <fn_name>  --entry "..." --exit "..."

Env:
  ANTHROPIC_API_KEY  — required (also read from credentials.json.anthropic.api_key)
  CLAUDE_MODEL       — defaults to claude-opus-4-7 per workspace standard
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from monitoring import llm_codegen  # noqa: E402

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7-1m")
CLAUDE_TIMEOUT_SEC = int(os.environ.get("CLAUDE_TIMEOUT_SEC", "180"))
MAX_OUTPUT_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "1500"))

# Few-shot examples — kept identical to the Ollama path so a swap is
# behaviour-compatible. The system prompt + examples both live inside
# one cache_control block to maximise hit-rate.

SYSTEM_PROMPT = textwrap.dedent("""\
    You are writing a Python function that computes trading signals on
    daily OHLCV bars. Follow the EXACT pattern of the examples — same
    signature, same column conventions, vectorized pandas only.

    PATTERN
    -------
    - Signature: `def <fn_name>(df: pd.DataFrame) -> pd.DataFrame`
    - Input columns (lowercase): open, high, low, close, volume
    - Output: df.copy() with two boolean columns added — `long_entry`,
      `long_exit`
    - Use `.shift(1)` so signals depend only on PRIOR bars (no look-ahead)
    - Use `.fillna(False)` on the boolean columns
    - Vectorized pandas / numpy only — NO for/while loops over rows
    - Imports allowed: pandas as pd, numpy as np
    - Do NOT use eval/exec/open/__import__ or any I/O

    Output ONLY the Python function source. No markdown fences. No
    explanation before or after. Start with `def <fn_name>(`.
    """)

FEW_SHOT_EXAMPLES: List[Tuple[str, str, str]] = [
    (
        "Buy on 5-Day Low: long if close < lowest low of prev 5 bars; "
        "exit when close > prev bar's high",
        "compute_5day_low",
        textwrap.dedent("""\
            def compute_5day_low(df: pd.DataFrame) -> pd.DataFrame:
                out = df.copy()
                lowest_5 = df["low"].rolling(5).min().shift(1)
                prev_high = df["high"].shift(1)
                out["long_entry"] = (df["close"] < lowest_5).fillna(False)
                out["long_exit"] = (df["close"] > prev_high).fillna(False)
                return out
            """),
    ),
    (
        "3-Bar Low: long if close < lowest low of prev 3 bars; exit when "
        "close > highest high of prev 7 bars",
        "compute_3bar_low",
        textwrap.dedent("""\
            def compute_3bar_low(df: pd.DataFrame) -> pd.DataFrame:
                out = df.copy()
                lowest_3 = df["low"].rolling(3).min().shift(1)
                highest_7 = df["high"].rolling(7).max().shift(1)
                out["long_entry"] = (df["close"] < lowest_3).fillna(False)
                out["long_exit"] = (df["close"] > highest_7).fillna(False)
                return out
            """),
    ),
    (
        "RSI(2) Oversold: long when 2-period RSI < 10 and close > 200-day SMA; "
        "exit when 2-period RSI > 70",
        "compute_rsi2_oversold",
        textwrap.dedent("""\
            def compute_rsi2_oversold(df: pd.DataFrame) -> pd.DataFrame:
                out = df.copy()
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(2).mean()
                loss = (-delta.clip(upper=0)).rolling(2).mean()
                rs = gain / loss.replace(0, np.nan)
                rsi2 = 100 - (100 / (1 + rs))
                sma200 = df["close"].rolling(200).mean()
                out["long_entry"] = ((rsi2 < 10) & (df["close"] > sma200)).fillna(False)
                out["long_exit"] = (rsi2 > 70).fillna(False)
                return out
            """),
    ),
    (
        "Donchian Breakout(20): long on close above prev 20-day high; "
        "exit on close below prev 10-day low",
        "compute_donchian_breakout_20",
        textwrap.dedent("""\
            def compute_donchian_breakout_20(df: pd.DataFrame) -> pd.DataFrame:
                out = df.copy()
                high20 = df["high"].rolling(20).max().shift(1)
                low10 = df["low"].rolling(10).min().shift(1)
                out["long_entry"] = (df["close"] > high20).fillna(False)
                out["long_exit"] = (df["close"] < low10).fillna(False)
                return out
            """),
    ),
    (
        "Turn-of-Month: long on the last trading day of the month; exit on "
        "the third trading day of the following month",
        "compute_turn_of_month",
        textwrap.dedent("""\
            def compute_turn_of_month(df: pd.DataFrame) -> pd.DataFrame:
                out = df.copy()
                idx = df.index
                day = idx.day
                month = idx.month
                next_month = pd.Series(month, index=idx).shift(-1)
                is_last = (next_month != month).fillna(False)
                out["long_entry"] = is_last.values.astype(bool)
                out["long_exit"] = (day == 3).fillna(False) if hasattr(day, "fillna") else pd.Series(day == 3, index=idx).fillna(False).values
                return out
            """),
    ),
]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _few_shot_block() -> str:
    """Render the few-shot examples as a single contiguous string so the
    whole preamble (system + examples) is one cache_control block."""
    parts: List[str] = ["EXAMPLES", "--------", ""]
    for desc, name, code in FEW_SHOT_EXAMPLES:
        parts.append(f"# Description: {desc}")
        parts.append(f"# Name: {name}")
        parts.append("")
        parts.append(code.rstrip())
        parts.append("")
    return "\n".join(parts)


def build_system_blocks() -> List[Dict]:
    """The system prompt block, cache_control flagged. The whole thing
    is static across every codegen call so it caches cleanly."""
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT + "\n\n" + _few_shot_block(),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def build_user_message(fn_name: str, *, entry_rules: str, exit_rules: str,
                        risk_management: str = "") -> str:
    return textwrap.dedent(f"""\
        NOW WRITE THE FUNCTION
        ----------------------
        Name: {fn_name}
        Entry rules: {entry_rules.strip() or "(unspecified)"}
        Exit rules: {exit_rules.strip() or "(unspecified)"}
        Risk management (informational, not coded): {risk_management.strip() or "(none)"}
        """)


def cache_key(fn_name: str, *, entry_rules: str, exit_rules: str,
              risk_management: str = "") -> str:
    """Deterministic key over the variable portion of the prompt. Same
    (entry, exit, risk, fn_name) → same cache_key. Used for stability
    tests and the optional on-disk response cache."""
    payload = json.dumps({
        "fn": fn_name,
        "entry": entry_rules.strip(),
        "exit": exit_rules.strip(),
        "risk": risk_management.strip(),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Anthropic API call
# ---------------------------------------------------------------------------

def _load_api_key() -> str:
    """Env var > credentials.json.anthropic.api_key."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        from config.utils import load_credentials
        section = load_credentials("anthropic")
        if isinstance(section, dict):
            return section.get("api_key") or ""
    except Exception:
        pass
    return ""


def _anthropic_post(
    url: str, headers: Dict[str, str], payload: Dict, timeout: float,
):
    """Indirection seam — tests mock this."""
    return requests.post(url, headers=headers, json=payload, timeout=timeout)


def call_claude(
    fn_name: str,
    *,
    entry_rules: str,
    exit_rules: str,
    risk_management: str = "",
    model: Optional[str] = None,
    temperature: float = 0.1,
    api_key: Optional[str] = None,
) -> Dict:
    """One Anthropic Messages API call. Returns:

      {text: str, cache_read_tokens: int, cache_creation_tokens: int,
       input_tokens: int, output_tokens: int}

    Raises RuntimeError on non-200 or empty content.
    """
    key = api_key or _load_api_key()
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set and credentials.json.anthropic.api_key "
            "is missing."
        )
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    user_msg = build_user_message(
        fn_name,
        entry_rules=entry_rules,
        exit_rules=exit_rules,
        risk_management=risk_management,
    )
    payload = {
        "model": model or DEFAULT_CLAUDE_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": temperature,
        "system": build_system_blocks(),
        "messages": [{"role": "user", "content": user_msg}],
    }
    resp = _anthropic_post(ANTHROPIC_API, headers, payload, CLAUDE_TIMEOUT_SEC)
    if resp.status_code != 200:
        raise RuntimeError(
            f"anthropic {resp.status_code}: {resp.text[:300]}"
        )
    body = resp.json()
    content = body.get("content") or []
    if not content or not isinstance(content, list):
        raise RuntimeError("anthropic returned no content block")
    # content is a list of {type: 'text', text: '...'}.
    chunks = [c.get("text", "") for c in content if c.get("type") == "text"]
    text = "".join(chunks).strip()
    if not text:
        raise RuntimeError("anthropic returned empty text")
    usage = body.get("usage") or {}
    return {
        "text": text,
        "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
    }


# ---------------------------------------------------------------------------
# Public API — mirrors llm_codegen.generate_compute_fn
# ---------------------------------------------------------------------------

def generate_compute_fn(
    fn_name: str,
    *,
    entry_rules: str,
    exit_rules: str,
    risk_management: str = "",
    model: Optional[str] = None,
    temperature: float = 0.1,
    api_key: Optional[str] = None,
    on_usage: Optional[Callable[[Dict], None]] = None,
) -> str:
    """End-to-end: build prompt → call Claude → extract → validate → smoke.

    Returns the code string. Reuses the Ollama-path's extract_code,
    validate_ast, and smoke_test helpers — output schema is identical.

    `on_usage` is an optional callback invoked with the usage dict
    (token counts incl. cache_read / cache_creation). Used by the
    budget gate (4.3.3) and the A/B harness (4.3.2).
    """
    result = call_claude(
        fn_name,
        entry_rules=entry_rules, exit_rules=exit_rules,
        risk_management=risk_management,
        model=model, temperature=temperature, api_key=api_key,
    )
    if on_usage is not None:
        try:
            on_usage(result)
        except Exception as e:
            log(f"on_usage callback failed (non-fatal): {e}", "WARNING")
    code = llm_codegen.extract_code(result["text"], fn_name)
    llm_codegen.validate_ast(code)
    llm_codegen.smoke_test(code, fn_name)
    return code


def fn_name_from_strategy_id(strategy_id: str) -> str:
    """Delegate to the Ollama-path helper so the function naming is
    identical between providers."""
    return llm_codegen.fn_name_from_strategy_id(strategy_id)


# ---------------------------------------------------------------------------
# Budget-gated wrapper (4.3.3)
# ---------------------------------------------------------------------------

def generate_with_budget_gate(
    fn_name: str,
    *,
    entry_rules: str,
    exit_rules: str,
    risk_management: str = "",
    model: Optional[str] = None,
    temperature: float = 0.1,
    api_key: Optional[str] = None,
    conn=None,
    cap_usd: Optional[float] = None,
    fallback_fn: Optional[Callable] = None,
    alert_fn: Optional[Callable[[str], bool]] = None,
    now_fn: Optional[Callable] = None,
) -> Dict:
    """Budget-gated Claude codegen.

    Returns {provider, code} where provider ∈ {"claude", "ollama"}.

    Flow:
      1. Pre-check: monitoring.api_budget.assert_can_spend. If exhausted,
         fire a Telegram alert (once per day) and fall back to Ollama.
      2. Call Claude. The usage callback persists today's spend so the
         next call sees the updated total.
      3. On any Claude error (network, content rejection), no fallback —
         the caller decides what to do. Budget exhaustion is the only
         path that triggers automatic fallback.
    """
    from monitoring import api_budget
    if conn is None:
        from data import db
        conn = db.init_db()
        own_conn = True
    else:
        own_conn = False

    try:
        check = api_budget.can_spend(
            conn, cap_usd=cap_usd, now_fn=now_fn,
        )
        if not check["ok"]:
            _maybe_alert_budget(
                conn=conn, check=check,
                alert_fn=alert_fn, now_fn=now_fn,
            )
            if fallback_fn is None:
                fallback_fn = lambda: llm_codegen.generate_compute_fn(
                    fn_name, entry_rules=entry_rules,
                    exit_rules=exit_rules,
                    risk_management=risk_management,
                )
            code = fallback_fn()
            return {"provider": "ollama", "code": code,
                    "budget": check}

        recorder = api_budget.make_usage_recorder(conn, now_fn=now_fn)
        code = generate_compute_fn(
            fn_name, entry_rules=entry_rules, exit_rules=exit_rules,
            risk_management=risk_management,
            model=model, temperature=temperature, api_key=api_key,
            on_usage=recorder,
        )
        return {"provider": "claude", "code": code,
                "budget": check}
    finally:
        if own_conn:
            conn.close()


_BUDGET_ALERT_META_PREFIX = "api_budget.alerted:"


def _maybe_alert_budget(
    *,
    conn,
    check: Dict,
    alert_fn: Optional[Callable[[str], bool]] = None,
    now_fn: Optional[Callable] = None,
) -> bool:
    """Fire a Telegram alert once per UTC day on budget exhaustion.

    Returns True iff an alert was actually sent. The dedupe key is
    `api_budget.alerted:<provider>:<date>` in the meta table.
    """
    key = f"{_BUDGET_ALERT_META_PREFIX}anthropic:{check['today']}"
    existing = conn.execute(
        "SELECT value FROM meta WHERE key=?", (key,),
    ).fetchone()
    if existing is not None:
        return False
    if alert_fn is None:
        try:
            from monitoring.telegram_alerter import send_message as alert_fn  # type: ignore[no-redef]
        except Exception:
            return False
    text = (
        "\U000026A0\U0000FE0F *Claude API budget exhausted* "
        f"({check['today']}): spent ${check['spent_usd']:.4f} / "
        f"cap ${check['cap_usd']:.4f}. Codegen falling back to Ollama."
    )
    try:
        sent = bool(alert_fn(text))
    except Exception as e:
        log(f"budget alert send failed: {e}", "WARNING")
        return False
    if sent:
        with conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, "1"),
            )
    return sent


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fn_name", help="e.g. compute_my_strategy")
    parser.add_argument("--entry", required=True)
    parser.add_argument("--exit", required=True)
    parser.add_argument("--risk", default="")
    parser.add_argument("--model", default=None,
                        help=f"override CLAUDE_MODEL ({DEFAULT_CLAUDE_MODEL})")
    parser.add_argument("--temperature", type=float, default=0.1)
    args = parser.parse_args()

    log(f"generating {args.fn_name} via Claude "
        f"({args.model or DEFAULT_CLAUDE_MODEL})", "INFO")
    code = generate_compute_fn(
        args.fn_name,
        entry_rules=args.entry, exit_rules=args.exit,
        risk_management=args.risk,
        model=args.model, temperature=args.temperature,
    )
    print(code)


if __name__ == "__main__":
    main()
