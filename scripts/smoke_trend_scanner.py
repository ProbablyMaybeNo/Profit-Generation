"""smoke_trend_scanner.py — End-to-end paper smoke test for the wide-
universe trend scanner pipeline (milestone 5.5.7.1).

Exercises every wire of the 5.5.x stack against a synthetic universe of
20 fake symbols with deterministic bar shapes:

  - universe loader              (override path with 20 symbols)
  - liquidity filter             (5.5.2.1 — dollar-volume floor)
  - bar fetch                    (synthetic loader matching wide_bars contract)
  - trend_scanner.scan           (5.5.3.1 — wide-universe fires)
  - signal_ranker.rank_signals   (5.5.4.1 — composite score)
  - auto_trader capacity cap     (5.5.4.2 — max_new_entries_per_day)
  - paper_trades tagging         (5.5.6.2 — is_scanner badge)

Output: a pipeline trace + final stats. NOT a unit test — the unit
tests live alongside in tests/test_smoke_trend_scanner.py and exercise
this script's scaffolding.

CLI:
  py -3.13 scripts/smoke_trend_scanner.py
  py -3.13 scripts/smoke_trend_scanner.py --json
  py -3.13 scripts/smoke_trend_scanner.py --cap 3
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import trend_scanner  # noqa: E402
from monitoring import signal_ranker  # noqa: E402


SCANNER_STRATEGY = "trend-donchian-breakout-20"
N_SYNTHETIC_SYMBOLS = 20
# Synthetic universe: 12 strong breakout setups + 8 flat (no fire) + a
# couple intentionally illiquid names so the liquidity filter has work.
BREAKOUT_SYMBOLS = [f"BRK{i:02d}" for i in range(12)]
FLAT_SYMBOLS = [f"FLT{i:02d}" for i in range(6)]
ILLIQUID_SYMBOLS = [f"ILQ{i:02d}" for i in range(2)]
ALL_SYMBOLS = BREAKOUT_SYMBOLS + FLAT_SYMBOLS + ILLIQUID_SYMBOLS

LIQUIDITY_FLOOR_USD = 50_000_000.0
DEFAULT_CAP = 5


def _date_string_index(n: int, end: date) -> List[str]:
    """Build a list of n consecutive business-day date strings (YYYY-MM-DD)
    ending on `end`. We use plain strings rather than a DatetimeIndex
    because the scanner persists `bar_ts = signals.index[-1]` and
    auto_trader.process_signals expects `bar_ts = asof.isoformat()`
    on '1d' intervals — keeping the index as a date-only string makes
    the two sides agree.
    """
    idx_ts = pd.date_range(end=pd.Timestamp(end), periods=n, freq="B")
    return [t.date().isoformat() for t in idx_ts]


def _breakout_bars(n: int = 60, peak: float = 150.0,
                     end: Optional[date] = None) -> pd.DataFrame:
    """Flat 50 sessions then a 10-bar ramp — Donchian-20 fires on last bar.

    `end` controls the bar-index endpoint; defaults to today so the
    last bar's bar_ts matches the auto_trader's `asof = date.today()`
    SELECT WHERE bar_ts = ?.
    """
    end = end or date.today()
    idx = _date_string_index(n, end)
    closes = np.concatenate([
        np.full(n - 10, 100.0),
        np.linspace(105.0, peak, 10),
    ])
    return pd.DataFrame({
        "open": closes,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": np.full(n, 10_000_000),
    }, index=idx)


def _flat_bars(n: int = 60, end: Optional[date] = None) -> pd.DataFrame:
    """No movement — no Donchian breakout fires."""
    end = end or date.today()
    idx = _date_string_index(n, end)
    return pd.DataFrame({
        "open": [100.0] * n, "high": [100.5] * n,
        "low": [99.5] * n, "close": [100.0] * n,
        "volume": [10_000_000] * n,
    }, index=idx)


def _build_universe_bars() -> Dict[str, pd.DataFrame]:
    """Synthetic universe — every symbol gets a deterministic bar set."""
    bars: Dict[str, pd.DataFrame] = {}
    for sym in BREAKOUT_SYMBOLS:
        bars[sym] = _breakout_bars()
    for sym in FLAT_SYMBOLS:
        bars[sym] = _flat_bars()
    for sym in ILLIQUID_SYMBOLS:
        bars[sym] = _breakout_bars()
    return bars


def _seed_liquidity_snapshots(conn) -> None:
    """High dollar volume for breakout + flat names, low for illiquid ones."""
    today = date.today().isoformat()
    for sym in BREAKOUT_SYMBOLS + FLAT_SYMBOLS:
        db.upsert_liquidity_snapshot(
            conn, symbol=sym, as_of_date=today,
            avg_dollar_volume_20d=200_000_000.0, last_close=100.0,
        )
    for sym in ILLIQUID_SYMBOLS:
        db.upsert_liquidity_snapshot(
            conn, symbol=sym, as_of_date=today,
            avg_dollar_volume_20d=5_000_000.0, last_close=100.0,
        )


def _seed_strategy_with_edge(conn) -> None:
    """Insert the donchian-breakout strategy with enough closed outcomes
    that auto_trader.eligibility passes the default thresholds."""
    db.upsert_strategy(conn, {
        "extra": {
            "strategy_id": SCANNER_STRATEGY,
            "strategy_class": "trend",
            "pyramidable": True,
        },
    })
    # +2 / +1% repeated → mean positive, low stdev → high sharpe.
    base = date(2023, 1, 1)
    for i, ret in enumerate([2.0, 1.0] * 20):
        d = (base + timedelta(days=i)).isoformat()
        sig = db.record_signal(
            conn, strategy_id=SCANNER_STRATEGY,
            symbol="SEED", bar_ts=d,
            signal_type="long_entry", close=100.0, bar_interval="1d",
            ts=f"{d}T10:00:00",
        )
        if sig is None:
            continue
        db.open_outcome(conn, signal_id=sig,
                        entry_ts=d, entry_price=100.0)
        exit_d = (base + timedelta(days=i + 5)).isoformat()
        db.close_outcome(conn, signal_id=sig,
                          exit_ts=exit_d,
                          exit_price=100.0 * (1 + ret / 100),
                          exit_reason="long_exit_signal", bars_held=5)


def _account_summary_stub():
    return {
        "portfolio_value": 100_000.0, "cash": 100_000.0,
        "equity": 100_000.0, "buying_power": 100_000.0,
        "equity_at_open": 100_000.0, "last_equity": 100_000.0,
    }


def _run_pipeline(cap: int = DEFAULT_CAP) -> Dict:
    """Walk the full scanner → ranker → trader pipeline. Returns the
    trace dict the CLI / tests inspect.
    """
    tmp = Path(tempfile.mkdtemp()) / "trading.db"
    import data.db as dbmod
    dbmod.DB_FILE = tmp
    conn = db.init_db(tmp)
    at.is_paper_mode = lambda: True

    _seed_liquidity_snapshots(conn)
    _seed_strategy_with_edge(conn)
    bars = _build_universe_bars()

    # --- universe loader path ---
    from monitoring import liquidity
    universe = list(ALL_SYMBOLS)
    filtered = liquidity.filter_by_dollar_volume(
        universe, min_usd=LIQUIDITY_FLOOR_USD, conn=conn,
    )
    skipped_liquidity = sorted(set(universe) - set(filtered))

    # --- scanner ---
    declaration = {
        "id": SCANNER_STRATEGY,
        "compute": "compute_donchian_breakout_20",
        "strategy_class": "trend",
        "active_in_regimes": ["trending_up", "trending_down", "mixed"],
        "pyramidable": True,
    }
    def bar_loader(symbols, lookback):
        return {s: bars[s] for s in symbols if s in bars}

    fires = trend_scanner.scan_trend_universe(
        declarations=[declaration],
        universe_override=filtered,
        bar_loader=bar_loader,
        conn=conn,
    )
    entry_fires = [f for f in fires if f["signal_type"] == "long_entry"]

    # --- ranker (independent computation for trace; the auto_trader
    # will rank again internally) ---
    sharpe_map = signal_ranker.sharpe_lookup_from_db(
        {f["strategy_id"] for f in entry_fires}, conn=conn,
    )
    dvol_map = signal_ranker.dollar_volume_lookup_from_db(
        {f["symbol"] for f in entry_fires}, conn=conn,
    )
    ranked = signal_ranker.rank_signals(
        entry_fires, regime="trending_up",
        strategy_decls=[declaration],
        sharpe_by_strategy=sharpe_map,
        dollar_volume_by_symbol=dvol_map,
    )

    # --- auto_trader with capacity cap ---
    from monitoring import config as cfg_mod
    from monitoring import regime_router as rr_mod
    cfg_mod.TRACKED_STRATEGIES = [declaration]
    rr_mod.latest_regime = lambda c: "trending_up"

    submitted = []
    def fake_market(client, *, symbol, qty, side, client_order_id=None):
        submitted.append({"symbol": symbol, "qty": qty, "side": side})
        order = MagicMock()
        order.id = f"smoke-{len(submitted)}"
        order.status = "accepted"
        order.submitted_at = f"2026-01-01T20:30:00Z"
        order.filled_avg_price = None
        return order
    at._submit_market_order = fake_market

    settings = {
        "enabled": True, "dry_run": False,
        "min_outcomes": 5, "min_mean_ret_pct": 0.0,
        "min_sharpe_ish": 0.10,
        "max_position_usd": 1000,
        "max_new_entries_per_day": cap,
        "cool_down_losers": 0, "earnings_veto_days": 0,
        "skip_intraday_signals": True,
        # Raise per-strategy cap so the daily-entry cap is the binding
        # constraint we want to verify here.
        "risk": {"max_open_per_strategy": 999},
    }
    def _bars_for_symbol(sym):
        df = bars.get(sym)
        if df is None:
            return []
        return [
            {"open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]),
             "volume": int(r["volume"])}
            for _, r in df.iterrows()
        ]

    result = at.process_signals(
        conn, asof=date.today(), settings=settings,
        client=MagicMock(),
        bars_fetcher=_bars_for_symbol,
        account_summary_fn=_account_summary_stub,
    )

    actions = result.get("actions", [])
    buys = [a for a in actions if a.get("action") in ("BUY", "DRY_BUY")]
    skip_capacity = [a for a in actions if a.get("action") == "SKIP_CAPACITY"]
    skip_ineligible = [a for a in actions
                        if a.get("action") == "SKIP_INELIGIBLE"]
    action_tally: Dict[str, int] = {}
    for a in actions:
        k = a.get("action", "?")
        action_tally[k] = action_tally.get(k, 0) + 1

    # --- paper-trades tagging ---
    paper_rows = conn.execute(
        "SELECT pt.symbol, s.extra_json FROM paper_trades pt "
        "  LEFT JOIN signals s ON s.id = pt.signal_id "
        " WHERE pt.side='buy'"
    ).fetchall()
    tagged_scanner = 0
    for r in paper_rows:
        raw = r["extra_json"] or ""
        if '"source": "trend_scanner"' in raw or '"wide_universe": true' in raw:
            tagged_scanner += 1

    trace = {
        "n_universe": len(universe),
        "n_after_liquidity": len(filtered),
        "skipped_liquidity": skipped_liquidity,
        "n_scanner_fires": len(entry_fires),
        "fired_symbols": sorted({f["symbol"] for f in entry_fires}),
        "ranked_top_5": [
            {"symbol": r["symbol"], "score": r["score"]}
            for r in ranked[:5]
        ],
        "cap": cap,
        "n_buys": len(buys),
        "buy_symbols": sorted({b["symbol"] for b in buys}),
        "n_skip_capacity": len(skip_capacity),
        "n_skip_ineligible": len(skip_ineligible),
        "n_paper_trades_scanner_tagged": tagged_scanner,
        "n_paper_trades_total": len(paper_rows),
        "action_tally": action_tally,
    }
    return trace


def _format_trace(trace: Dict) -> str:
    out: List[str] = []
    out.append("=" * 78)
    out.append("SMOKE TEST — wide-universe trend scanner pipeline")
    out.append("=" * 78)
    out.append(f"  universe size:         {trace['n_universe']}")
    out.append(f"  after liquidity:       {trace['n_after_liquidity']}")
    out.append(f"  skipped by liquidity:  {len(trace['skipped_liquidity'])} "
               f"({', '.join(trace['skipped_liquidity'])})")
    out.append(f"  scanner fires:         {trace['n_scanner_fires']}")
    out.append(f"  fired symbols:         {', '.join(trace['fired_symbols'])}")
    out.append("")
    out.append("  Top-5 by ranker score:")
    for r in trace["ranked_top_5"]:
        out.append(f"    {r['symbol']:>8s}  score={r['score']:.3f}")
    out.append("")
    out.append(f"  cap (max_new_entries):  {trace['cap']}")
    out.append(f"  paper buys submitted:   {trace['n_buys']}")
    out.append(f"  paper buy symbols:      {', '.join(trace['buy_symbols'])}")
    out.append(f"  SKIP_CAPACITY:          {trace['n_skip_capacity']}")
    out.append(f"  SKIP_INELIGIBLE:        {trace['n_skip_ineligible']}")
    out.append(f"  scanner-tagged trades:  {trace['n_paper_trades_scanner_tagged']} "
               f"/ {trace['n_paper_trades_total']}")
    out.append("  action tally:")
    for k, v in sorted(trace.get("action_tally", {}).items()):
        out.append(f"    {k:<32s} {v}")
    out.append("=" * 78)
    # Self-assertions — print PASS/FAIL summary so a quick eyeball works.
    failures: List[str] = []
    if trace["n_scanner_fires"] < 5:
        failures.append("expected ≥5 scanner fires (got "
                         f"{trace['n_scanner_fires']})")
    if not trace["skipped_liquidity"]:
        failures.append("liquidity filter dropped nothing — expected ILQ*")
    if trace["n_buys"] != min(trace["cap"], trace["n_scanner_fires"]):
        failures.append(f"capacity cap not honoured: buys={trace['n_buys']} "
                         f"cap={trace['cap']} fires={trace['n_scanner_fires']}")
    if trace["n_paper_trades_scanner_tagged"] != trace["n_buys"]:
        failures.append("not all paper trades carry the scanner tag")
    if failures:
        out.append("FAIL:")
        for f in failures:
            out.append(f"  - {f}")
    else:
        out.append("PASS — every pipeline stage fired as expected.")
    return "\n".join(out)


def smoke_test(*, cap: int = DEFAULT_CAP) -> Dict:
    """Public entrypoint exercised by the unit tests."""
    return _run_pipeline(cap=cap)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP,
                        help="max_new_entries_per_day for the cap stage")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of human log")
    args = parser.parse_args()
    trace = _run_pipeline(cap=args.cap)
    if args.json:
        print(json.dumps(trace, indent=2, default=str))
        return
    print(_format_trace(trace))


if __name__ == "__main__":
    main()
