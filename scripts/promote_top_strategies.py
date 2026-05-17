"""
promote_top_strategies.py — Auto-promote the best validator-passing
strategies into monitoring.config.TRACKED_STRATEGIES.

Scans every `data/scrapes/*/records.jsonl`, identifies PASS test runs
not already represented by an active tracked strategy, ranks the
remaining candidates by a stability score (mean PASS Sharpe × sqrt
of distinct PASS instruments), and promotes the top N via the
existing `scripts.promote_strategy.promote` machinery.

Idempotent: candidates already in TRACKED_STRATEGIES are skipped at
the dedupe step, never promoted twice. The underlying promote() call
is itself idempotent at the file level (noop on identical entries).

Usage:
  py -3.13 scripts/promote_top_strategies.py --top 10
  py -3.13 scripts/promote_top_strategies.py --dry-run
  py -3.13 scripts/promote_top_strategies.py --top 5 --json
"""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_TOP = 10
DEFAULT_RECORDS_GLOB = "data/scrapes/*/records.jsonl"


# ---- Scanning + extraction -----------------------------------------------

def iter_records(records_paths: Iterable[Path]) -> Iterable[Dict]:
    """Yield parsed records from one or more JSONL paths. Malformed lines
    skipped silently."""
    for p in records_paths:
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue


def extract_candidate(record: Dict) -> Optional[Dict]:
    """Pull the fields we need from a flat record into a stable shape.
    Returns None when the record is unusable (no strategy_id, no
    test_runs, no compute_fn, etc.).
    """
    extra = record.get("extra") or {}
    flat = {**extra, **{k: v for k, v in record.items() if k != "extra"}}
    sid = flat.get("strategy_id")
    if not sid:
        return None
    compute_fn = flat.get("compute_fn")
    if not compute_fn or "/" in str(compute_fn):
        # Path-style compute_fn references a file, not a callable — skip.
        return None
    test_runs = flat.get("test_runs") or []
    pass_runs = [r for r in test_runs
                  if (r.get("verdict") or "").upper() == "PASS"]
    if not pass_runs:
        return None
    instruments_pass = sorted({r.get("instrument") for r in pass_runs
                                if r.get("instrument")})
    sharpes = [float(r.get("sharpe", 0) or 0) for r in pass_runs
                if r.get("sharpe") is not None]
    if not sharpes:
        return None
    mean_sharpe = sum(sharpes) / len(sharpes)
    return {
        "strategy_id":     sid,
        "compute_fn":      str(compute_fn),
        "instruments":     instruments_pass,
        "n_pass":          len(pass_runs),
        "mean_sharpe":     round(mean_sharpe, 4),
        "min_sharpe":      round(min(sharpes), 4),
        "max_sharpe":      round(max(sharpes), 4),
        "score":           round(mean_sharpe * math.sqrt(len(instruments_pass)),
                                  4) if instruments_pass else 0.0,
    }


def rank_candidates(candidates: List[Dict]) -> List[Dict]:
    """Sort by score desc, breaking ties by mean_sharpe desc then strategy_id."""
    return sorted(
        candidates,
        key=lambda c: (-c["score"], -c["mean_sharpe"], c["strategy_id"]),
    )


def dedupe_against_active(candidates: List[Dict],
                           active_ids: Iterable[str]) -> List[Dict]:
    active = {a for a in active_ids if a}
    return [c for c in candidates if c["strategy_id"] not in active]


# ---- Glue ----------------------------------------------------------------

def _active_strategy_ids() -> List[str]:
    from monitoring.config import TRACKED_STRATEGIES
    return [s.get("id") for s in TRACKED_STRATEGIES if s.get("id")]


def _records_paths(glob: str = DEFAULT_RECORDS_GLOB) -> List[Path]:
    return sorted(ROOT.glob(glob))


def promote_top(
    *,
    top_n: int = DEFAULT_TOP,
    dry_run: bool = False,
    records_paths: Optional[List[Path]] = None,
    active_ids: Optional[List[str]] = None,
    promote_fn=None,
) -> Dict:
    """End-to-end orchestration. Returns a report dict."""
    if records_paths is None:
        records_paths = _records_paths()
    if active_ids is None:
        active_ids = _active_strategy_ids()
    if promote_fn is None:
        from scripts.promote_strategy import promote as real_promote
        promote_fn = real_promote

    raw: List[Dict] = []
    for rec in iter_records(records_paths):
        cand = extract_candidate(rec)
        if cand:
            raw.append(cand)

    # If the same strategy_id appears in multiple records.jsonl files,
    # keep the one with the higher score.
    by_id: Dict[str, Dict] = {}
    for c in raw:
        prev = by_id.get(c["strategy_id"])
        if prev is None or c["score"] > prev["score"]:
            by_id[c["strategy_id"]] = c
    candidates = list(by_id.values())

    candidates = dedupe_against_active(candidates, active_ids)
    candidates = rank_candidates(candidates)
    promote_list = candidates[:top_n]

    promotions: List[Dict] = []
    for c in promote_list:
        action = "would_promote" if dry_run else None
        if not dry_run:
            try:
                summary = promote_fn(
                    strategy_id=c["strategy_id"],
                    compute_fn=c["compute_fn"],
                    active_on=c["instruments"],
                )
                action = summary.get("tracked_action", "added")
            except Exception as e:
                action = f"error: {str(e)[:120]}"
        promotions.append({
            "strategy_id":   c["strategy_id"],
            "score":         c["score"],
            "mean_sharpe":   c["mean_sharpe"],
            "n_pass":        c["n_pass"],
            "instruments":   c["instruments"],
            "compute_fn":    c["compute_fn"],
            "action":        action or "promoted",
        })

    return {
        "dry_run":           dry_run,
        "top_n":             top_n,
        "candidates_total":  len(candidates) + len(promote_list)
                              if False else len(candidates),
        "records_scanned":   len(records_paths),
        "active_count":      len(active_ids),
        "promotions":        promotions,
        "skipped_candidates": [c["strategy_id"] for c in candidates[top_n:]],
    }


def format_report(result: Dict) -> str:
    lines = [
        f"promote_top_strategies — dry_run={result['dry_run']} "
        f"top_n={result['top_n']}",
        f"  records.jsonl scanned: {result['records_scanned']}",
        f"  already-active strategies: {result['active_count']}",
        f"  fresh candidates (after dedupe): {result['candidates_total']}",
        "",
    ]
    if not result["promotions"]:
        lines.append("  no candidates to promote — TRACKED_STRATEGIES is already "
                      "at or beyond top-N.")
        return "\n".join(lines)
    lines.append("Top promotions:")
    for p in result["promotions"]:
        instruments = ", ".join(p["instruments"][:6])
        if len(p["instruments"]) > 6:
            instruments += f" +{len(p['instruments']) - 6}"
        lines.append(
            f"  [{p['action']}] {p['strategy_id']} · "
            f"score={p['score']} · mean Sharpe={p['mean_sharpe']} · "
            f"{p['n_pass']} PASS · [{instruments}]"
        )
    if result["skipped_candidates"]:
        lines.append("")
        lines.append(
            f"  skipped (rank > top): "
            f"{', '.join(result['skipped_candidates'][:10])}"
            + (" …" if len(result["skipped_candidates"]) > 10 else "")
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Auto-promote top-N validator-passing strategies."
    )
    parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                        help=f"how many to promote (default {DEFAULT_TOP})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen without modifying configs")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    args = parser.parse_args()
    result = promote_top(top_n=args.top, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
