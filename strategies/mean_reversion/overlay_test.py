"""
overlay_test.py — Test 3-Bar Low as a defensive overlay on top of B&H.

Question: does combining a static B&H position with a mean-reversion overlay
(3-Bar Low rule, in cash when not in signal) produce a portfolio with lower
drawdown than pure B&H, while preserving most of the return?

Three portfolios compared per symbol:
  A) 100% B&H
  B) 50/50 B&H + 3-Bar Low overlay
  C) 70/30 B&H + 3-Bar Low overlay (favorable for B&H bias)
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import math
import numpy as np
import pandas as pd

from backtest import BacktestEngine, Order, load_bars, summarize
from strategies.mean_reversion.botnet101 import SignalStrategy, compute_3bar_low


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


def run_eq_curve(symbol, df, strategy, initial_cash, slippage_bps=2.0) -> pd.Series:
    engine = BacktestEngine(
        data={symbol: df}, strategy=strategy,
        initial_cash=initial_cash, slippage_bps=slippage_bps,
    )
    portfolio = engine.run()
    if not portfolio.equity_curve:
        return pd.Series(dtype=float)
    s = pd.Series(
        [eq for _, eq in portfolio.equity_curve],
        index=[ts for ts, _ in portfolio.equity_curve],
    )
    return s


def metrics(eq: pd.Series, periods_per_year: int = 252) -> dict:
    rets = eq.pct_change().dropna()
    if len(rets) < 2:
        return {"total_return_pct": 0.0, "cagr_pct": 0.0, "sharpe": 0.0, "max_dd_pct": 0.0}
    initial = float(eq.iloc[0])
    final = float(eq.iloc[-1])
    total = final / initial - 1.0
    years = len(eq) / periods_per_year
    cagr = ((final / initial) ** (1 / years) - 1.0) if years > 0 else 0.0
    sharpe = rets.mean() / rets.std() * math.sqrt(periods_per_year) if rets.std() > 0 else 0.0
    peaks = eq.cummax()
    max_dd = ((eq - peaks) / peaks).min()
    return {
        "total_return_pct": total * 100,
        "cagr_pct": cagr * 100,
        "sharpe": sharpe,
        "max_dd_pct": max_dd * 100,
    }


def combine_curves(curve_a: pd.Series, weight_a: float,
                   curve_b: pd.Series, weight_b: float,
                   initial_total: float) -> pd.Series:
    """
    Combine two normalized return paths into a single portfolio equity curve.
    Each leg starts with weight_a*initial_total / weight_b*initial_total of cash.
    The combined curve is the sum of the two legs at each timestamp.
    """
    if curve_a.empty or curve_b.empty:
        return pd.Series(dtype=float)
    common = curve_a.index.intersection(curve_b.index)
    return curve_a.loc[common] + curve_b.loc[common]


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2010-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"
    symbols = sys.argv[3].split(",") if len(sys.argv) > 3 else ["SPY", "QQQ"]

    print(f"=== 3-Bar Low Defensive Overlay Test ===")
    print(f"Window: {start} -> {end}")
    print(f"Symbols: {symbols}\n")

    rows = []
    for symbol in symbols:
        print(f"Loading {symbol} daily ...")
        data = load_bars([symbol], start=start, end=end, interval="1d", source="yf")
        if symbol not in data:
            continue
        df = data[symbol]

        bh_curve = run_eq_curve(symbol, df, BuyAndHold(symbol), 10_000.0)
        signals = compute_3bar_low(df)
        mr_curve = run_eq_curve(symbol, df, SignalStrategy(symbol, signals, "3-bar-low"), 10_000.0)

        if bh_curve.empty or mr_curve.empty:
            print(f"  FAIL: empty curves")
            continue

        common = bh_curve.index.intersection(mr_curve.index)
        bh_norm = bh_curve.loc[common] / bh_curve.iloc[0]
        mr_norm = mr_curve.loc[common] / mr_curve.iloc[0]

        scenarios = [
            ("100% B&H",                  1.0, 0.0),
            ("100% 3-Bar Low",            0.0, 1.0),
            ("50/50 B&H + 3-Bar Low",     0.5, 0.5),
            ("70/30 B&H + 3-Bar Low",     0.7, 0.3),
            ("30/70 B&H + 3-Bar Low",     0.3, 0.7),
        ]
        for label, w_bh, w_mr in scenarios:
            combined = (bh_norm * w_bh + mr_norm * w_mr) * 10_000.0
            m = metrics(combined)
            rows.append({
                "symbol": symbol, "scenario": label,
                **{k: round(v, 2) for k, v in m.items()},
            })

    if not rows:
        print("FAIL: no results")
        return

    results = pd.DataFrame(rows)
    print()
    for symbol in symbols:
        sub = results[results["symbol"] == symbol]
        if sub.empty:
            continue
        print(f"\n=== {symbol} ===")
        print(sub[["scenario", "total_return_pct", "cagr_pct", "sharpe", "max_dd_pct"]].to_string(index=False))

        bh = sub[sub["scenario"] == "100% B&H"].iloc[0]
        for _, row in sub.iterrows():
            if row["scenario"] == "100% B&H":
                continue
            sharpe_delta = row["sharpe"] - bh["sharpe"]
            dd_delta = row["max_dd_pct"] - bh["max_dd_pct"]
            cagr_delta = row["cagr_pct"] - bh["cagr_pct"]
            print(f"  {row['scenario']:<28} dCAGR {cagr_delta:+.2f}%  "
                  f"dSharpe {sharpe_delta:+.2f}  dMaxDD {dd_delta:+.2f}%")

    out_path = ROOT / "data" / "overlay_test_results.csv"
    results.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
