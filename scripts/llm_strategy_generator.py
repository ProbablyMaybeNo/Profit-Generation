"""
llm_strategy_generator.py — Use a local Ollama LLM to invent candidate
trading strategies and append them to records.jsonl as UNTESTED.

Replaces the deprecated Reddit/forum scraper. Reddit closed self-service
API access 2026-05; here we use an LLM as a source of *diverse strategy
ideas*. The validator (`batch_validate`) is the quality filter — this
script just emits candidates with concrete, codeable rules.

Pipeline:
  1. Build a prompt asking Ollama for N JSON strategies in a category.
  2. Parse the response as a JSON list. Each item must have
     `strategy_id`, `title`, `entry_rules`, `exit_rules`,
     `risk_management`.
  3. Drop malformed items; keep at most --count items per run.
  4. Dedupe against existing strategy_ids in records.jsonl.
  5. Append accepted items as UNTESTED records matching the schema
     codegen_strategy + batch_validate expect.

CLI:
  py -3.13 scripts/llm_strategy_generator.py --category mean-reversion --count 10
  py -3.13 scripts/llm_strategy_generator.py --category breakout --count 5 \
      --avoid "Bollinger,RSI,MACD"
  py -3.13 scripts/llm_strategy_generator.py --category momentum --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402

RECORDS_PATH = (
    ROOT / "data" / "scrapes"
    / "tradingview-in-daytrading-strategies-2026-04-26"
    / "records.jsonl"
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL_DEFAULT = os.environ.get(
    "OLLAMA_STRATEGY_MODEL",
    os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b"),
)
OLLAMA_TIMEOUT_SEC = int(os.environ.get("OLLAMA_TIMEOUT_SEC", "300"))

# Hard cap so a sloppy CLI invocation can't flood records.jsonl.
HARD_COUNT_CAP = 50

REQUIRED_FIELDS = ("strategy_id", "title", "entry_rules", "exit_rules",
                   "risk_management")

PROMPT_TEMPLATE = textwrap.dedent("""\
    You are a quantitative-trading strategist. Invent {count} DISTINCT
    candidate trading strategies in the category: {category}.

    Each strategy must be:
      - Concrete and codeable: numeric thresholds, specific indicators,
        specific lookback windows. NO vague rules like "buy when trend is up".
      - Free of look-ahead bias: rules can only use prior-bar data, NOT
        the current bar's close before the bar closes.
      - Oriented to DAILY bars (OHLCV).
      - Practical: not derived from intra-day microstructure.
      - Different from each other (different mechanics, not just different
        thresholds on the same indicator).
    {avoid_clause}
    Output a JSON array. NO markdown fences. NO commentary. NO trailing
    text. The array must contain exactly {count} objects, each with these
    keys:

      "strategy_id"       — short kebab-case identifier (unique, lowercase,
                            <= 50 chars, e.g. "donchian-20-breakout-vol-filter")
      "title"             — human title (<= 80 chars)
      "entry_rules"       — plain English entry rules with concrete numbers
      "exit_rules"        — plain English exit rules with concrete numbers
      "risk_management"   — plain English risk notes (stop-loss / sizing)

    Example single object:
      {{
        "strategy_id": "donchian-20-breakout",
        "title": "Donchian 20-Day Breakout",
        "entry_rules": "Long when today's close > the prior 20-bar high.",
        "exit_rules": "Exit when today's close < the prior 10-bar low.",
        "risk_management": "Stop-loss at 2x ATR(20). Size to 1% account risk."
      }}

    Start the response with `[` and end with `]`. No other characters.
    """)


# ---------------------------------------------------------------------------
# Ollama plumbing — same indirection seam pattern as monitoring.llm_codegen
# ---------------------------------------------------------------------------

def _ollama_post(url: str, payload: Dict, timeout: float):
    """Indirection seam — tests mock this."""
    return requests.post(url, json=payload, timeout=timeout)


def call_ollama(prompt: str, *, model: Optional[str] = None,
                temperature: float = 0.7) -> str:
    url = f"{OLLAMA_URL}/api/generate"
    payload = {
        "model": model or OLLAMA_MODEL_DEFAULT,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            # Strategy lists can be long; allocate generously.
            "num_predict": 4000,
        },
    }
    resp = _ollama_post(url, payload, OLLAMA_TIMEOUT_SEC)
    if resp.status_code != 200:
        raise RuntimeError(f"ollama {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    text = body.get("response") or ""
    if not text.strip():
        raise RuntimeError("ollama returned empty response")
    return text


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_prompt(*, category: str, count: int,
                 avoid: Optional[List[str]] = None) -> str:
    avoid = [a.strip() for a in (avoid or []) if a and a.strip()]
    if avoid:
        joined = ", ".join(avoid)
        avoid_clause = (
            f"\n    Do NOT use any of these techniques or indicators: "
            f"{joined}. Strategies that rely on them must be skipped.\n"
        )
    else:
        avoid_clause = ""
    return PROMPT_TEMPLATE.format(
        category=category.strip() or "(any category)",
        count=count,
        avoid_clause=avoid_clause,
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _strip_fences(raw: str) -> str:
    m = _FENCE_RE.search(raw)
    if m:
        return m.group(1).strip()
    return raw.strip()


def _extract_first_json_array(text: str) -> Optional[list]:
    """Locate and decode the first JSON array in `text`. Returns None on failure."""
    start = text.find("[")
    if start < 0:
        return None
    dec = json.JSONDecoder()
    try:
        value, _ = dec.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list):
        return None
    return value


def parse_response(raw: str) -> List[Dict]:
    """Extract a list of candidate-strategy dicts. Malformed items are dropped."""
    cleaned = _strip_fences(raw)
    array = _extract_first_json_array(cleaned)
    if array is None:
        # Some models emit one-object-per-line — try that as a fallback.
        items: List[Dict] = []
        for line in cleaned.splitlines():
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                items.append(obj)
        return [it for it in items if _is_valid_item(it)]
    return [it for it in array if isinstance(it, dict) and _is_valid_item(it)]


def _is_valid_item(item: Dict) -> bool:
    for f in REQUIRED_FIELDS:
        val = item.get(f)
        if not isinstance(val, str) or not val.strip():
            return False
    sid = item["strategy_id"].strip()
    if len(sid) > 80 or not re.match(r"^[a-z0-9][a-z0-9_\-]*$", sid):
        return False
    return True


# ---------------------------------------------------------------------------
# records.jsonl I/O
# ---------------------------------------------------------------------------

def load_existing_strategy_ids(records_path: Path) -> set[str]:
    if not records_path.exists():
        return set()
    ids: set[str] = set()
    with records_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = (rec.get("extra") or {}).get("strategy_id")
            if sid:
                ids.add(sid)
    return ids


def append_record(records_path: Path, record: Dict) -> None:
    records_path.parent.mkdir(parents=True, exist_ok=True)
    with records_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_record(item: Dict, *, category: str, model: str) -> Dict:
    """Construct an UNTESTED record matching codegen_strategy schema."""
    today = date.today().isoformat()
    strategy_id = f"llm-{item['strategy_id'].strip().lower()}"
    title = item["title"].strip()
    entry = item["entry_rules"].strip()
    exit_ = item["exit_rules"].strip()
    risk = item["risk_management"].strip()

    description = f"Entry: {entry} | Exit: {exit_}"

    extra: Dict = {
        "agent_summary": (
            f"LLM-generated {category} candidate `{title}`. {description[:300]}"
        ),
        "description_full_readable": (
            f"Entry: {entry}\nExit: {exit_}\nRisk: {risk}"
        ),
        "strategy_id": strategy_id,
        "methodology_family": f"llm-{category}",
        "instruments": [],
        "timeframes": {"execution": "1d"},
        "core_concepts": [],
        "entry_rules": entry,
        "exit_rules": exit_,
        "risk_management": risk,
        "tested": False,
        "test_runs": [],
        "current_verdict": "UNTESTED",
        "verdict_summary": "LLM-generated candidate, not yet validated",
        "failure_modes": [],
        "improvement_hypotheses": [],
        "code_paths": {},
        "data_artifacts": [],
        "first_logged_iso": today,
        "last_updated_iso": today,
        "scraper": "llm_strategy_generator",
        "llm_model": model,
        "llm_category": category,
    }

    return {
        "url": f"llm://{model}/{category}/{strategy_id}",
        "title": title,
        "author": f"llm:{model}",
        "description": description[:500],
        "source": "llm_strategy_generator",
        "date_scraped": today,
        "tags": ["UNTESTED", "llm-generated", category],
        "extra": extra,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def generate(
    *,
    category: str,
    count: int,
    avoid: Optional[List[str]] = None,
    model: Optional[str] = None,
    temperature: float = 0.7,
    records_path: Path = RECORDS_PATH,
    dry_run: bool = False,
    ollama_caller: Optional[Callable[[str], str]] = None,
) -> Dict:
    """
    Generate `count` candidate strategies via Ollama and append the
    accepted ones (passing dedupe + shape validation) to records.jsonl.

    Returns: {requested, received, accepted, deduped, malformed, records: [...]}
    """
    if count <= 0:
        raise ValueError("count must be positive")
    if count > HARD_COUNT_CAP:
        log(
            f"count {count} exceeds hard cap {HARD_COUNT_CAP} — clamping",
            "WARNING",
        )
        count = HARD_COUNT_CAP

    used_model = model or OLLAMA_MODEL_DEFAULT
    prompt = build_prompt(category=category, count=count, avoid=avoid)

    if ollama_caller is None:
        raw = call_ollama(prompt, model=used_model, temperature=temperature)
    else:
        raw = ollama_caller(prompt)

    items = parse_response(raw)
    malformed = max(0, count - len(items))

    # Honor the count cap even if the model overshoots.
    items = items[:count]

    existing_ids = load_existing_strategy_ids(records_path)
    seen_in_run: set[str] = set()
    accepted: List[Dict] = []
    deduped = 0

    for it in items:
        candidate_id = f"llm-{it['strategy_id'].strip().lower()}"
        if candidate_id in existing_ids or candidate_id in seen_in_run:
            deduped += 1
            continue
        seen_in_run.add(candidate_id)
        record = build_record(it, category=category, model=used_model)
        if not dry_run:
            append_record(records_path, record)
        accepted.append(record)

    return {
        "requested": count,
        "received": len(items) + malformed,
        "accepted": len(accepted),
        "deduped": deduped,
        "malformed": malformed,
        "records": accepted,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", required=True,
                        help="e.g. mean-reversion, breakout, momentum")
    parser.add_argument("--count", type=int, default=10,
                        help=f"how many candidates to request "
                             f"(default 10, hard cap {HARD_COUNT_CAP})")
    parser.add_argument("--avoid", default="",
                        help="comma-separated techniques to exclude")
    parser.add_argument("--model", default=None,
                        help=f"override OLLAMA model "
                             f"(default {OLLAMA_MODEL_DEFAULT})")
    parser.add_argument("--temperature", type=float, default=0.7,
                        help="LLM temperature (default 0.7 for variety)")
    parser.add_argument("--records-path", default=str(RECORDS_PATH),
                        help="path to records.jsonl")
    parser.add_argument("--dry-run", action="store_true",
                        help="do not write to records.jsonl")
    args = parser.parse_args()

    avoid = [a for a in args.avoid.split(",") if a.strip()]

    log(
        f"llm_strategy_generator: category={args.category} count={args.count}"
        f" model={args.model or OLLAMA_MODEL_DEFAULT} temp={args.temperature}",
        "INFO",
    )

    summary = generate(
        category=args.category,
        count=args.count,
        avoid=avoid,
        model=args.model,
        temperature=args.temperature,
        records_path=Path(args.records_path),
        dry_run=args.dry_run,
    )

    log(
        f"done: requested={summary['requested']} received={summary['received']}"
        f" accepted={summary['accepted']} deduped={summary['deduped']}"
        f" malformed={summary['malformed']}",
        "SUCCESS" if summary["accepted"] > 0 else "WARNING",
    )
    return 0 if summary["accepted"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
