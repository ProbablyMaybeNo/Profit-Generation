"""
promote_strategy.py — Mark a validated strategy as active.

Adds an entry to `monitoring/config.py::TRACKED_STRATEGIES` (the
`active_on` ETF / stock universe + the compute_fn pointer), registers
any new module under `monitoring.intraday_monitor.COMPUTE_FN_MODULES`,
and reseeds the trading.db `strategies` table so the dashboard's
daily/intraday loops pick it up on the next refresh.

The two source files are edited via a *sentinel-block* protocol:
the first promotion wraps the list literal in `# --- START
AUTO-PROMOTED ---` / `# --- END AUTO-PROMOTED ---` markers; subsequent
runs rewrite the block in place between those markers. This keeps edits
localized and safe to re-run.

Idempotent:
  - Already-promoted strategy_id with the same active_on + compute_fn:
    no file changes.
  - Already-registered module in COMPUTE_FN_MODULES: no change.

Reversible:
  - `demote(strategy_id)` removes the entry from TRACKED_STRATEGIES (and
    its module from COMPUTE_FN_MODULES *if* no other tracked strategy
    references it).

Dry-run:
  - `dry_run=True` returns the would-be diff summary without writing.

CLI:
  py -3.13 scripts/promote_strategy.py --strategy-id rsi2-oversold \\
      --active-on GDX,KRE,XHB --compute-fn compute_rsi2_oversold \\
      --module strategies.generated.compute_rsi2_oversold
  py -3.13 scripts/promote_strategy.py --strategy-id rsi2-oversold --demote
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402

CONFIG_PATH = ROOT / "monitoring" / "config.py"
INTRADAY_PATH = ROOT / "monitoring" / "intraday_monitor.py"

DEFAULT_INTRADAY_MODULES = (
    "strategies.mean_reversion.botnet101",
)

PROMOTABLE_VERDICTS = {"PASS", "PASS_WITH_NUANCE"}

# Sentinel markers — placed once per file, never removed.
TS_START = "# --- START AUTO-PROMOTED ---"
TS_END = "# --- END AUTO-PROMOTED ---"
MOD_START = "# --- START AUTO-PROMOTED-MODULES ---"
MOD_END = "# --- END AUTO-PROMOTED-MODULES ---"


# ---------------------------------------------------------------------------
# Helpers — TRACKED_STRATEGIES read/write
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def parse_tracked_strategies(source: str) -> List[Dict]:
    """Parse TRACKED_STRATEGIES = [...] from a config.py source string."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "TRACKED_STRATEGIES":
                    return ast.literal_eval(node.value)
    return []


def parse_compute_fn_modules(source: str) -> List[str]:
    """Parse COMPUTE_FN_MODULES = [...] from intraday_monitor.py source."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "COMPUTE_FN_MODULES":
                    value = ast.literal_eval(node.value)
                    return list(value)
    return []


def _format_tracked_strategies(entries: List[Dict]) -> str:
    """Render the TRACKED_STRATEGIES list literal, one entry per line."""
    if not entries:
        return "TRACKED_STRATEGIES = []"
    body = ",\n".join(
        f"    {{{_format_dict_inline(e)}}}" for e in entries
    )
    return f"TRACKED_STRATEGIES = [\n{body},\n]"


def _format_dict_inline(d: Dict) -> str:
    # Preserve key order: id, compute, active_on, then anything else.
    preferred = ["id", "compute", "active_on"]
    keys = preferred + [k for k in d.keys() if k not in preferred]
    parts = []
    for k in keys:
        if k not in d:
            continue
        v = d[k]
        parts.append(f'"{k}": {json.dumps(v, ensure_ascii=False)}')
    return ", ".join(parts)


def _format_compute_fn_modules(modules: List[str]) -> str:
    if not modules:
        return "COMPUTE_FN_MODULES = []"
    body = ",\n".join(f'    "{m}"' for m in modules)
    return f"COMPUTE_FN_MODULES = [\n{body},\n]"


# ---------------------------------------------------------------------------
# Sentinel-block protocol — rewrite a list assignment between markers
# ---------------------------------------------------------------------------

def _ensure_sentinel_block(source: str, *, start: str, end: str,
                           var_name: str) -> str:
    """If markers aren't present, wrap the current `var_name = ...` assignment
    in a sentinel block. Safe to call multiple times."""
    if start in source and end in source:
        return source

    # Find the assignment line (e.g. "TRACKED_STRATEGIES = [")
    pattern = rf"^{re.escape(var_name)}\s*=\s*\["
    match = re.search(pattern, source, re.MULTILINE)
    if not match:
        raise ValueError(f"could not find `{var_name} = [` in source")
    assign_start = match.start()

    # Walk to the closing bracket of the list literal.
    depth = 0
    i = match.end() - 1  # points at the `[`
    while i < len(source):
        ch = source[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        raise ValueError(f"unterminated list literal for `{var_name}`")
    # End at end of the line containing the closing bracket.
    line_end = source.find("\n", i)
    if line_end == -1:
        line_end = len(source)

    before = source[:assign_start]
    block = source[assign_start:line_end]
    after = source[line_end:]
    return f"{before}{start}\n{block}\n{end}{after}"


def _replace_inside_sentinel(source: str, *, start: str, end: str,
                             replacement: str) -> str:
    """Replace whatever is between two sentinel markers with `replacement`."""
    s_idx = source.find(start)
    e_idx = source.find(end)
    if s_idx == -1 or e_idx == -1 or e_idx < s_idx:
        raise ValueError("sentinel markers missing or malformed")
    before = source[:s_idx + len(start)]
    after = source[e_idx:]
    return f"{before}\n{replacement}\n{after}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_passing_symbols(record: Dict) -> List[str]:
    """Return the symbols whose most recent test run was a PASS."""
    extra = record.get("extra") or {}
    runs = extra.get("test_runs") or []
    if not runs:
        return []
    latest_by_sym: Dict[str, Dict] = {}
    for r in runs:
        sym = r.get("instrument")
        if not sym:
            continue
        prev = latest_by_sym.get(sym)
        if prev is None or (r.get("date_iso") or "") >= (prev.get("date_iso") or ""):
            latest_by_sym[sym] = r
    passing: List[str] = []
    for sym, r in latest_by_sym.items():
        if (r.get("verdict") or "").upper() == "PASS":
            passing.append(sym)
    passing.sort()
    return passing


def _next_tracked(existing: List[Dict], *, strategy_id: str,
                  compute_fn: str, active_on: List[str]) -> Tuple[List[Dict], str]:
    """Return (new_list, action) where action is one of:
       'noop' (already present and identical), 'updated' (present, edited),
       'added' (not present)."""
    new_entry = {
        "id": strategy_id,
        "compute": compute_fn,
        "active_on": list(active_on),
    }
    out = list(existing)
    for idx, entry in enumerate(out):
        if entry.get("id") == strategy_id:
            if (entry.get("compute") == new_entry["compute"]
                    and list(entry.get("active_on") or []) == new_entry["active_on"]):
                return out, "noop"
            out[idx] = {**entry, **new_entry}
            return out, "updated"
    out.append(new_entry)
    return out, "added"


def _without_strategy(existing: List[Dict], strategy_id: str) -> Tuple[List[Dict], str]:
    out = [e for e in existing if e.get("id") != strategy_id]
    if len(out) == len(existing):
        return out, "noop"
    return out, "removed"


def _next_modules(existing: List[str], module: str) -> Tuple[List[str], str]:
    if module in existing:
        return list(existing), "noop"
    return existing + [module], "added"


def _without_module(existing: List[str], module: str,
                    *, still_referenced: bool) -> Tuple[List[str], str]:
    if still_referenced:
        return list(existing), "still-referenced"
    if module not in existing:
        return list(existing), "noop"
    return [m for m in existing if m != module], "removed"


def promote(
    *,
    strategy_id: str,
    compute_fn: str,
    active_on: Iterable[str],
    module: Optional[str] = None,
    config_path: Path = CONFIG_PATH,
    intraday_path: Path = INTRADAY_PATH,
    dry_run: bool = False,
    reseed: bool = True,
    seeder=None,
) -> Dict:
    """Promote a strategy. Returns a summary dict.

    `seeder` is a no-arg callable that reseeds trading.db; default uses
    `scripts.seed_strategies.main`. Tests inject a fake.
    """
    active_on = list(active_on)
    if not active_on:
        raise ValueError("active_on must contain at least one symbol")

    cfg_src = _read_text(config_path)
    cfg_src = _ensure_sentinel_block(
        cfg_src, start=TS_START, end=TS_END,
        var_name="TRACKED_STRATEGIES",
    )
    existing = parse_tracked_strategies(cfg_src)
    new_list, tracked_action = _next_tracked(
        existing, strategy_id=strategy_id,
        compute_fn=compute_fn, active_on=active_on,
    )
    new_cfg = cfg_src
    if tracked_action != "noop":
        new_cfg = _replace_inside_sentinel(
            cfg_src, start=TS_START, end=TS_END,
            replacement=_format_tracked_strategies(new_list),
        )

    module_action = "skipped"
    new_intraday: Optional[str] = None
    if module:
        intra_src = _read_text(intraday_path)
        intra_src = _ensure_sentinel_block(
            intra_src, start=MOD_START, end=MOD_END,
            var_name="COMPUTE_FN_MODULES",
        )
        existing_modules = parse_compute_fn_modules(intra_src)
        new_modules, module_action = _next_modules(existing_modules, module)
        if module_action != "noop":
            new_intraday = _replace_inside_sentinel(
                intra_src, start=MOD_START, end=MOD_END,
                replacement=_format_compute_fn_modules(new_modules),
            )

    summary = {
        "strategy_id": strategy_id,
        "active_on": active_on,
        "compute_fn": compute_fn,
        "module": module,
        "tracked_action": tracked_action,
        "module_action": module_action,
        "dry_run": dry_run,
        "reseeded": False,
    }

    if dry_run:
        return summary

    if tracked_action != "noop":
        _write_text(config_path, new_cfg)
    if module and new_intraday is not None and module_action != "noop":
        _write_text(intraday_path, new_intraday)

    if reseed and (tracked_action != "noop" or module_action == "added"):
        try:
            if seeder is None:
                from scripts import seed_strategies as ss
                seeder = ss.main
            seeder()
            summary["reseeded"] = True
        except Exception as e:
            log(f"reseed failed: {e}", "WARNING")
            summary["reseed_error"] = str(e)
    return summary


def demote(
    *,
    strategy_id: str,
    module: Optional[str] = None,
    config_path: Path = CONFIG_PATH,
    intraday_path: Path = INTRADAY_PATH,
    dry_run: bool = False,
    reseed: bool = True,
    seeder=None,
) -> Dict:
    cfg_src = _read_text(config_path)
    if TS_START not in cfg_src:
        cfg_src = _ensure_sentinel_block(
            cfg_src, start=TS_START, end=TS_END,
            var_name="TRACKED_STRATEGIES",
        )
    existing = parse_tracked_strategies(cfg_src)
    new_list, tracked_action = _without_strategy(existing, strategy_id)
    new_cfg = cfg_src
    if tracked_action != "noop":
        new_cfg = _replace_inside_sentinel(
            cfg_src, start=TS_START, end=TS_END,
            replacement=_format_tracked_strategies(new_list),
        )

    module_action = "skipped"
    new_intraday: Optional[str] = None
    if module:
        intra_src = _read_text(intraday_path)
        if MOD_START not in intra_src:
            intra_src = _ensure_sentinel_block(
                intra_src, start=MOD_START, end=MOD_END,
                var_name="COMPUTE_FN_MODULES",
            )
        existing_modules = parse_compute_fn_modules(intra_src)
        # Only remove the module if no remaining tracked strategy points
        # at a compute fn that's plausibly under this module. We can't
        # cheaply resolve that, so the safer rule is: keep the module if
        # any tracked strategy still references the same module name
        # explicitly (rare), otherwise remove. For now, keep behaviour
        # simple: remove only if the demoted strategy was the last one
        # referencing this module (via the still_referenced flag the
        # caller provides). Without external knowledge here, treat the
        # current list `new_list` as authoritative: if any remaining
        # entry's `compute` matches a function plausibly under `module`,
        # we keep it.
        still_ref = any(
            module.endswith(e.get("compute") or "") or
            (e.get("compute") or "") in module
            for e in new_list
        )
        new_modules, module_action = _without_module(
            existing_modules, module, still_referenced=still_ref,
        )
        if module_action == "removed":
            new_intraday = _replace_inside_sentinel(
                intra_src, start=MOD_START, end=MOD_END,
                replacement=_format_compute_fn_modules(new_modules),
            )

    summary = {
        "strategy_id": strategy_id,
        "module": module,
        "tracked_action": tracked_action,
        "module_action": module_action,
        "dry_run": dry_run,
        "reseeded": False,
    }

    if dry_run:
        return summary

    if tracked_action != "noop":
        _write_text(config_path, new_cfg)
    if module and new_intraday is not None and module_action == "removed":
        _write_text(intraday_path, new_intraday)

    if reseed and tracked_action != "noop":
        try:
            if seeder is None:
                from scripts import seed_strategies as ss
                seeder = ss.main
            seeder()
            summary["reseeded"] = True
        except Exception as e:
            log(f"reseed failed: {e}", "WARNING")
            summary["reseed_error"] = str(e)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--active-on", default="",
                        help="comma-separated symbols (required for promote)")
    parser.add_argument("--compute-fn", default="",
                        help="compute_fn name (required for promote)")
    parser.add_argument("--module", default=None,
                        help="dotted module path for COMPUTE_FN_MODULES")
    parser.add_argument("--demote", action="store_true",
                        help="reverse a prior promotion")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-reseed", action="store_true")
    args = parser.parse_args()

    if args.demote:
        summary = demote(
            strategy_id=args.strategy_id,
            module=args.module,
            dry_run=args.dry_run,
            reseed=not args.no_reseed,
        )
    else:
        active_on = [s.strip().upper() for s in args.active_on.split(",")
                     if s.strip()]
        if not active_on:
            print("--active-on is required for promotion")
            return 1
        if not args.compute_fn:
            print("--compute-fn is required for promotion")
            return 1
        summary = promote(
            strategy_id=args.strategy_id,
            compute_fn=args.compute_fn,
            active_on=active_on,
            module=args.module,
            dry_run=args.dry_run,
            reseed=not args.no_reseed,
        )

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
