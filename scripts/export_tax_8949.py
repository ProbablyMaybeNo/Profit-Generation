"""export_tax_8949.py — IRS Form 8949 CSV export.

Joins the project's `paper_trades` table (buy + sell rows keyed by
signal_id) into closed round-trips, then emits one CSV row per round-trip
in the column shape the IRS expects on Form 8949:

  description, date_acquired, date_sold, proceeds, cost_basis, gain_loss

Splits short-term (held < 365 days) vs long-term (held >= 365 days) into
two output files, mirroring how 8949 actually breaks out: Box A/B/C for
short-term, Box D/E/F for long-term.

Useful now against paper trades for shape validation; identical code path
once live trades start flowing.

CLI:
  py -3.13 scripts/export_tax_8949.py --year 2026
  py -3.13 scripts/export_tax_8949.py --year 2026 --out D:/Taxes/
  py -3.13 scripts/export_tax_8949.py --year 2026 --include-paper
"""

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402

LONG_TERM_DAYS = 365
FORM_8949_COLUMNS = [
    "description", "date_acquired", "date_sold",
    "proceeds", "cost_basis", "gain_loss",
]


def _parse_ts(raw: Optional[str]) -> Optional[date]:
    """Pull a date out of an Alpaca-style ISO timestamp. Accepts both
    naive and tz-aware strings."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return date.fromisoformat(str(raw)[:10])
        except Exception:
            return None


def _hold_days(date_acquired: date, date_sold: date) -> int:
    return (date_sold - date_acquired).days


def closed_round_trips(
    conn: sqlite3.Connection, *, year: Optional[int] = None,
) -> List[Dict]:
    """Pair every buy with its matching sell via signal_id; return
    closed round-trips. Rejected / canceled fills are excluded.

    When `year` is set, only round-trips whose SELL date falls in that
    calendar year are returned (matching how the IRS gates the form).
    """
    rows = conn.execute(
        "SELECT signal_id, side, symbol, qty, fill_price, submitted_at, "
        "       filled_at, status "
        "  FROM paper_trades "
        " WHERE status NOT IN ('rejected', 'canceled') "
        " ORDER BY signal_id ASC, submitted_at ASC, id ASC"
    ).fetchall()
    by_signal: Dict[int, Dict[str, list]] = defaultdict(lambda: {"buys": [], "sells": []})
    for r in rows:
        sid = r["signal_id"]
        if sid is None:
            continue
        bucket = by_signal[sid]["buys" if (r["side"] or "").lower() == "buy" else "sells"]
        bucket.append(dict(r))

    round_trips: List[Dict] = []
    for sid, sides in by_signal.items():
        if not sides["buys"] or not sides["sells"]:
            continue
        buy = sides["buys"][0]
        sell = sides["sells"][0]
        date_acquired = (_parse_ts(buy.get("filled_at"))
                         or _parse_ts(buy.get("submitted_at")))
        date_sold = (_parse_ts(sell.get("filled_at"))
                      or _parse_ts(sell.get("submitted_at")))
        if date_acquired is None or date_sold is None:
            continue
        if year is not None and date_sold.year != int(year):
            continue
        qty = float(buy.get("qty") or sell.get("qty") or 0)
        if qty <= 0:
            continue
        cost_basis = round(qty * float(buy.get("fill_price") or 0), 2)
        proceeds = round(qty * float(sell.get("fill_price") or 0), 2)
        gain_loss = round(proceeds - cost_basis, 2)
        round_trips.append({
            "signal_id": sid,
            "symbol": buy.get("symbol"),
            "qty": qty,
            "date_acquired": date_acquired,
            "date_sold": date_sold,
            "cost_basis": cost_basis,
            "proceeds": proceeds,
            "gain_loss": gain_loss,
            "hold_days": _hold_days(date_acquired, date_sold),
        })
    return round_trips


def split_short_long(round_trips: List[Dict]) -> Dict[str, List[Dict]]:
    """Long-term = held >= 365 days (IRS rule, day-count, not month-count)."""
    out = {"short_term": [], "long_term": []}
    for rt in round_trips:
        bucket = "long_term" if rt["hold_days"] >= LONG_TERM_DAYS else "short_term"
        out[bucket].append(rt)
    return out


def _to_8949_row(rt: Dict) -> Dict[str, str]:
    """Project a round-trip into the IRS Form 8949 column shape."""
    qty_str = f"{rt['qty']:g}"  # strip trailing zeros (.0 → "10")
    return {
        "description": f"{qty_str} {rt['symbol']}",
        "date_acquired": rt["date_acquired"].strftime("%m/%d/%Y"),
        "date_sold": rt["date_sold"].strftime("%m/%d/%Y"),
        "proceeds": f"{rt['proceeds']:.2f}",
        "cost_basis": f"{rt['cost_basis']:.2f}",
        "gain_loss": f"{rt['gain_loss']:.2f}",
    }


def write_csv(rows: List[Dict], path: Path) -> int:
    """Write 8949 rows to CSV. Returns count written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FORM_8949_COLUMNS)
        writer.writeheader()
        for rt in rows:
            writer.writerow(_to_8949_row(rt))
    return len(rows)


def export(
    *,
    year: int,
    out_dir: Path,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    """Top-level export. Returns {short_term_csv, long_term_csv, counts,
    totals}."""
    own_conn = conn is None
    if own_conn:
        conn = db.init_db()
    try:
        rts = closed_round_trips(conn, year=year)
        split = split_short_long(rts)

        out_dir.mkdir(parents=True, exist_ok=True)
        st_path = out_dir / f"form_8949_short_term_{year}.csv"
        lt_path = out_dir / f"form_8949_long_term_{year}.csv"
        write_csv(split["short_term"], st_path)
        write_csv(split["long_term"], lt_path)

        st_total = round(sum(r["gain_loss"] for r in split["short_term"]), 2)
        lt_total = round(sum(r["gain_loss"] for r in split["long_term"]), 2)
        return {
            "year": year,
            "short_term_csv": str(st_path),
            "long_term_csv": str(lt_path),
            "counts": {
                "short_term": len(split["short_term"]),
                "long_term": len(split["long_term"]),
                "total": len(rts),
            },
            "totals": {
                "short_term_gain_loss": st_total,
                "long_term_gain_loss": lt_total,
                "net": round(st_total + lt_total, 2),
            },
        }
    finally:
        if own_conn:
            conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Form 8949 CSV export")
    parser.add_argument("--year", type=int, required=True,
                        help="Tax year (sell-date year). Required.")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "tax",
                        help="Output directory (default: data/tax)")
    args = parser.parse_args(argv)

    summary = export(year=args.year, out_dir=args.out)
    import json
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
