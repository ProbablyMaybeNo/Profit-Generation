"""
universe.py — Trend-scanner universe loader (milestone 5.5.1.1).

Loads the S&P 500, Nasdaq-100, and high-volume ETF universes from
in-repo CSV snapshots at `data/universes/`. Returns a deduplicated
list of symbols suitable for the wide-universe trend scanner.

The CSV files are manually curated snapshots — quarterly refresh is
handled via `scripts/refresh_universe.py` (5.5.1.2) rather than an
auto-fetch on every run, so the universe never silently changes under
the scanner. See `docs/RUNBOOK.md` for the quarterly refresh procedure.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, List, Optional

from config.utils import get_project_root, log

UNIVERSE_DIR = get_project_root() / "data" / "universes"

DEFAULT_FILES = ("sp500.csv", "nasdaq100.csv", "etfs.csv")


def _load_csv_symbols(path: Path) -> List[str]:
    """Read symbols from a universe CSV. Returns [] if missing/unreadable."""
    if not path.exists():
        log(f"universe: source file missing — {path.name}", level="WARNING")
        return []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None or "symbol" not in reader.fieldnames:
                log(
                    f"universe: {path.name} has no 'symbol' column "
                    f"(found: {reader.fieldnames})",
                    level="WARNING",
                )
                return []
            symbols = []
            for row in reader:
                sym = (row.get("symbol") or "").strip().upper()
                if sym:
                    symbols.append(sym)
            return symbols
    except OSError as exc:
        log(f"universe: failed to read {path.name} ({exc})", level="WARNING")
        return []


def load_trend_universe(
    files: Optional[Iterable[str]] = None,
    *,
    universe_dir: Optional[Path] = None,
) -> List[str]:
    """
    Load the combined trend-scanner universe.

    Returns a sorted, deduplicated list of symbols pulled from the CSV
    files in `data/universes/`. Missing files are logged and skipped —
    a partial universe is preferable to an exception on a routine EOD run.

    Args:
        files: optional iterable of filenames to load (default: all three).
        universe_dir: optional override of the universe directory (tests).
    """
    base = Path(universe_dir) if universe_dir is not None else UNIVERSE_DIR
    targets = tuple(files) if files is not None else DEFAULT_FILES

    seen = set()
    out: List[str] = []
    for fname in targets:
        for sym in _load_csv_symbols(base / fname):
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
    out.sort()
    return out


def universe_breakdown(
    universe_dir: Optional[Path] = None,
) -> dict:
    """Return per-source counts + combined unique count for debugging."""
    base = Path(universe_dir) if universe_dir is not None else UNIVERSE_DIR
    per_source = {}
    combined = set()
    for fname in DEFAULT_FILES:
        syms = _load_csv_symbols(base / fname)
        per_source[fname] = len(syms)
        combined.update(syms)
    return {
        "per_source": per_source,
        "unique_total": len(combined),
    }


if __name__ == "__main__":
    import json

    syms = load_trend_universe()
    print(json.dumps({
        "count": len(syms),
        "first_20": syms[:20],
        "breakdown": universe_breakdown(),
    }, indent=2))
