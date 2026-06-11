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


RECONCILED_NO_POSITION_EXIT_REASON = "reconciled_no_position"


def _open_outcomes_with_symbols(conn) -> List[Dict]:
    """Every OPEN outcome joined to its entry signal's symbol + interval.

    One row per open outcome carrying signal_id, symbol, strategy_id,
    bar_interval, entry_ts, entry_price. Used by the broker-reconcile sweep
    (A3) to find outcomes whose real position is already gone.
    """
    rows = conn.execute(
        "SELECT o.signal_id AS signal_id, o.entry_ts AS entry_ts, "
        "       o.entry_price AS entry_price, s.symbol AS symbol, "
        "       s.strategy_id AS strategy_id, s.bar_interval AS bar_interval "
        "  FROM outcomes o JOIN signals s ON s.id = o.signal_id "
        " WHERE o.status='open'"
    ).fetchall()
    return [dict(r) for r in rows]


def _daily_close_mark(symbol: str, daily_bars_fn: Callable) -> Optional[float]:
    """Latest available DAILY-bar close for a symbol, via the system's
    existing daily-bar source (monitoring.wide_bars.fetch_wide_daily_bars).

    Last-resort exit mark for 1d trend orphans (donchian-breakout, ma-cross)
    that have no recorded sell fill, no snapshot row, and no intraday bar —
    the 175 outcomes the A3 sweep honestly skipped on its first live run.

    Returns None on any failure (no bars, fetch error, malformed frame) so
    the caller still SKIPs honestly rather than fabricating a price.
    """
    try:
        frames = daily_bars_fn([symbol])
    except Exception as e:  # noqa: BLE001
        log(f"_daily_close_mark: daily-bar fetch failed for {symbol}: {e}",
            "WARNING")
        return None
    if not frames:
        return None
    df = frames.get(symbol)
    if df is None:
        df = frames.get(str(symbol).upper())
    if df is None or getattr(df, "empty", True):
        return None
    if "close" not in getattr(df, "columns", []):
        return None
    try:
        closes = df["close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1])
    except Exception:  # noqa: BLE001
        return None


def _default_daily_bars_fn(symbols):
    from monitoring.wide_bars import fetch_wide_daily_bars
    return fetch_wide_daily_bars(symbols)


def _last_known_mark(
    conn,
    symbol: str,
    entry_ts,
    *,
    daily_bars_fn: Optional[Callable] = None,
) -> Optional[float]:
    """Best honest exit mark for a symbol whose broker position is gone.

    Resolution order (most position-specific first):
      1. fill_price of a recorded, non-terminal SELL for this symbol;
      2. latest snapshots.close for the symbol;
      3. latest intraday_bars.close for the symbol;
      4. latest DAILY-bar close (wide_bars.fetch_wide_daily_bars) — the
         last resort for 1d trend orphans with none of the above.
    Returns None when no price is available so the caller can SKIP rather
    than fabricate an exit price.
    """
    row = conn.execute(
        "SELECT fill_price FROM paper_trades "
        " WHERE symbol=? AND side='sell' AND fill_price IS NOT NULL "
        "   AND status NOT IN ('canceled','rejected') "
        " ORDER BY COALESCE(filled_at, submitted_at) DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    if row is not None and row["fill_price"] is not None:
        try:
            return float(row["fill_price"])
        except (TypeError, ValueError):
            pass
    row = conn.execute(
        "SELECT close FROM snapshots WHERE symbol=? AND close IS NOT NULL "
        " ORDER BY snapshot_date DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    if row is not None and row["close"] is not None:
        try:
            return float(row["close"])
        except (TypeError, ValueError):
            pass
    try:
        row = conn.execute(
            "SELECT close FROM intraday_bars WHERE symbol=? AND close IS NOT NULL "
            " ORDER BY ts_utc DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    except Exception:
        row = None
    if row is not None and row["close"] is not None:
        try:
            return float(row["close"])
        except (TypeError, ValueError):
            pass
    # 4th / last resort: the system's daily-bar close. Only reached when the
    # three position-specific sources above are all unavailable.
    return _daily_close_mark(symbol, daily_bars_fn or _default_daily_bars_fn)


def sweep_orphan_outcomes(
    conn,
    held_symbols,
    *,
    now_iso: Optional[str] = None,
    daily_bars_fn: Optional[Callable] = None,
) -> Dict:
    """Close OPEN outcomes whose real broker position is already gone (A3).

    `held_symbols` is the set/iterable of symbols the broker currently holds
    (from alpaca_open_positions). An OPEN outcome is an ORPHAN when its
    symbol is NOT in that set — the position closed (stop fill, manual close,
    missed reconcile) but the outcome never closed, so the ledger diverged
    18x from broker reality (audit: 260 open outcomes / 179 symbols vs 14
    real positions).

    Each orphan is closed with exit_reason='reconciled_no_position' at the
    best last-known mark (_last_known_mark), resolving sell-fill -> snapshot
    -> intraday bar -> DAILY-bar close in that precedence. Orphans with NO
    available price (not even a daily close) are SKIPPED (no fabricated exit).
    Outcomes whose symbol IS held are left untouched. Idempotent — closed
    outcomes drop out of the OPEN query. Best-effort per row: one failure
    never aborts the rest.

    The daily-bar fetch (wide_bars) is batched once across all not-held
    candidate symbols, then read per-symbol from that prefetched dict, so a
    175-symbol backlog costs one batched broker call, not 175.

    Returns {scanned, swept, skipped, phantom, held}. `phantom` counts no-fill
    rows quarantined as 'phantom_no_fill' rather than booked at a fake mark.
    """
    held = {str(s).upper() for s in (held_symbols or [])}
    fallback_ts = now_iso or _utc_now_iso()
    candidates = _open_outcomes_with_symbols(conn)

    # Batch-fetch daily bars once for every not-held candidate symbol so the
    # 4th-resort daily-close lookup is a single broker call, not one per row.
    orphan_syms = sorted({
        str(o.get("symbol")).upper() for o in candidates
        if o.get("symbol") is not None
        and str(o.get("symbol")).upper() not in held
    })
    prefetched: Dict[str, object] = {}
    if orphan_syms:
        fetch = daily_bars_fn or _default_daily_bars_fn
        try:
            prefetched = fetch(orphan_syms) or {}
        except Exception as e:  # noqa: BLE001
            log(f"sweep_orphan_outcomes: batched daily-bar fetch failed "
                f"({type(e).__name__}: {e}) — daily fallback unavailable",
                "WARNING")
            prefetched = {}

    def _prefetched_daily(syms):
        return {s: prefetched[s] for s in
                (str(x).upper() for x in syms) if s in prefetched}

    swept = 0
    skipped = 0
    phantom = 0
    for o in candidates:
        sym = o.get("symbol")
        if sym is None:
            skipped += 1
            continue
        if str(sym).upper() in held:
            # The position genuinely still exists — never close it here.
            continue
        # An outcome with NO order at all (neither buy nor sell fill) is a
        # phantom ROW, not an orphan POSITION — there was never a real entry to
        # reconcile. Booking it at a last-known mark would fabricate a win/loss
        # (the bug in docs/TICKET_PHANTOM_OUTCOMES.md), so quarantine it
        # (exit_reason='phantom_no_fill', return NULL) instead. A sell-only
        # orphan (position closed, buy fill unlinked) DOES have a fill here, so
        # it still books at its last-known mark below.
        if not db.signal_has_any_fill(conn, int(o["signal_id"])):
            if db.mark_outcome_phantom(conn, int(o["signal_id"])):
                phantom += 1
                log(f"PHANTOM_NO_FILL quarantined orphan outcome sig "
                    f"{o.get('signal_id')} ({o.get('strategy_id')}/{sym}) — "
                    f"no backing fill, not booked", "INFO")
            continue
        mark = _last_known_mark(conn, sym, o.get("entry_ts"),
                                daily_bars_fn=_prefetched_daily)
        if mark is None:
            skipped += 1
            continue
        try:
            db.close_outcome(
                conn, signal_id=int(o["signal_id"]),
                exit_ts=fallback_ts, exit_price=float(mark),
                exit_reason=RECONCILED_NO_POSITION_EXIT_REASON,
            )
        except Exception as e:
            log(f"sweep_orphan_outcomes: close failed for "
                f"{o.get('strategy_id')}/{sym} sig {o.get('signal_id')}: {e}",
                "WARNING")
            skipped += 1
            continue
        log(f"RECONCILE_NO_POSITION closed orphan outcome sig "
            f"{o.get('signal_id')} ({o.get('strategy_id')}/{sym}) "
            f"@ {mark} (reason={RECONCILED_NO_POSITION_EXIT_REASON})", "INFO")
        swept += 1
    return {"scanned": len(candidates), "swept": swept,
            "skipped": skipped, "phantom": phantom, "held": len(held)}


def reconcile(*,
              conn=None,
              client=None,
              alpaca_positions_fn: Optional[Callable] = None,
              send_fn: Optional[Callable] = None,
              save_path: Optional[Path] = None,
              now_fn: Optional[Callable] = None,
              alert: bool = True,
              sweep_orphans: bool = False,
              sync_fills: bool = True,
              daily_bars_fn: Optional[Callable] = None) -> Dict:
    """End-to-end run: backfill broker fills, pull both sides, compute drift,
    persist snapshot, alert on drift. Returns the full result dict.

    All side-effects are pluggable for tests. When `alpaca_positions_fn` is
    supplied (test path), the broker fill-sync is skipped — tests stub the
    position view directly and never touch a live order endpoint.
    """
    now_fn = now_fn or _utc_now_iso
    own_conn = False
    if conn is None:
        conn = db.init_db()
        own_conn = True
    try:
        # Backfill fills from the broker BEFORE measuring drift so the
        # comparison runs on corrected data. Orders submitted as 'accepted'
        # get their real status / fill_price / filled_at here.
        if sync_fills and alpaca_positions_fn is None:
            try:
                from config.utils import get_alpaca_client
                from monitoring import order_sync
                client = client or get_alpaca_client()
                sync_res = order_sync.sync_order_fills(conn, client)
                if sync_res.get("updated"):
                    log(f"reconcile: order_sync updated {sync_res['updated']} "
                        f"row(s), {sync_res['filled']} newly filled", "INFO")
            except Exception as e:
                log(f"reconcile: order_sync skipped ({type(e).__name__}: {e})",
                    "WARNING")
        db_pos = db_open_positions(conn)
        # Pull broker truth while the conn is still open so the A3 orphan
        # sweep can close OPEN outcomes whose real position is already gone.
        if alpaca_positions_fn is None:
            from config.utils import get_alpaca_client
            client = client or get_alpaca_client()
            alpaca_pos = alpaca_open_positions(client)
        else:
            alpaca_pos = alpaca_positions_fn()
        sweep_res = None
        if sweep_orphans:
            try:
                sweep_res = sweep_orphan_outcomes(
                    conn, set(alpaca_pos.keys()), now_iso=now_fn(),
                    daily_bars_fn=daily_bars_fn,
                )
                if sweep_res.get("swept"):
                    log(f"reconcile: orphan sweep closed {sweep_res['swept']} "
                        f"OPEN outcome(s) with no broker position "
                        f"({sweep_res['skipped']} skipped)", "INFO")
            except Exception as e:
                log(f"reconcile: orphan sweep skipped "
                    f"({type(e).__name__}: {e})", "WARNING")
    finally:
        if own_conn:
            conn.close()
    result = compute_drift(db_pos, alpaca_pos)
    result["as_of"] = now_fn()
    if sweep_res is not None:
        result["orphan_sweep"] = sweep_res
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Position reconciliation.")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of formatted markdown")
    parser.add_argument("--no-alert", action="store_true",
                        help="skip Telegram alert even on drift")
    parser.add_argument("--sweep-orphans", "--backfill", dest="sweep_orphans",
                        action="store_true",
                        help="also close OPEN outcomes whose broker position "
                             "is gone (A3 orphan sweep). Idempotent: already-"
                             "closed outcomes are untouched, held symbols are "
                             "never closed. Used by the nightly Reconcile task "
                             "and for the one-time B2 backlog backfill.")
    args = parser.parse_args(argv)
    result = reconcile(alert=not args.no_alert,
                       sweep_orphans=args.sweep_orphans)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_section(result))
        if result.get("orphan_sweep") is not None:
            s = result["orphan_sweep"]
            print(f"\nOrphan sweep: scanned={s['scanned']} swept={s['swept']} "
                  f"skipped={s['skipped']} phantom={s.get('phantom', 0)} "
                  f"held={s['held']}")
        if result["drift_count"] > 0:
            print("\n" + format_telegram_alert(result))
    sys.exit(1 if result["drift_count"] > 0 else 0)


if __name__ == "__main__":
    main()
