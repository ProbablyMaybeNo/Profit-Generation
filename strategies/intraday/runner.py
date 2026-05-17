"""runner.py — Run the intraday mean-reversion cluster on a configurable
universe and intraday bar interval. Compares to buy-and-hold of each symbol.

Usage:
  py -3.13 -m strategies.intraday.runner [start] [end] [symbols_csv] [interval]

Defaults:
  start=2026-04-01  end=today  symbols=SPY,QQQ,IWM  interval=5m

Notes:
  - Intraday bars come from Alpaca (free IEX feed). yfinance has thin
    minute history; this runner deliberately uses source="alpaca".
  - Buy-and-hold here means a single open at the first bar of the window
    held to the last bar — purely a benchmark for the strategy's risk-
    adjusted P&L, not an investing recommendation.
"""

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from backtest import BacktestEngine, Order, load_bars, summarize  # noqa: E402
from strategies.intraday.mean_reversion_intraday import (  # noqa: E402
    INTRADAY_STRATEGIES,
)
from strategies.mean_reversion.botnet101 import SignalStrategy  # noqa: E402


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


def run_one(symbol, df, label, compute_fn, slippage_bps, periods_per_year):
    signals = compute_fn(df)
    n_signals = int(signals["long_entry"].sum())
    if n_signals == 0:
        return {
            "symbol": symbol, "strategy": label,
            "n_signals": 0, "fills": 0, "trades": 0,
            "total_return_pct": 0.0, "cagr_pct": 0.0,
            "sharpe": 0.0, "max_dd_pct": 0.0, "win_rate_pct": 0.0,
        }
    strat = SignalStrategy(symbol, signals, label)
    engine = BacktestEngine(
        data={symbol: df}, strategy=strat,
        initial_cash=10_000.0, slippage_bps=slippage_bps,
    )
    portfolio = engine.run()
    if not portfolio.equity_curve:
        return {
            "symbol": symbol, "strategy": label,
            "n_signals": n_signals, "fills": 0, "trades": 0,
            "total_return_pct": 0.0, "cagr_pct": 0.0,
            "sharpe": 0.0, "max_dd_pct": 0.0, "win_rate_pct": 0.0,
        }
    rep = summarize(portfolio, periods_per_year=periods_per_year)
    return {
        "symbol": symbol, "strategy": label,
        "n_signals": n_signals,
        "fills": rep.num_fills, "trades": rep.num_round_trips,
        "total_return_pct": round(rep.total_return_pct, 2),
        "cagr_pct": round(rep.cagr_pct, 2),
        "sharpe": round(rep.sharpe, 2),
        "max_dd_pct": round(rep.max_drawdown_pct, 2),
        "win_rate_pct": round(rep.win_rate_pct, 1),
    }


# Annualization factor for Sharpe given common intraday bar widths.
# Assumes a 6.5h US equity session and ~252 trading days/yr.
_PERIODS_PER_YEAR = {
    "1m":  252 * 6 * 60,
    "5m":  252 * 6 * 60 // 5,
    "15m": 252 * 6 * 60 // 15,
    "30m": 252 * 6 * 60 // 30,
    "1h":  252 * 6,
    "1d":  252,
}


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-04-01"
    end = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
    symbols = sys.argv[3].split(",") if len(sys.argv) > 3 else ["SPY", "QQQ", "IWM"]
    interval = sys.argv[4] if len(sys.argv) > 4 else "5m"
    slippage_bps = 2.0
    periods_per_year = _PERIODS_PER_YEAR.get(interval, 252)

    print(f"=== intraday mean-reversion cluster — interval={interval} ===")
    print(f"Window:    {start} -> {end}")
    print(f"Symbols:   {symbols}")
    print(f"Slippage:  {slippage_bps} bps per fill")
    print()

    rows = []
    for symbol in symbols:
        print(f"Loading {symbol} {interval} bars (alpaca) ...")
        data = load_bars([symbol], start=start, end=end,
                         interval=interval, source="alpaca")
        if symbol not in data:
            print(f"  FAIL: no data for {symbol}")
            continue
        df = data[symbol]
        print(f"  {len(df)} bars  ({df.index[0]} -> {df.index[-1]})")

        for label, fn in INTRADAY_STRATEGIES:
            rows.append(run_one(symbol, df, label, fn,
                                slippage_bps, periods_per_year))

    if not rows:
        print("FAIL: no results")
        return

    results = pd.DataFrame(rows)
    out_path = ROOT / "data" / f"intraday_mean_reversion_{interval}_results.csv"
    results.to_csv(out_path, index=False)

    print()
    for symbol in symbols:
        sub = results[results["symbol"] == symbol].copy()
        if sub.empty:
            continue
        print(f"\n=== {symbol} ({interval}) ===")
        print(sub[[
            "strategy", "trades", "total_return_pct",
            "sharpe", "max_dd_pct", "win_rate_pct"
        ]].to_string(index=False))

    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
