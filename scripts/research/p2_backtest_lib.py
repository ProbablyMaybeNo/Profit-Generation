"""
p2_backtest_lib.py — Backtest harness + full metrics for the P2 sweep.

Reuses backtest/engine.py (next-bar-open fills, 5bps slippage) and
backtest/portfolio.py. Wraps any compute_fn(df)->df[long_entry,long_exit]
strategy into the engine's Strategy protocol, single-symbol, long-only,
all-in / all-out (matches the botnet101 reference-CSV convention).

Metrics computed per strategy x symbol from per-trade round-trip returns:
  n_trades, win_rate, mean_ret_pct (expectancy), profit_factor, payoff_ratio,
  sharpe (per-trade ann.), and from the equity curve: total_return, cagr,
  max_dd, plus buy&hold CAGR for the same span. sharpe_ish = mean/std of
  per-trade returns (matches the live eligibility metric).
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backtest.engine import BacktestEngine, Order  # noqa: E402
from backtest.report import _round_trips  # noqa: E402


class SignalStrategy:
    """Single-symbol long-only adapter. Pre-computes long_entry/long_exit via
    compute_fn, then on each bar: if flat and entry fired -> buy all-in;
    if long and exit fired -> sell all. Signals act on the bar they fire
    (engine fills at next bar open)."""

    def __init__(self, symbol: str, signals: pd.DataFrame, cash_frac: float = 0.99):
        self.symbol = symbol
        self.entry = signals["long_entry"].astype(bool)
        self.exit = signals["long_exit"].astype(bool)
        self.cash_frac = cash_frac

    def on_bar(self, ts, bars, portfolio):
        bar = bars.get(self.symbol)
        if bar is None or ts not in self.entry.index:
            return []
        held = portfolio.qty(self.symbol)
        if held <= 0:
            if bool(self.entry.loc[ts]):
                qty = int(portfolio.cash * self.cash_frac / bar.close)
                if qty > 0:
                    return [Order(self.symbol, qty, "buy", "market")]
        else:
            if bool(self.exit.loc[ts]):
                return [Order(self.symbol, held, "sell", "market")]
        return []


@dataclass
class Metrics:
    strategy: str
    symbol: str
    n_trades: int
    win_rate_pct: float
    mean_ret_pct: float       # expectancy per trade (% of entry notional)
    expectancy_pct: float     # alias, same as mean_ret_pct
    profit_factor: float
    payoff_ratio: float
    sharpe_ish: float         # per-trade mean/std (live eligibility metric)
    sharpe_ann: float         # daily-equity Sharpe *252
    max_dd_pct: float
    total_return_pct: float
    cagr_pct: float
    bh_cagr_pct: float        # buy & hold over same span
    n_bars: int


def _trade_returns_pct(fills) -> List[float]:
    """Per-round-trip return as % of entry cost (FIFO), long-only."""
    open_lots: Dict[str, list] = {}
    rets: List[float] = []
    for f in fills:
        lots = open_lots.setdefault(f.symbol, [])
        if f.side == "buy":
            lots.append([f.qty, f.price])
        else:
            remaining = f.qty
            while remaining > 1e-9 and lots:
                lot_qty, lot_price = lots[0]
                used = min(lot_qty, remaining)
                rets.append((f.price - lot_price) / lot_price * 100.0)
                lot_qty -= used
                remaining -= used
                if lot_qty < 1e-9:
                    lots.pop(0)
                else:
                    lots[0] = [lot_qty, lot_price]
    return rets


def _equity_metrics(portfolio, periods=252):
    eq = pd.Series([e for _, e in portfolio.equity_curve])
    initial, final = float(eq.iloc[0]), float(eq.iloc[-1])
    total_ret = final / initial - 1.0
    rets = eq.pct_change().dropna()
    if len(rets) > 1 and rets.std() > 0:
        sharpe_ann = float(rets.mean() / rets.std() * math.sqrt(periods))
    else:
        sharpe_ann = 0.0
    running_max = eq.cummax()
    dd = (eq / running_max - 1.0).min()
    n = len(eq)
    years = n / periods if periods else 0
    cagr = ((final / initial) ** (1 / years) - 1.0) if years > 0 and final > 0 else 0.0
    return total_ret * 100, cagr * 100, float(dd) * 100, sharpe_ann, n


def _bh_cagr(df: pd.DataFrame, periods=252) -> float:
    first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
    years = len(df) / periods
    if years <= 0 or first <= 0:
        return 0.0
    return ((last / first) ** (1 / years) - 1.0) * 100.0


def backtest_one(strategy_name: str, compute_fn: Callable, symbol: str,
                 df: pd.DataFrame, initial_cash: float = 100_000.0) -> Metrics:
    sig = compute_fn(df.copy())
    strat = SignalStrategy(symbol, sig)
    engine = BacktestEngine({symbol: df}, strat, initial_cash=initial_cash,
                            slippage_bps=5.0)
    pf = engine.run()

    trade_rets = _trade_returns_pct(pf.fills)
    n = len(trade_rets)
    arr = np.array(trade_rets) if n else np.array([0.0])
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    mean_ret = float(arr.mean()) if n else 0.0
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    pf_ratio = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else (float("inf") if avg_win > 0 else 0.0)
    sharpe_ish = float(arr.mean() / arr.std()) if n > 1 and arr.std() > 0 else 0.0

    total_ret, cagr, max_dd, sharpe_ann, n_bars = _equity_metrics(pf)
    bh = _bh_cagr(df)

    return Metrics(
        strategy=strategy_name, symbol=symbol, n_trades=n,
        win_rate_pct=round(win_rate, 1), mean_ret_pct=round(mean_ret, 3),
        expectancy_pct=round(mean_ret, 3),
        profit_factor=round(pf_ratio, 2) if pf_ratio != float("inf") else 999.0,
        payoff_ratio=round(payoff, 2) if payoff != float("inf") else 999.0,
        sharpe_ish=round(sharpe_ish, 3), sharpe_ann=round(sharpe_ann, 2),
        max_dd_pct=round(max_dd, 2), total_return_pct=round(total_ret, 2),
        cagr_pct=round(cagr, 2), bh_cagr_pct=round(bh, 2), n_bars=n_bars,
    )


def aggregate(metrics_rows: List[Metrics], strategy_name: str,
              data: Dict[str, pd.DataFrame], compute_fn: Callable) -> Metrics:
    """Pooled metrics across all symbols for a strategy: concatenate every
    per-trade return into one population (equal-weight by trade)."""
    all_rets: List[float] = []
    for sym, df in data.items():
        sig = compute_fn(df.copy())
        strat = SignalStrategy(sym, sig)
        engine = BacktestEngine({sym: df}, strat, initial_cash=100_000.0, slippage_bps=5.0)
        pf = engine.run()
        all_rets.extend(_trade_returns_pct(pf.fills))

    n = len(all_rets)
    arr = np.array(all_rets) if n else np.array([0.0])
    wins = arr[arr > 0]; losses = arr[arr < 0]
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    mean_ret = float(arr.mean()) if n else 0.0
    gross_win = float(wins.sum()); gross_loss = float(-losses.sum())
    pf_ratio = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else (999.0 if avg_win > 0 else 0.0)
    sharpe_ish = float(arr.mean() / arr.std()) if n > 1 and arr.std() > 0 else 0.0

    return Metrics(
        strategy=strategy_name, symbol="ALL", n_trades=n,
        win_rate_pct=round(win_rate, 1), mean_ret_pct=round(mean_ret, 3),
        expectancy_pct=round(mean_ret, 3), profit_factor=round(pf_ratio, 2),
        payoff_ratio=round(payoff, 2), sharpe_ish=round(sharpe_ish, 3),
        sharpe_ann=0.0, max_dd_pct=0.0, total_return_pct=0.0, cagr_pct=0.0,
        bh_cagr_pct=0.0, n_bars=0,
    )


def metrics_to_df(rows: List[Metrics]) -> pd.DataFrame:
    return pd.DataFrame([asdict(r) for r in rows])
