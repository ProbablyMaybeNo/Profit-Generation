"""
build.py — Regenerate records.csv and manifest.json from records.jsonl.

records.jsonl is the source of truth. Edit it directly to add/update strategies,
then run this script to refresh the derived files:

    py -3.13 build.py

Schema: scraper-agent base + extra.* trading-specific fields. See README.md.
"""

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BUNDLE_DIR = Path(__file__).parent
JSONL = BUNDLE_DIR / "records.jsonl"
CSV_OUT = BUNDLE_DIR / "records.csv"
MANIFEST = BUNDLE_DIR / "manifest.json"


CSV_COLUMNS = [
    "url", "title", "author", "source", "date_scraped", "tags",
    "strategy_id", "methodology_family", "instruments", "timeframes",
    "core_concepts", "current_verdict", "verdict_summary",
    "tested", "n_test_runs", "best_sharpe", "best_pf", "best_win_rate_pct",
    "failure_modes", "improvement_hypotheses", "code_paths",
    "data_artifacts", "agent_summary",
    "first_logged_iso", "last_updated_iso",
]


def load_records():
    if not JSONL.exists():
        return []
    out = []
    with open(JSONL, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"records.jsonl line {i} invalid JSON: {e}")
    return out


def flatten_for_csv(rec):
    extra = rec.get("extra", {})
    test_runs = extra.get("test_runs", []) or []

    def best(metric, higher_is_better=True):
        vals = [tr.get(metric) for tr in test_runs if tr.get(metric) is not None]
        if not vals:
            return ""
        return max(vals) if higher_is_better else min(vals)

    def joinlist(key, sep="; "):
        v = extra.get(key)
        if isinstance(v, list):
            return sep.join(str(x) for x in v)
        if isinstance(v, dict):
            return json.dumps(v, ensure_ascii=False)
        return v or ""

    row = {
        "url": rec.get("url", ""),
        "title": rec.get("title", ""),
        "author": rec.get("author", ""),
        "source": rec.get("source", ""),
        "date_scraped": rec.get("date_scraped", ""),
        "tags": "; ".join(rec.get("tags", []) or []),
        "strategy_id": extra.get("strategy_id", ""),
        "methodology_family": extra.get("methodology_family", ""),
        "instruments": joinlist("instruments"),
        "timeframes": json.dumps(extra.get("timeframes", {}), ensure_ascii=False),
        "core_concepts": joinlist("core_concepts"),
        "current_verdict": extra.get("current_verdict", ""),
        "verdict_summary": extra.get("verdict_summary", ""),
        "tested": extra.get("tested", False),
        "n_test_runs": len(test_runs),
        "best_sharpe": best("sharpe", True),
        "best_pf": best("profit_factor", True),
        "best_win_rate_pct": best("win_rate_pct", True),
        "failure_modes": joinlist("failure_modes"),
        "improvement_hypotheses": joinlist("improvement_hypotheses"),
        "code_paths": json.dumps(extra.get("code_paths", {}), ensure_ascii=False),
        "data_artifacts": joinlist("data_artifacts"),
        "agent_summary": extra.get("agent_summary", ""),
        "first_logged_iso": extra.get("first_logged_iso", ""),
        "last_updated_iso": extra.get("last_updated_iso", ""),
    }
    return row


def write_csv(records):
    with open(CSV_OUT, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow(flatten_for_csv(r))


def write_manifest(records, errors=None):
    by_verdict = {}
    for r in records:
        v = (r.get("extra") or {}).get("current_verdict", "UNTESTED")
        by_verdict[v] = by_verdict.get(v, 0) + 1

    manifest = {
        "bundle_name": BUNDLE_DIR.name,
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "build.py (Claude Code interactive session)",
        "record_count": len(records),
        "verdict_counts": by_verdict,
        "schema_fields": {
            "base": [
                "url", "title", "author", "description", "source",
                "date_scraped", "tags",
            ],
            "extra_required": [
                "agent_summary", "description_full_readable",
            ],
            "extra_strategy": [
                "strategy_id", "methodology_family", "instruments", "timeframes",
                "core_concepts", "entry_rules", "exit_rules", "risk_management",
                "tested", "test_runs", "current_verdict", "verdict_summary",
                "failure_modes", "improvement_hypotheses", "code_paths",
                "data_artifacts", "first_logged_iso", "last_updated_iso",
            ],
        },
        "verdict_vocab": ["UNTESTED", "PASS", "PASS_WITH_NUANCE", "MARGINAL", "FAIL", "DEPRECATED"],
        "consumer_notes": (
            "records.jsonl is the source of truth. records.csv and "
            "manifest.json are derived — regenerate with `py -3.13 build.py` "
            "after editing records.jsonl. CSV uses UTF-8 BOM for Excel "
            "compatibility. Test runs are nested in extra.test_runs; "
            "best_sharpe/best_pf/best_win_rate_pct in CSV are aggregates "
            "across runs. See README.md for full schema and workflow."
        ),
        "errors": errors or [],
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def main():
    try:
        records = load_records()
    except ValueError as e:
        print(f"ERROR loading records.jsonl: {e}", file=sys.stderr)
        sys.exit(1)

    write_csv(records)
    write_manifest(records)

    print(f"records.jsonl    -> {len(records)} records")
    print(f"records.csv      -> {CSV_OUT}")
    print(f"manifest.json    -> {MANIFEST}")
    print()
    by_verdict = {}
    for r in records:
        v = (r.get("extra") or {}).get("current_verdict", "UNTESTED")
        by_verdict[v] = by_verdict.get(v, 0) + 1
    print("By verdict:")
    for v, n in sorted(by_verdict.items()):
        print(f"  {v}: {n}")


if __name__ == "__main__":
    main()
