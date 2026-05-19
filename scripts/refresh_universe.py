"""
refresh_universe.py — Quarterly refresh of S&P 500 + Nasdaq-100 constituents
(milestone 5.5.1.2).

Scrapes Wikipedia (the canonical free source for index membership) and
rewrites `data/universes/sp500.csv` and `data/universes/nasdaq100.csv`.
Telegram-alerts on adds / removes since the last run so Ross sees the
diff before the scanner picks up the new universe.

The ETFs CSV (`data/universes/etfs.csv`) is hand-curated and is NOT
touched by this script — refresh ETFs manually when adding new names.

Idempotent: running twice in a row with no Wikipedia change writes
the same CSV body. Run quarterly via manual invocation:

    py -3.13 scripts/refresh_universe.py
    py -3.13 scripts/refresh_universe.py --dry-run   (no file writes)
    py -3.13 scripts/refresh_universe.py --no-telegram

Acceptance per plan 5.5.1.2: parses, diffs, alerts. Tests cover the
parsing/diff logic with HTML fixtures (no network).
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from monitoring import universe as universe_loader  # noqa: E402

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

USER_AGENT = "profit-generation-universe-refresh/1.0"


@dataclass
class IndexRow:
    symbol: str
    name: str
    sector: str


# ---------------------------------------------------------------------------
# HTML parsing (BeautifulSoup, no JS execution required)
# ---------------------------------------------------------------------------


def _fetch_html(url: str, *, fetcher=None) -> str:
    """Fetch URL and return HTML text. `fetcher` is the test seam."""
    if fetcher is not None:
        return fetcher(url)
    import requests
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_sp500_html(html: str) -> List[IndexRow]:
    """Parse Wikipedia's S&P 500 constituents table.

    Wikipedia's structure: first wikitable with id 'constituents' has
    columns: Symbol, Security, GICS Sector, ... . We hit that one.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="constituents")
    if table is None:
        # Fallback: first wikitable on the page
        table = soup.find("table", class_="wikitable")
    if table is None:
        return []
    rows: List[IndexRow] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        # Skip header row (th-only)
        if all(c.name == "th" for c in cells):
            continue
        sym = cells[0].get_text(strip=True).replace("\xa0", " ")
        name = cells[1].get_text(strip=True)
        sector = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        if not sym or not name:
            continue
        rows.append(IndexRow(symbol=sym.upper(), name=name, sector=sector))
    return rows


def parse_ndx_html(html: str) -> List[IndexRow]:
    """Parse Wikipedia's Nasdaq-100 components table.

    Wikipedia's Nasdaq-100 page has a table with id 'constituents'
    in recent versions (the layout has flipped a few times — fall
    back to scanning wikitable rows for a 4-col Company/Symbol/...
    shape).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="constituents")
    if table is None:
        # Fallback: scan wikitables for one with a Ticker / Symbol column,
        # then fall back to the first wikitable on the page.
        for cand in soup.find_all("table", class_="wikitable"):
            header = cand.find("tr")
            if header is None:
                continue
            headers = [c.get_text(strip=True).lower()
                       for c in header.find_all(["th", "td"])]
            if any(h in ("symbol", "ticker") for h in headers):
                table = cand
                break
        if table is None:
            table = soup.find("table", class_="wikitable")
    if table is None:
        return []

    body = table.find("tbody") or table
    header_cells = body.find("tr").find_all(["th", "td"]) if body.find("tr") else []
    headers = [c.get_text(strip=True).lower() for c in header_cells]

    def _idx(*candidates):
        for c in candidates:
            if c in headers:
                return headers.index(c)
        return -1

    sym_i = _idx("symbol", "ticker")
    name_i = _idx("company", "name", "security")
    sector_i = _idx("gics sector", "sector", "gics sub-industry")

    if sym_i < 0:
        # If headers couldn't be matched, assume symbol-first or
        # symbol-second layouts are both possible — punt with first/second
        # column inspection on a sample row.
        sample = None
        for tr in body.find_all("tr")[1:]:
            tds = tr.find_all(["td", "th"])
            if len(tds) >= 2:
                sample = tds
                break
        if sample is None:
            return []
        # Heuristic: ticker is short uppercase, mostly alpha
        col0 = sample[0].get_text(strip=True)
        col1 = sample[1].get_text(strip=True)
        if col0.isupper() and len(col0) <= 5 and col0.isalpha():
            sym_i, name_i = 0, 1
        elif col1.isupper() and len(col1) <= 5 and col1.isalpha():
            sym_i, name_i = 1, 0
        else:
            return []

    rows: List[IndexRow] = []
    for tr in body.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) <= max(sym_i, name_i):
            continue
        if all(c.name == "th" for c in cells):
            continue
        sym = cells[sym_i].get_text(strip=True).replace("\xa0", " ")
        name = cells[name_i].get_text(strip=True) if name_i >= 0 else ""
        sector = (cells[sector_i].get_text(strip=True)
                  if 0 <= sector_i < len(cells) else "")
        if not sym or not sym.replace(".", "").isalnum():
            continue
        if len(sym) > 6:
            # Wikipedia sometimes embeds footnote numerals — strip suffix digits
            cleaned = "".join(c for c in sym if c.isalpha() or c == ".")
            if 1 <= len(cleaned) <= 6:
                sym = cleaned
            else:
                continue
        rows.append(IndexRow(symbol=sym.upper(), name=name, sector=sector))
    return rows


# ---------------------------------------------------------------------------
# CSV write
# ---------------------------------------------------------------------------


def write_universe_csv(rows: List[IndexRow], path: Path) -> None:
    """Write rows to `path` as symbol,name,sector CSV (deduped, stable order)."""
    seen = set()
    deduped: List[IndexRow] = []
    for r in rows:
        if r.symbol in seen:
            continue
        seen.add(r.symbol)
        deduped.append(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "name", "sector"])
        for r in deduped:
            writer.writerow([r.symbol, r.name, r.sector])


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def diff_symbols(
    new_symbols: Iterable[str], existing_path: Path,
) -> Tuple[List[str], List[str]]:
    """Return (added, removed) — symbols newly in / dropped from `new_symbols`."""
    new_set = {s.upper() for s in new_symbols}
    old_set: set = set()
    if existing_path.exists():
        try:
            with open(existing_path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    sym = (row.get("symbol") or "").strip().upper()
                    if sym:
                        old_set.add(sym)
        except OSError:
            pass
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    return added, removed


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def refresh(
    *,
    dry_run: bool = False,
    enable_telegram: bool = True,
    sp500_fetcher=None,
    ndx_fetcher=None,
    telegram_sender=None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Dict[str, list]]:
    """Run the refresh and return a summary dict.

    `*_fetcher` and `telegram_sender` are seams for tests.
    """
    base = Path(base_dir) if base_dir is not None else universe_loader.UNIVERSE_DIR

    sp500_html = _fetch_html(SP500_URL, fetcher=sp500_fetcher)
    sp500_rows = parse_sp500_html(sp500_html)
    log(f"refresh_universe: parsed S&P 500 → {len(sp500_rows)} rows")

    ndx_html = _fetch_html(NDX_URL, fetcher=ndx_fetcher)
    ndx_rows = parse_ndx_html(ndx_html)
    log(f"refresh_universe: parsed Nasdaq-100 → {len(ndx_rows)} rows")

    sp500_path = base / "sp500.csv"
    ndx_path = base / "nasdaq100.csv"

    sp_added, sp_removed = diff_symbols([r.symbol for r in sp500_rows], sp500_path)
    ndx_added, ndx_removed = diff_symbols([r.symbol for r in ndx_rows], ndx_path)

    summary = {
        "sp500": {
            "count": len(sp500_rows),
            "added": sp_added,
            "removed": sp_removed,
        },
        "nasdaq100": {
            "count": len(ndx_rows),
            "added": ndx_added,
            "removed": ndx_removed,
        },
    }

    if not dry_run:
        if sp500_rows:
            write_universe_csv(sp500_rows, sp500_path)
        else:
            log("refresh_universe: skipping S&P 500 write — parser returned 0 rows",
                level="WARNING")
        if ndx_rows:
            write_universe_csv(ndx_rows, ndx_path)
        else:
            log("refresh_universe: skipping Nasdaq-100 write — parser returned 0 rows",
                level="WARNING")

    if enable_telegram and (sp_added or sp_removed or ndx_added or ndx_removed):
        _alert(summary, sender=telegram_sender)

    return summary


def _alert(summary: Dict, *, sender=None) -> None:
    """Push a Telegram one-liner with the diff."""
    sp = summary["sp500"]
    nx = summary["nasdaq100"]
    parts = []
    if sp["added"] or sp["removed"]:
        parts.append(
            f"S&P 500: +{len(sp['added'])} / -{len(sp['removed'])}"
            f" (added: {', '.join(sp['added'][:5]) or '—'};"
            f" removed: {', '.join(sp['removed'][:5]) or '—'})"
        )
    if nx["added"] or nx["removed"]:
        parts.append(
            f"NDX: +{len(nx['added'])} / -{len(nx['removed'])}"
            f" (added: {', '.join(nx['added'][:5]) or '—'};"
            f" removed: {', '.join(nx['removed'][:5]) or '—'})"
        )
    msg = "[universe-refresh] " + " | ".join(parts) if parts else None
    if msg is None:
        return
    if sender is not None:
        sender(msg)
        return
    try:
        from monitoring.telegram_alerter import send_message
        send_message(msg)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log(f"refresh_universe: telegram send failed ({exc})", level="WARNING")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Quarterly universe refresh")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse + diff but do not write CSVs")
    p.add_argument("--no-telegram", action="store_true",
                   help="Suppress Telegram diff alert")
    args = p.parse_args(argv)

    summary = refresh(
        dry_run=args.dry_run,
        enable_telegram=not args.no_telegram,
    )

    import json
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
