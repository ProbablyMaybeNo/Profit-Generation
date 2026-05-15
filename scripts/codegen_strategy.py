"""
codegen_strategy.py — Generate a compute_fn for a strategy and write it
to strategies/generated/<strategy_id>.py.

Two ways to provide rules:
  (a) From an existing record in records.jsonl (by strategy_id):
        py -3.13 scripts/codegen_strategy.py --strategy-id rsi2-oversold

  (b) From the command line (creates a new UNTESTED record AND generates):
        py -3.13 scripts/codegen_strategy.py \\
            --strategy-id rsi2-oversold \\
            --title "RSI(2) Oversold" \\
            --entry "long when 2-period RSI < 10 and close > 200d SMA" \\
            --exit  "exit when 2-period RSI > 70" \\
            --new

After generation:
  - File: strategies/generated/<id_safe>.py
  - DB strategies table updated with compute_fn pointer
  - records.jsonl updated with the same compute_fn pointer (so next seed
    keeps the link)
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import llm_codegen  # noqa: E402

RECORDS_PATH = (
    ROOT / "data" / "scrapes"
    / "tradingview-in-daytrading-strategies-2026-04-26" / "records.jsonl"
)
GENERATED_DIR = ROOT / "strategies" / "generated"


def _safe_filename(strategy_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", strategy_id).strip("_").lower() or "strategy"


def _load_records() -> list:
    if not RECORDS_PATH.exists():
        return []
    out = []
    with RECORDS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _save_records(records: list) -> None:
    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RECORDS_PATH.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _find_record(records: list, strategy_id: str) -> Optional[Dict]:
    for r in records:
        extra = r.get("extra", {}) or {}
        if extra.get("strategy_id") == strategy_id:
            return r
    return None


def _make_new_record(strategy_id: str, title: str, entry: str, exit_: str,
                     risk: str, source_url: str) -> Dict:
    today = date.today().isoformat()
    return {
        "url": source_url or f"local://{strategy_id}",
        "title": title or strategy_id,
        "author": "user-supplied",
        "description": entry[:200],
        "source": "user-cli",
        "date_scraped": today,
        "tags": ["UNTESTED", "user-supplied"],
        "extra": {
            "agent_summary": entry,
            "description_full_readable": f"Entry: {entry}\nExit: {exit_}\nRisk: {risk}",
            "strategy_id": strategy_id,
            "methodology_family": "user-supplied",
            "instruments": [],
            "timeframes": {"execution": "1d"},
            "core_concepts": [],
            "entry_rules": entry,
            "exit_rules": exit_,
            "risk_management": risk,
            "tested": False,
            "test_runs": [],
            "current_verdict": "UNTESTED",
            "verdict_summary": "freshly generated, not yet validated",
            "failure_modes": [],
            "improvement_hypotheses": [],
            "code_paths": {},
            "data_artifacts": [],
            "first_logged_iso": today,
            "last_updated_iso": today,
        },
    }


def _write_generated_file(fn_name: str, code: str, strategy_id: str,
                          source_url: str) -> Path:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    pkg_init = GENERATED_DIR / "__init__.py"
    if not pkg_init.exists():
        pkg_init.write_text("", encoding="utf-8")
    path = GENERATED_DIR / f"{_safe_filename(strategy_id)}.py"
    header = (
        f'"""LLM-generated compute_fn for strategy `{strategy_id}`.\n\n'
        f'Source: {source_url}\n'
        f'Generated: {date.today().isoformat()}\n\n'
        f'DO NOT hand-edit unless you also update records.jsonl. Re-run\n'
        f'codegen_strategy.py to regenerate.\n"""\n\n'
        f'import pandas as pd\n'
        f'import numpy as np\n\n\n'
    )
    path.write_text(header + code, encoding="utf-8")
    return path


def codegen_record(
    record: Dict,
    *,
    model: Optional[str] = None,
    temperature: float = 0.1,
    dry_run: bool = False,
    source_url_override: str = "",
) -> Dict:
    """
    Generate a compute_fn for one record. Pure function — does NOT touch
    records.jsonl or trading.db (callers do that).

    Returns:
      {ok: bool, fn_name, code (str), path (Path or None),
       error: Optional[str]}
    """
    extra = record.get("extra", {}) or {}
    sid = extra.get("strategy_id")
    if not sid:
        return {"ok": False, "error": "record missing extra.strategy_id"}

    entry = extra.get("entry_rules") or ""
    exit_ = extra.get("exit_rules") or ""
    risk = extra.get("risk_management") or ""
    if not entry or not exit_:
        return {"ok": False, "error": "record missing entry_rules or exit_rules"}

    fn_name = llm_codegen.fn_name_from_strategy_id(sid)
    try:
        code = llm_codegen.generate_compute_fn(
            fn_name, entry_rules=entry, exit_rules=exit_,
            risk_management=risk, model=model, temperature=temperature,
        )
    except Exception as e:
        return {"ok": False, "fn_name": fn_name, "error": f"codegen failed: {e!s:.300}"}

    if dry_run:
        return {"ok": True, "fn_name": fn_name, "code": code, "path": None}

    path = _write_generated_file(
        fn_name, code, sid,
        source_url_override or record.get("url", ""),
    )
    return {"ok": True, "fn_name": fn_name, "code": code, "path": path}


def _persist_codegen(record: Dict, result: Dict, records: list,
                     is_new: bool) -> None:
    """Update record + records.jsonl + trading.db with codegen output."""
    extra = record.get("extra", {}) or {}
    extra["compute_fn"] = result["fn_name"]
    if result.get("path") is not None:
        try:
            rel = str(result["path"].relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(result["path"]).replace("\\", "/")
        extra["code_paths"] = {**(extra.get("code_paths") or {}), "compute_fn": rel}
    extra["last_updated_iso"] = date.today().isoformat()
    record["extra"] = extra

    if is_new:
        records.append(record)
    else:
        sid = extra["strategy_id"]
        for i, r in enumerate(records):
            if (r.get("extra", {}) or {}).get("strategy_id") == sid:
                records[i] = record
                break
    _save_records(records)
    conn = db.init_db()
    try:
        db.upsert_strategy(conn, record)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--new", action="store_true",
                        help="Create a new UNTESTED record from --title/--entry/--exit/--risk")
    parser.add_argument("--title", default="")
    parser.add_argument("--entry", default="")
    parser.add_argument("--exit", dest="exit_", default="")
    parser.add_argument("--risk", default="")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print generated code without writing or updating DB")
    args = parser.parse_args()

    records = _load_records()
    record = _find_record(records, args.strategy_id)

    if args.new:
        if record is not None:
            print(f"refusing to --new: strategy {args.strategy_id} already in records.jsonl")
            return 1
        if not (args.entry and args.exit_):
            print("--new requires --entry and --exit")
            return 2
        record = _make_new_record(args.strategy_id, args.title, args.entry,
                                  args.exit_, args.risk, args.source_url)

    if record is None:
        print(f"strategy {args.strategy_id} not found in records.jsonl "
              f"(use --new to create it)")
        return 3

    # Apply CLI overrides into the record so the helper sees them.
    extra = record.get("extra", {}) or {}
    if args.entry:
        extra["entry_rules"] = args.entry
    if args.exit_:
        extra["exit_rules"] = args.exit_
    if args.risk:
        extra["risk_management"] = args.risk
    record["extra"] = extra

    print(f"generating {llm_codegen.fn_name_from_strategy_id(args.strategy_id)} via Ollama...")
    result = codegen_record(
        record, model=args.model, temperature=args.temperature,
        dry_run=args.dry_run, source_url_override=args.source_url,
    )
    if not result["ok"]:
        print(f"FAILED: {result.get('error')}")
        return 4

    if args.dry_run:
        print("--- generated code (dry-run, not written) ---")
        print(result["code"])
        return 0

    print(f"wrote {result['path']}")
    _persist_codegen(record, result, records, is_new=args.new)
    print("updated records.jsonl + trading.db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
