"""
backtest_candle_continuation.py — Stage 2 GO-GATE backtest for the intraday
trend-following build (Track B, docs/INTRADAY_TREND_BUILD_PLAN.md).

OFFLINE research only. Reads the cached historical bars produced by
scripts/collect_intraday_history.py (data/intraday_history_<interval>.pkl) and
runs strategies.intraday.candle_continuation.compute_candle_continuation per
symbol, modelling the live execution rules so the backtest matches the wired
behaviour as closely as a bar-resolution sim can:

  * Entry  — strategy long_entry on bar i fills at bar i+1 OPEN (next-bar-open,
             matching backtest/engine.py). One position at a time per symbol.
  * Init   — ATR initial stop at entry - 2.5 * ATR14 (monitoring/stops.py
             conventions; compute_atr over the bars up to and including the
             signal bar). Quantised with quantize_stop_price.
  * Trail  — ratcheting ATR trailing stop mirroring monitoring/trailing_stops
             atr_trail: stop = HH(since entry) - 3.0 * ATR14, ratcheted UP only
             (never loosens), seeded from the initial stop.
  * Exit   — first of, evaluated per bar after entry:
               1. trailing/initial stop crossed intrabar (bar low <= stop)
                  -> exit at the stop price,
               2. strategy long_exit signalled on the bar -> exit next bar OPEN,
               3. session end (last bar of the calendar day) -> flat at close.
             No overnight holds — intraday strategy.

Outputs data/intraday_continuation_backtest.csv with per-symbol AND aggregate
rows: trades, win_rate, avg_win_pct, avg_loss_pct, profit_factor,
expectancy_pct, max_drawdown_pct, avg_bars_held. Prints an ASCII summary table.

CLI:
  py -3.13 -m scripts.backtest_candle_continuation
  py -3.13 -m scripts.backtest_candle_continuation --interval 15m
  py -3.13 -m scripts.backtest_candle_continuation --interval 5m --out data/foo.csv
"""

from __future__ import annotations

import argparse
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring.stops import compute_atr, quantize_stop_price  # noqa: E402
from monitoring.trailing_stops import ratchet  # noqa: E402
from strategies.intraday.candle_continuation import (  # noqa: E402
    compute_candle_continuation,
)

INITIAL_STOP_ATR_MULT = 2.5
TRAIL_ATR_MULT = 3.0
ATR_PERIOD = 14

OUT_COLUMNS = [
    "symbol", "trades", "win_rate", "avg_win_pct", "avg_loss_pct",
    "profit_factor", "expectancy_pct", "max_drawdown_pct", "avg_bars_held",
]


@dataclass
class Trade:
    symbol: str
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    exit_reason: str
    bars_held: int

    @property
    def return_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price


def _atr_at(highs, lows, closes, end_idx: int, period: int = ATR_PERIOD):
    """ATR14 over the bars up to and including end_idx (inclusive). Returns
    None when fewer than period+1 bars are available — same contract as
    monitoring.stops.compute_atr."""
    lo = max(0, end_idx - period)
    rows = [
        {"high": highs[i], "low": lows[i], "close": closes[i]}
        for i in range(lo, end_idx + 1)
    ]
    return compute_atr(rows, period=period)


def simulate_symbol(symbol: str, df: pd.DataFrame, **strat_overrides) -> List[Trade]:
    """Run the continuation strategy + execution model over one symbol's bars.
    Pure given `df` (no IO). Returns the list of closed trades."""
    sig = compute_candle_continuation(df, **strat_overrides)
    opens = sig["open"].to_numpy(dtype=float)
    highs = sig["high"].to_numpy(dtype=float)
    lows = sig["low"].to_numpy(dtype=float)
    closes = sig["close"].to_numpy(dtype=float)
    long_entry = sig["long_entry"].to_numpy(dtype=bool)
    long_exit = sig["long_exit"].to_numpy(dtype=bool)

    if isinstance(sig.index, pd.DatetimeIndex):
        sessions = sig.index.normalize()
        same_session = [
            (i + 1 < len(sessions)) and (sessions[i + 1] == sessions[i])
            for i in range(len(sessions))
        ]
    else:
        same_session = [i + 1 < len(closes) for i in range(len(closes))]

    n = len(closes)
    trades: List[Trade] = []
    i = 0
    while i < n - 1:
        if not long_entry[i]:
            i += 1
            continue
        # Entry signal on bar i -> fill at bar i+1 open.
        entry_idx = i + 1
        entry_price = opens[entry_idx]
        if entry_price <= 0:
            i += 1
            continue

        atr = _atr_at(highs, lows, closes, entry_idx)
        stop = None
        if atr is not None and atr > 0:
            stop = quantize_stop_price(entry_price - INITIAL_STOP_ATR_MULT * atr)
        hh = highs[entry_idx]

        exit_idx = None
        exit_price = None
        exit_reason = None
        j = entry_idx
        while j < n:
            # 1. stop crossed intrabar (initial or trailed)
            if stop is not None and lows[j] <= stop:
                exit_idx = j
                exit_price = stop
                exit_reason = "trailing_stop" if j > entry_idx else "initial_stop"
                break
            # session boundary: this bar is the last of its session (or the
            # last bar of the data) -> flat at close, never hold overnight.
            is_session_end = (j >= len(same_session)) or (not same_session[j])
            if is_session_end:
                exit_idx = j
                exit_price = closes[j]
                exit_reason = "eod_close"
                break
            # 2. strategy long_exit on bar j -> exit next bar open
            if long_exit[j] and j + 1 < n:
                exit_idx = j + 1
                exit_price = opens[j + 1]
                exit_reason = "long_exit_signal"
                break
            # otherwise advance: ratchet the trail off the new HH using ATR14
            hh = max(hh, highs[j])
            atr_j = _atr_at(highs, lows, closes, j)
            if atr_j is not None and atr_j > 0:
                proposed = quantize_stop_price(hh - TRAIL_ATR_MULT * atr_j)
                if proposed is not None:
                    stop = ratchet(stop, proposed, side="long")
            j += 1

        if exit_idx is None:
            # ran off the end of the data — flat at last bar close (no overnight)
            exit_idx = n - 1
            exit_price = closes[n - 1]
            exit_reason = "eod_close"

        trades.append(Trade(
            symbol=symbol, entry_idx=entry_idx, exit_idx=exit_idx,
            entry_price=float(entry_price), exit_price=float(exit_price),
            exit_reason=exit_reason, bars_held=int(exit_idx - entry_idx),
        ))
        i = exit_idx + 1
    return trades


def metrics_for(symbol: str, trades: List[Trade]) -> Dict:
    """Pure: per-symbol performance metrics from a list of trades."""
    n = len(trades)
    if n == 0:
        return {"symbol": symbol, "trades": 0, "win_rate": 0.0,
                "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "profit_factor": 0.0,
                "expectancy_pct": 0.0, "max_drawdown_pct": 0.0,
                "avg_bars_held": 0.0}
    rets = [t.return_pct for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    win_rate = len(wins) / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0
    expectancy = sum(rets) / n
    # Max drawdown of the cumulative per-trade equity curve (compounded).
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rets:
        equity *= (1.0 + r)
        peak = max(peak, equity)
        dd = (equity - peak) / peak
        max_dd = min(max_dd, dd)
    avg_bars = sum(t.bars_held for t in trades) / n
    return {
        "symbol": symbol,
        "trades": n,
        "win_rate": round(win_rate, 4),
        "avg_win_pct": round(avg_win * 100, 4),
        "avg_loss_pct": round(avg_loss * 100, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf")
        else float("inf"),
        "expectancy_pct": round(expectancy * 100, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "avg_bars_held": round(avg_bars, 2),
    }


def aggregate_metrics(all_trades: List[Trade]) -> Dict:
    row = metrics_for("ALL", all_trades)
    return row


def run_backtest(data: Dict[str, pd.DataFrame],
                 **strat_overrides) -> pd.DataFrame:
    """Run every symbol, return a DataFrame with per-symbol rows + an ALL
    aggregate row at the bottom. Columns == OUT_COLUMNS."""
    rows: List[Dict] = []
    pooled: List[Trade] = []
    for sym in sorted(data):
        trades = simulate_symbol(sym, data[sym], **strat_overrides)
        pooled.extend(trades)
        rows.append(metrics_for(sym, trades))
    rows.append(aggregate_metrics(pooled))
    return pd.DataFrame(rows, columns=OUT_COLUMNS)


def _fmt_pf(v) -> str:
    if v == float("inf"):
        return "inf"
    return f"{v:.2f}"


def render_table(df: pd.DataFrame) -> str:
    """ASCII-only summary table (Windows cp1252 safe — no unicode)."""
    header = (f"{'symbol':<7} {'trades':>6} {'win%':>6} {'avgW%':>7} "
              f"{'avgL%':>7} {'PF':>6} {'exp%':>7} {'maxDD%':>8} {'barsHld':>7}")
    lines = [header, "-" * len(header)]
    for _, r in df.iterrows():
        lines.append(
            f"{r['symbol']:<7} {int(r['trades']):>6} "
            f"{r['win_rate'] * 100:>6.1f} {r['avg_win_pct']:>7.2f} "
            f"{r['avg_loss_pct']:>7.2f} {_fmt_pf(r['profit_factor']):>6} "
            f"{r['expectancy_pct']:>7.3f} {r['max_drawdown_pct']:>8.2f} "
            f"{r['avg_bars_held']:>7.1f}"
        )
    return "\n".join(lines)


def load_history(interval: str, *, root: Path = ROOT) -> Dict[str, pd.DataFrame]:
    path = root / "data" / f"intraday_history_{interval}.pkl"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found - run scripts.collect_intraday_history first."
        )
    with open(path, "rb") as fh:
        return pickle.load(fh)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=str, default="5m",
                        help="Which cached history to backtest (default 5m).")
    parser.add_argument("--out", type=str, default=None,
                        help="Output CSV path "
                             "(default data/intraday_continuation_backtest.csv).")
    args = parser.parse_args(argv)

    try:
        data = load_history(args.interval)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1
    if not data:
        print(f"ERROR: no symbols in cached {args.interval} history.")
        return 1

    df = run_backtest(data)
    out_path = Path(args.out) if args.out else (
        ROOT / "data" / "intraday_continuation_backtest.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    print(f"=== candle_continuation backtest ({args.interval}, "
          f"{len(data)} symbols) ===")
    print(render_table(df))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
