"""
liquidity.py — Dollar-volume liquidity filter for the trend scanner
(milestone 5.5.2.1).

The wide-universe scanner (5.5.3) gets handed ~600 symbols. Many are
illiquid mid-caps where fills slip badly on entries / exits with
trailing stops. This module trims that list to symbols where the
20-day average dollar volume (close × volume) clears a threshold.

Default threshold: $50M/day — keeps liquid mid-caps and above, drops
micro-caps. Configurable per-strategy via the `liquidity_floor_usd`
key on a strategy declaration (5.5.3 wires that up).

Source: `liquidity_snapshots` table in trading.db. Populated daily
by the EOD pipeline (or one-off via this module's CLI). Missing
snapshots cause a symbol to be EXCLUDED conservatively — we'd rather
miss a fire than route to an illiquid name we can't model.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402

DEFAULT_MIN_USD = 50_000_000.0  # $50M/day
DEFAULT_LOOKBACK_DAYS = 20
SNAPSHOT_STALENESS_DAYS = 7  # snapshot older than this is treated as missing


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def filter_by_dollar_volume(
    symbols: Iterable[str],
    *,
    min_usd: float = DEFAULT_MIN_USD,
    conn=None,
    as_of: Optional[date] = None,
    max_staleness_days: int = SNAPSHOT_STALENESS_DAYS,
) -> List[str]:
    """Return symbols whose 20-day avg dollar volume ≥ `min_usd`.

    Missing or stale snapshots → excluded (conservative).
    Result preserves input order (deduped, uppercased).
    """
    syms = []
    seen = set()
    for s in symbols:
        u = s.upper()
        if u and u not in seen:
            seen.add(u)
            syms.append(u)
    if not syms:
        return []

    own_conn = conn is None
    if own_conn:
        conn = db.init_db()
    try:
        snapshots = db.get_liquidity_snapshots(conn, syms)
    finally:
        if own_conn:
            conn.close()

    cutoff = (as_of or date.today()) - timedelta(days=max_staleness_days)
    out: List[str] = []
    for sym in syms:
        snap = snapshots.get(sym)
        if not snap:
            continue
        adv = snap.get("avg_dollar_volume_20d")
        if adv is None or adv < min_usd:
            continue
        snap_date = _parse_date(snap.get("as_of_date"))
        if snap_date is None or snap_date < cutoff:
            continue
        out.append(sym)
    return out


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Populate snapshots (computes ADV from a bar loader)
# ---------------------------------------------------------------------------


def compute_avg_dollar_volume(df, *, window: int = DEFAULT_LOOKBACK_DAYS) -> Optional[float]:
    """Compute the rolling N-day mean of (close * volume) from a bar df."""
    if df is None or len(df) < window:
        return None
    tail = df.iloc[-window:]
    if "close" not in tail.columns or "volume" not in tail.columns:
        return None
    dollar_vol = (tail["close"].astype(float) * tail["volume"].astype(float)).mean()
    if dollar_vol is None or _is_nan(dollar_vol):
        return None
    return float(dollar_vol)


def _is_nan(x) -> bool:
    try:
        return x != x  # NaN != NaN
    except Exception:
        return False


def populate_liquidity_snapshots(
    symbols: Iterable[str],
    *,
    bar_loader=None,
    conn=None,
    as_of: Optional[date] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    fetch_padding_days: int = 14,
) -> Dict[str, Tuple[float, float]]:
    """
    Fetch recent daily bars per symbol, compute 20-day avg dollar volume,
    upsert into liquidity_snapshots. Returns {symbol: (adv, last_close)}.

    `bar_loader` defaults to `backtest.data.load_bars` (1d). Tests inject
    a stub: callable(symbols, start, end, interval) → {sym: df}.
    """
    syms = [s.upper() for s in symbols]
    syms = list(dict.fromkeys(syms))
    if not syms:
        return {}

    asof = as_of or date.today()
    start = (asof - timedelta(days=lookback_days + fetch_padding_days)).isoformat()
    end = asof.isoformat()

    if bar_loader is None:
        from backtest.data import load_bars
        bar_loader = load_bars

    try:
        bars = bar_loader(syms, start, end, "1d")
    except Exception as exc:  # noqa: BLE001 — never fail the EOD run on this
        log(f"liquidity: bar fetch failed ({exc})", level="WARNING")
        return {}

    own_conn = conn is None
    if own_conn:
        conn = db.init_db()

    result: Dict[str, Tuple[float, float]] = {}
    try:
        for sym in syms:
            df = bars.get(sym)
            adv = compute_avg_dollar_volume(df, window=lookback_days)
            if adv is None:
                continue
            try:
                last_close = float(df["close"].iloc[-1])
            except Exception:  # noqa: BLE001
                last_close = None
            db.upsert_liquidity_snapshot(
                conn,
                symbol=sym,
                as_of_date=asof.isoformat(),
                avg_dollar_volume_20d=adv,
                last_close=last_close,
            )
            result[sym] = (adv, last_close if last_close is not None else 0.0)
    finally:
        if own_conn:
            conn.close()

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Liquidity snapshot populator")
    p.add_argument("--universe", default="trend",
                   help="'trend' (default) or comma-separated symbols")
    p.add_argument("--min-usd", type=float, default=DEFAULT_MIN_USD)
    p.add_argument("--show-filter", action="store_true",
                   help="After populating, print the filtered subset")
    args = p.parse_args(argv)

    if args.universe == "trend":
        from monitoring.universe import load_trend_universe
        syms = load_trend_universe()
    else:
        syms = [s.strip().upper() for s in args.universe.split(",") if s.strip()]

    print(f"populating liquidity snapshots for {len(syms)} symbols...")
    populated = populate_liquidity_snapshots(syms)
    print(f"  wrote {len(populated)} rows")

    if args.show_filter:
        filtered = filter_by_dollar_volume(syms, min_usd=args.min_usd)
        print(f"  {len(filtered)} symbols pass ${args.min_usd:,.0f}/day filter")
        print(filtered[:30])
    return 0


if __name__ == "__main__":
    sys.exit(main())
