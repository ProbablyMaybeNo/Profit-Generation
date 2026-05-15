"""
llm_codegen.py — LLM-assisted compute_fn generator.

Turns an English strategy description into a Python `compute_<name>(df) ->
DataFrame with long_entry/long_exit columns` function via a local Ollama
model (default qwen2.5-coder:14b).

Safety: the generated code is parsed via AST and rejected if it imports
anything outside an allowlist (pandas, numpy, math, datetime), uses
forbidden names (eval, exec, compile, open, __import__), or accesses
dunder attributes. Then it is exec'd in a fresh namespace and smoke-tested
against a synthetic 100-bar DataFrame.

CLI:
  py -3.13 -m monitoring.llm_codegen <fn_name>  --entry "..." --exit "..."
"""

import argparse
import ast
import json
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Callable, Dict, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b")
OLLAMA_TIMEOUT_SEC = int(os.environ.get("OLLAMA_TIMEOUT_SEC", "180"))

ALLOWED_IMPORT_ROOTS = {"pandas", "numpy", "math", "datetime", "typing"}
FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "globals", "locals",
    "input", "vars", "exit", "quit", "help",
}

PROMPT_TEMPLATE = textwrap.dedent("""\
    You are writing a Python function that computes trading signals on
    daily OHLCV bars. Follow the EXACT pattern of the examples — same
    signature, same column conventions, vectorized pandas only.

    PATTERN
    -------
    - Signature: `def {fn_name}(df: pd.DataFrame) -> pd.DataFrame`
    - Input columns (lowercase): open, high, low, close, volume
    - Output: df.copy() with two boolean columns added — `long_entry`,
      `long_exit`
    - Use `.shift(1)` so signals depend only on PRIOR bars (no look-ahead)
    - Use `.fillna(False)` on the boolean columns
    - Vectorized pandas / numpy only — NO for/while loops over rows
    - Imports allowed: pandas as pd, numpy as np
    - Do NOT use eval/exec/open/__import__ or any I/O

    EXAMPLE 1 — "Buy on 5-Day Low: long if close < lowest low of prev 5 bars; exit when close > prev bar's high"

    def compute_5day_low(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        lowest_5 = df["low"].rolling(5).min().shift(1)
        prev_high = df["high"].shift(1)
        out["long_entry"] = (df["close"] < lowest_5).fillna(False)
        out["long_exit"] = (df["close"] > prev_high).fillna(False)
        return out

    EXAMPLE 2 — "3-Bar Low: long if close < lowest low of prev 3 bars; exit when close > highest high of prev 7 bars"

    def compute_3bar_low(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        lowest_3 = df["low"].rolling(3).min().shift(1)
        highest_7 = df["high"].rolling(7).max().shift(1)
        out["long_entry"] = (df["close"] < lowest_3).fillna(False)
        out["long_exit"] = (df["close"] > highest_7).fillna(False)
        return out

    EXAMPLE 3 — "RSI(2) Oversold: long when 2-period RSI < 10 and close > 200-day SMA; exit when 2-period RSI > 70"

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

    NOW WRITE THE FUNCTION
    ----------------------
    Name: {fn_name}
    Entry rules: {entry_rules}
    Exit rules: {exit_rules}
    Risk management (informational, not coded): {risk_management}

    Output ONLY the Python function source. No markdown fences. No
    explanation before or after. Start with `def {fn_name}(`.
    """)


# ---------- prompt + LLM call ----------

def build_prompt(fn_name: str, *, entry_rules: str, exit_rules: str,
                 risk_management: str = "") -> str:
    return PROMPT_TEMPLATE.format(
        fn_name=fn_name,
        entry_rules=entry_rules.strip() or "(unspecified)",
        exit_rules=exit_rules.strip() or "(unspecified)",
        risk_management=risk_management.strip() or "(none)",
    )


def _ollama_post(url: str, payload: Dict, timeout: float):
    """Indirection seam — tests mock this."""
    return requests.post(url, json=payload, timeout=timeout)


def call_ollama(prompt: str, *, model: Optional[str] = None,
                temperature: float = 0.1) -> str:
    url = f"{OLLAMA_URL}/api/generate"
    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": 800},
    }
    resp = _ollama_post(url, payload, OLLAMA_TIMEOUT_SEC)
    if resp.status_code != 200:
        raise RuntimeError(f"ollama {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    text = body.get("response") or ""
    if not text.strip():
        raise RuntimeError("ollama returned empty response")
    return text


# ---------- code extraction + validation ----------

_FENCE_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)


def extract_code(raw: str, fn_name: str) -> str:
    """Strip markdown fences, isolate the function definition + its trailing block."""
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()

    # Find the start of `def fn_name(`
    needle = f"def {fn_name}("
    start = text.find(needle)
    if start == -1:
        raise ValueError(f"function `{fn_name}` not found in LLM output")
    body = text[start:]

    # Walk backward to grab any `import` lines that immediately precede the def.
    preamble_lines = []
    for line in text[:start].splitlines()[::-1]:
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            preamble_lines.insert(0, line)
        elif not stripped:
            continue
        else:
            break

    return ("\n".join(preamble_lines) + ("\n" if preamble_lines else "") + body).strip() + "\n"


def validate_ast(code: str) -> None:
    """Raise ValueError if the code uses forbidden imports / names / dunders."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"syntax error: {e}") from e
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORT_ROOTS:
                    issues.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORT_ROOTS:
                issues.append(f"forbidden import from: {node.module}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            issues.append(f"forbidden name: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            issues.append(f"dunder attribute access: {node.attr}")
    if issues:
        raise ValueError("AST validation failed: " + "; ".join(issues))


def smoke_test(code: str, fn_name: str, *, n_bars: int = 100) -> Callable:
    """exec the code, call the function on a synthetic frame, return the callable."""
    import numpy as np
    import pandas as pd

    ns: Dict = {"pd": pd, "np": np}
    try:
        exec(compile(code, "<llm_generated>", "exec"), ns)
    except Exception as e:
        raise ValueError(f"exec failed: {e}") from e

    fn = ns.get(fn_name)
    if not callable(fn):
        raise ValueError(f"function `{fn_name}` not defined in generated code")

    rng = np.random.default_rng(42)
    closes = 100 + rng.normal(0, 1, n_bars).cumsum()
    df = pd.DataFrame({
        "open":   closes + rng.normal(0, 0.2, n_bars),
        "high":   closes + np.abs(rng.normal(0, 0.5, n_bars)),
        "low":    closes - np.abs(rng.normal(0, 0.5, n_bars)),
        "close":  closes,
        "volume": rng.integers(1_000_000, 5_000_000, n_bars),
    })
    df.index = pd.date_range("2024-01-01", periods=n_bars, freq="D")

    try:
        result = fn(df)
    except Exception as e:
        raise ValueError(f"smoke call raised: {e}") from e
    if not isinstance(result, pd.DataFrame):
        raise ValueError(f"`{fn_name}` returned {type(result).__name__}, expected DataFrame")
    for col in ("long_entry", "long_exit"):
        if col not in result.columns:
            raise ValueError(f"output missing column `{col}`")
        if result[col].dtype != bool:
            raise ValueError(f"`{col}` must be boolean, got {result[col].dtype}")
    return fn


# ---------- top-level orchestration ----------

def generate_compute_fn(
    fn_name: str,
    *,
    entry_rules: str,
    exit_rules: str,
    risk_management: str = "",
    model: Optional[str] = None,
    temperature: float = 0.1,
) -> str:
    """End-to-end: build prompt → call Ollama → extract → validate → smoke. Returns the code."""
    prompt = build_prompt(
        fn_name, entry_rules=entry_rules,
        exit_rules=exit_rules, risk_management=risk_management,
    )
    raw = call_ollama(prompt, model=model, temperature=temperature)
    code = extract_code(raw, fn_name)
    validate_ast(code)
    smoke_test(code, fn_name)
    return code


def fn_name_from_strategy_id(strategy_id: str) -> str:
    """Convert e.g. 'botnet101-buy-5day-low' → 'compute_buy_5day_low_botnet101'."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", strategy_id).strip("_").lower()
    if not cleaned:
        cleaned = "strategy"
    if not cleaned.startswith("compute_"):
        cleaned = "compute_" + cleaned
    return cleaned


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("fn_name", help="e.g. compute_my_strategy")
    parser.add_argument("--entry", required=True, help="entry rules in plain English")
    parser.add_argument("--exit", required=True, help="exit rules in plain English")
    parser.add_argument("--risk", default="", help="risk-management notes (informational)")
    parser.add_argument("--model", default=None, help=f"override OLLAMA_MODEL ({OLLAMA_MODEL})")
    parser.add_argument("--temperature", type=float, default=0.1)
    args = parser.parse_args()

    log(f"generating {args.fn_name} via {args.model or OLLAMA_MODEL}", "INFO")
    code = generate_compute_fn(
        args.fn_name,
        entry_rules=args.entry, exit_rules=args.exit,
        risk_management=args.risk, model=args.model, temperature=args.temperature,
    )
    print(code)
