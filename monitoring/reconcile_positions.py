"""
reconcile_positions.py — Compare Alpaca-reported open positions to the
paper_trades table and surface any drift.

Drift = our model of open positions ≠ broker's source of truth. Causes:
- order accepted but filled later than we recorded
- manual close at the broker we didn't capture
- bug in record_paper_trade
- Alpaca outage that swallowed a fill webhook

What it does:
- queries Alpaca `list_positions()` (paper account)
- queries paper_trades for BUYs that have no later SELL for the same
  (strategy_id, symbol), filtering out canceled/rejected legs
- computes 3 disjoint drift sets:
    only_in_alpaca   — broker holds the position, our DB doesn't
    only_in_db       — our DB thinks open, broker doesn't
    qty_mismatch     — both sides know about it but qty disagrees
- writes the summary to `data/last_reconcile.json` so daily_report can
  splice a "Position Reconciliation" section into the next post
- fires a Telegram alert when any drift exists

CLI:
  py -3.13 -m monitoring.reconcile_positions          # run once, print + persist
  py -3.13 -m monitoring.reconcile_positions --json   # machine-readable
  py -3.13 -m monitoring.reconcile_positions --no-alert  # skip telegram
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402

RECONCILE_SNAPSHOT = ROOT / "data" / "last_reconcile.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_open_positions(conn) -> Dict[str, Dict]:
    """Open paper-trade positions: BUYs without a later SELL for the
    same (strategy, symbol). Returns {symbol: {strategy_id, qty}}.

    Aggregates qty across strategies for the same symbol (Alpaca reports
    one row per symbol, not per strategy — so we sum here for a like-vs-like
    comparison).
    """
    rows = conn.execute(
        "SELECT id, strategy_id, symbol, qty, submitted_at "
        "  FROM paper_trades "
        " WHERE side='buy' "
        "   AND status IN ('filled', 'partially_filled', 'accepted', 'new') "
        " ORDER BY submitted_at ASC",
    ).fetchall()
    out: Dict[str, Dict] = {}
    for r in rows:
        sym = r["symbol"]
        sid = r["strategy_id"]
        qty = float(r["qty"] or 0)
        if qty <= 0:
            continue
        # Has a later sell for this (sid, sym) closed it out?
        later_sell = conn.execute(
            "SELECT 1 FROM paper_trades WHERE strategy_id=? AND symbol=? "
            "  AND side='sell' AND submitted_at > ? "
            "  AND status NOT IN ('canceled', 'rejected') LIMIT 1",
            (sid, sym, r["submitted_at"]),
        ).fetchone()
        if later_sell is not None:
            continue
        bucket = out.setdefault(sym, {"qty": 0.0, "strategies": []})
        bucket["qty"] += qty
        bucket["strategies"].append(sid)
    return out


def alpaca_open_positions(client) -> Dict[str, Dict]:
    """Wrap client.list_positions / get_all_positions and normalise to
    {symbol: {qty, avg_entry_price}}."""
    # alpaca-py exposes `get_all_positions`; older clients had `list_positions`.
    getter = (getattr(client, "get_all_positions", None)
              or getattr(client, "list_positions", None))
    if getter is None:
        raise RuntimeError("alpaca client has neither get_all_positions "
                            "nor list_positions")
    positions = getter() or []
    out: Dict[str, Dict] = {}
    for p in positions:
        sym = getattr(p, "symbol", None) or (p.get("symbol") if isinstance(p, dict) else None)
        qty_raw = getattr(p, "qty", None) or (p.get("qty") if isinstance(p, dict) else None)
        avg_raw = (getattr(p, "avg_entry_price", None)
                    or (p.get("avg_entry_price") if isinstance(p, dict) else None))
        try:
            qty = float(qty_raw or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            avg = float(avg_raw) if avg_raw is not None else None
        except (TypeError, ValueError):
            avg = None
        if sym and qty > 0:
            out[sym] = {"qty": qty, "avg_entry_price": avg}
    return out


def compute_drift(db_pos: Dict[str, Dict],
                  alpaca_pos: Dict[str, Dict]) -> Dict:
    """Pure function — no I/O. Compares two normalised dicts and returns
    {only_in_alpaca, only_in_db, qty_mismatch, agree_count, drift_count}."""
    db_syms = set(db_pos.keys())
    al_syms = set(alpaca_pos.keys())

    only_in_alpaca: List[Dict] = sorted(
        [{"symbol": s, "qty": alpaca_pos[s]["qty"]}
         for s in al_syms - db_syms],
        key=lambda x: x["symbol"],
    )
    only_in_db: List[Dict] = sorted(
        [{"symbol": s, "qty": db_pos[s]["qty"],
          "strategies": db_pos[s].get("strategies", [])}
         for s in db_syms - al_syms],
        key=lambda x: x["symbol"],
    )
    qty_mismatch: List[Dict] = []
    agree = 0
    for s in db_syms & al_syms:
        db_q = float(db_pos[s]["qty"])
        al_q = float(alpaca_pos[s]["qty"])
        if abs(db_q - al_q) > 1e-6:
            qty_mismatch.append({
                "symbol": s,
                "db_qty": db_q,
                "alpaca_qty": al_q,
                "delta": round(al_q - db_q, 4),
            })
        else:
            agree += 1
    qty_mismatch.sort(key=lambda x: x["symbol"])
    drift_count = len(only_in_alpaca) + len(only_in_db) + len(qty_mismatch)
    return {
        "agree_count": agree,
        "drift_count": drift_count,
        "only_in_alpaca": only_in_alpaca,
        "only_in_db": only_in_db,
        "qty_mismatch": qty_mismatch,
    }


def format_section(result: Dict) -> str:
    """Markdown chunk suitable for splicing into daily_report's body."""
    drift = result["drift_count"]
    if drift == 0:
        return (
            f"### Position Reconciliation\n\n"
            f"No drift. {result['agree_count']} symbol(s) match between "
            f"Alpaca and paper_trades as of {result['as_of']}.\n"
        )
    lines = [
        f"### Position Reconciliation",
        "",
        f"⚠️ **{drift} drift(s) detected** as of {result['as_of']} "
        f"({result['agree_count']} agree).",
        "",
    ]
    if result["only_in_alpaca"]:
        lines.append("**Only in Alpaca (broker holds, DB doesn't):**")
        for r in result["only_in_alpaca"]:
            lines.append(f"- {r['symbol']} × {r['qty']:g}")
        lines.append("")
    if result["only_in_db"]:
        lines.append("**Only in DB (DB thinks open, broker doesn't):**")
        for r in result["only_in_db"]:
            strats = ", ".join(r.get("strategies") or []) or "?"
            lines.append(f"- {r['symbol']} × {r['qty']:g}  ({strats})")
        lines.append("")
    if result["qty_mismatch"]:
        lines.append("**Quantity mismatch:**")
        for r in result["qty_mismatch"]:
            sign = "+" if r["delta"] >= 0 else ""
            lines.append(f"- {r['symbol']}: db={r['db_qty']:g} "
                          f"alpaca={r['alpaca_qty']:g} ({sign}{r['delta']:g})")
        lines.append("")
    return "\n".join(lines)


def format_telegram_alert(result: Dict) -> str:
    drift = result["drift_count"]
    if drift == 0:
        return ""
    parts = [f"⚠️ Position drift detected: {drift} symbol(s)"]
    for r in result["only_in_alpaca"][:5]:
        parts.append(f"• ALPACA-only: {r['symbol']} ×{r['qty']:g}")
    for r in result["only_in_db"][:5]:
        parts.append(f"• DB-only: {r['symbol']} ×{r['qty']:g}")
    for r in result["qty_mismatch"][:5]:
        parts.append(f"• mismatch: {r['symbol']} db={r['db_qty']:g} "
                      f"alpaca={r['alpaca_qty']:g}")
    return "\n".join(parts)


def _save_snapshot(result: Dict, *, path: Optional[Path] = None) -> None:
    p = Path(path) if path is not None else RECONCILE_SNAPSHOT
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


def load_snapshot(*, path: Optional[Path] = None) -> Optional[Dict]:
    """Helper for daily_report to read the latest reconciliation result.
    Returns None when the file is missing or unparseable."""
    p = Path(path) if path is not None else RECONCILE_SNAPSHOT
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def reconcile(*,
              conn=None,
              client=None,
              alpaca_positions_fn: Optional[Callable] = None,
              send_fn: Optional[Callable] = None,
              save_path: Optional[Path] = None,
              now_fn: Optional[Callable] = None,
              alert: bool = True) -> Dict:
    """End-to-end run: pull both sides, compute drift, persist snapshot,
    alert on drift. Returns the full result dict.

    All side-effects are pluggable for tests.
    """
    now_fn = now_fn or _utc_now_iso
    own_conn = False
    if conn is None:
        conn = db.init_db()
        own_conn = True
    try:
        db_pos = db_open_positions(conn)
    finally:
        if own_conn:
            conn.close()
    if alpaca_positions_fn is None:
        from config.utils import get_alpaca_client
        client = client or get_alpaca_client()
        alpaca_pos = alpaca_open_positions(client)
    else:
        alpaca_pos = alpaca_positions_fn()
    result = compute_drift(db_pos, alpaca_pos)
    result["as_of"] = now_fn()
    _save_snapshot(result, path=save_path)
    if alert and result["drift_count"] > 0:
        text = format_telegram_alert(result)
        if send_fn is None:
            from monitoring import telegram_alerter
            send_fn = telegram_alerter.send_message
        try:
            send_fn(text)
        except Exception as e:
            log(f"reconcile: telegram alert failed: {e}", "WARNING")
    return result


def main():
    parser = argparse.ArgumentParser(description="Position reconciliation.")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of formatted markdown")
    parser.add_argument("--no-alert", action="store_true",
                        help="skip Telegram alert even on drift")
    args = parser.parse_args()
    result = reconcile(alert=not args.no_alert)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_section(result))
        if result["drift_count"] > 0:
            print("\n" + format_telegram_alert(result))
    sys.exit(1 if result["drift_count"] > 0 else 0)


if __name__ == "__main__":
    main()
