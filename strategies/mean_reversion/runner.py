"""
runner.py — Run the Botnet101 mean-reversion cluster on a configurable
universe and date range. Compares to buy-and-hold of each symbol.

Usage:
  python -m strategies.mean_reversion.runner [start] [end] [symbols_csv]

Defaults:
  start=2010-01-01  end=2024-12-31  symbols=SPY,QQQ,IWM
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import math
import pandas as pd

from backtest import BacktestEngine, Order, load_bars, summarize
from strategies.mean_reversion.botnet101 import (
    SignalStrategy,
    compute_5day_low,
    compute_3bar_low,
    compute_bb_ibs,
    compute_avg_hl_range_ibs,
    compute_turn_of_month,
    compute_consecutive_below_ema,
    compute_turn_around_tuesday,
    compute_consecutive_bearish,
    compute_4bar_momentum_reversal,
)


STRATEGIES = [
    ("buy-5day-low",                 "Buy on 5-day Low",            compute_5day_low),
    ("3-bar-low",                    "3-Bar Low",                   compute_3bar_low),
    ("3-bar-low-200ema",             "3-Bar Low + 200 EMA filter",  lambda df: compute_3bar_low(df, use_ema_filter=True)),
    ("bb-reversal-ibs",              "BB Reversal + IBS",           compute_bb_ibs),
    ("avg-hl-range-ibs",             "Avg HL Range + IBS",          compute_avg_hl_range_ibs),
    ("turn-of-month-25",             "Turn of the Month (>=25)",    compute_turn_of_month),
    ("consec-below-sma5",            "3 bars below SMA(5)",         compute_consecutive_below_ema),
    ("turn-around-tuesday",          "Turn-around Tuesday",         compute_turn_around_tuesday),
    ("turn-around-tuesday-200sma",   "Turn-around Tue + 200 SMA",   lambda df: compute_turn_around_tuesday(df, use_ma_filter=True)),
    ("consec-bearish-3",             "3 consecutive bearish",       compute_consecutive_bearish),
    ("4bar-momentum-reversal",       "4-Bar Momentum Reversal",     compute_4bar_momentum_reversal),
]


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


def run_one(symbol, df, label, name, compute_fn, slippage_bps):
    signals = compute_fn(df)
    n_signals = int(signals["long_entry"].sum())
    if n_signals == 0:
        return {
            "symbol": symbol, "strategy": label, "name": name,
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
            "symbol": symbol, "strategy": label, "name": name,
            "n_signals": n_signals, "fills": 0, "trades": 0,
            "total_return_pct": 0.0, "cagr_pct": 0.0,
            "sharpe": 0.0, "max_dd_pct": 0.0, "win_rate_pct": 0.0,
        }
    rep = summarize(portfolio, periods_per_year=252)
    return {
        "symbol": symbol, "strategy": label, "name": name,
        "n_signals": n_signals,
        "fills": rep.num_fills, "trades": rep.num_round_trips,
        "total_return_pct": round(rep.total_return_pct, 2),
        "cagr_pct": round(rep.cagr_pct, 2),
        "sharpe": round(rep.sharpe, 2),
        "max_dd_pct": round(rep.max_drawdown_pct, 2),
        "win_rate_pct": round(rep.win_rate_pct, 1),
    }


def run_buy_and_hold(symbol, df, slippage_bps):
    engine = BacktestEngine(
        data={symbol: df}, strategy=BuyAndHold(symbol),
        initial_cash=10_000.0, slippage_bps=slippage_bps,
    )
    portfolio = engine.run()
    rep = summarize(portfolio, periods_per_year=252)
    return {
        "symbol": symbol, "strategy": "BUY_AND_HOLD", "name": "Buy & Hold",
        "n_signals": 1,
        "fills": rep.num_fills, "trades": 0,
        "total_return_pct": round(rep.total_return_pct, 2),
        "cagr_pct": round(rep.cagr_pct, 2),
        "sharpe": round(rep.sharpe, 2),
        "max_dd_pct": round(rep.max_drawdown_pct, 2),
        "win_rate_pct": 0.0,
    }


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2010-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
    symbols = sys.argv[3].split(",") if len(sys.argv) > 3 else ["SPY", "QQQ", "IWM"]
    slippage_bps = 2.0

    print(f"=== Botnet101 mean-reversion cluster ===")
    print(f"Window: {start} -> {end}")
    print(f"Symbols: {symbols}")
    print(f"Slippage: {slippage_bps} bps per fill")
    print()

    rows = []
    for symbol in symbols:
        print(f"Loading {symbol} daily bars (yfinance) ...")
        data = load_bars([symbol], start=start, end=end, interval="1d", source="yf")
        if symbol not in data:
            print(f"  FAIL: no data for {symbol}")
            continue
        df = data[symbol]
        print(f"  {len(df)} bars  ({df.index[0].date()} -> {df.index[-1].date()})")

        rows.append(run_buy_and_hold(symbol, df, slippage_bps))
        for label, name, fn in STRATEGIES:
            rows.append(run_one(symbol, df, label, name, fn, slippage_bps))

    if not rows:
        print("FAIL: no results")
        return

    results = pd.DataFrame(rows)
    out_path = ROOT / "data" / "botnet101_mean_reversion_results.csv"
    results.to_csv(out_path, index=False)

    print()
    for symbol in symbols:
        sub = results[results["symbol"] == symbol].copy()
        if sub.empty:
            continue
        print(f"\n=== {symbol} ===")
        print(sub[[
            "strategy", "trades", "total_return_pct", "cagr_pct",
            "sharpe", "max_dd_pct", "win_rate_pct"
        ]].to_string(index=False))

        bh = sub[sub["strategy"] == "BUY_AND_HOLD"].iloc[0]
        passers = []
        for _, row in sub.iterrows():
            if row["strategy"] == "BUY_AND_HOLD":
                continue
            if (row["sharpe"] >= bh["sharpe"]
                    and row["max_dd_pct"] >= bh["max_dd_pct"]
                    and row["trades"] >= 20):
                passers.append(row["strategy"])
        if passers:
            print(f"  Beat B&H on Sharpe and MaxDD with >=20 trades: {passers}")
        else:
            print(f"  None beat B&H on the joint Sharpe + MaxDD gate.")

    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
