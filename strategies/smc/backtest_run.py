"""
backtest_run.py — Run TJRStrategy on SPY 5m bars and compare to buy-and-hold
over the same period.

Usage:
  python -m strategies.smc.backtest_run [start] [end]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from collections import Counter

import pandas as pd

from backtest import BacktestEngine, Order, load_bars, summarize
from backtest.portfolio import Portfolio
from strategies.smc.strategy import TJRConfig, TJRStrategy


class BuyAndHold:
    def __init__(self, symbol):
        self.symbol = symbol
        self.bought = False

    def on_bar(self, ts, bars, portfolio):
        if self.bought or self.symbol not in bars:
            return []
        bar = bars[self.symbol]
        qty = int(portfolio.cash * 0.99 / bar.close)
        if qty <= 0:
            return []
        self.bought = True
        return [Order(symbol=self.symbol, qty=qty, side="buy", type="market")]


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2025-01-01"
    symbol = "SPY"

    print(f"Loading {symbol} 5m bars from {start} to {end} (Alpaca IEX) ...")
    data = load_bars([symbol], start=start, end=end, interval="5m")
    if symbol not in data:
        print(f"FAIL: no {symbol} data returned")
        sys.exit(1)
    df = data[symbol]
    print(f"Loaded {len(df):,} 5m bars  ({df.index[0]} -> {df.index[-1]})")

    print("\n=== TJR Strategy ===")
    cfg = TJRConfig()
    print(f"config: {cfg}")
    strat = TJRStrategy(symbol=symbol, ltf_df=df, config=cfg)

    print(f"precomputed: {len(strat.htf_swings)} HTF swings  "
          f"{len(strat.htf_sweeps)} HTF sweeps  "
          f"{len(strat.ltf_bos)} LTF BOS  "
          f"{len(strat.ltf_fvgs)} FVGs  {len(strat.ltf_obs)} OBs")

    engine = BacktestEngine(
        data={symbol: df},
        strategy=strat,
        initial_cash=100_000.0,
        slippage_bps=5.0,
    )
    portfolio = engine.run()
    report = summarize(portfolio, periods_per_year=252 * 78)

    print(report)

    if strat.trade_log:
        reasons = Counter(t["exit_reason"] for t in strat.trade_log)
        print(f"\nExit reasons: {dict(reasons)}")
        wins = sum(1 for t in strat.trade_log if t["exit_reason"] == "target")
        losses = sum(1 for t in strat.trade_log if t["exit_reason"] == "stop")
        time_outs = sum(1 for t in strat.trade_log if t["exit_reason"] == "time")
        n = len(strat.trade_log)
        print(f"Closed trades: {n}")
        print(f"  Target hits:  {wins}  ({wins/n*100:.1f}%)")
        print(f"  Stop outs:    {losses}  ({losses/n*100:.1f}%)")
        print(f"  Time exits:   {time_outs}  ({time_outs/n*100:.1f}%)")

    print("\n=== Buy & Hold (same period, same data) ===")
    bh_engine = BacktestEngine(
        data={symbol: df},
        strategy=BuyAndHold(symbol),
        initial_cash=100_000.0,
        slippage_bps=5.0,
    )
    bh_portfolio = bh_engine.run()
    bh_report = summarize(bh_portfolio, periods_per_year=252 * 78)
    print(bh_report)

    print("\n=== Verdict ===")
    print(f"Strategy CAGR: {report.cagr_pct:+.2f}%   B&H CAGR: {bh_report.cagr_pct:+.2f}%")
    print(f"Strategy Sharpe: {report.sharpe:.2f}   B&H Sharpe: {bh_report.sharpe:.2f}")
    print(f"Strategy MaxDD: {report.max_drawdown_pct:.2f}%   B&H MaxDD: {bh_report.max_drawdown_pct:.2f}%")

    passed = (
        report.sharpe > bh_report.sharpe
        and report.max_drawdown_pct > bh_report.max_drawdown_pct  # less negative
        and report.num_round_trips >= 30
    )
    print(f"\nGATE (Sharpe > B&H, MDD better, >=30 trades): "
          f"{'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    main()
