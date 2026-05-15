"""
runner.py — Run both ORB strategies on intraday SPY/QQQ data.

Usage:
  python -m strategies.orb.runner [start] [end] [symbol]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from collections import Counter

import pandas as pd

from backtest import BacktestEngine, Order, load_bars, summarize
from backtest.data import resample_bars
from strategies.orb.orbo import ORBOStrategy, ORBOConfig
from strategies.orb.orb_pivots import ORBPivotsStrategy, ORBPivotsConfig


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


def run_one(label, symbol, df, strategy, slippage_bps=5.0):
    engine = BacktestEngine(
        data={symbol: df}, strategy=strategy,
        initial_cash=10_000.0, slippage_bps=slippage_bps,
    )
    portfolio = engine.run()
    if not portfolio.equity_curve:
        return None
    rep = summarize(portfolio, periods_per_year=252 * 78)
    return {
        "label": label, "symbol": symbol,
        "trades": rep.num_round_trips,
        "fills": rep.num_fills,
        "total_return_pct": round(rep.total_return_pct, 2),
        "cagr_pct": round(rep.cagr_pct, 2),
        "sharpe": round(rep.sharpe, 2),
        "max_dd_pct": round(rep.max_drawdown_pct, 2),
        "win_rate_pct": round(rep.win_rate_pct, 1),
    }


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2025-01-01"
    symbol = sys.argv[3] if len(sys.argv) > 3 else "SPY"
    slippage_bps = 5.0

    print(f"=== ORB family backtest ===")
    print(f"Window: {start} -> {end}")
    print(f"Symbol: {symbol}")
    print(f"Slippage: {slippage_bps} bps per fill\n")

    print(f"Loading {symbol} 5m bars (Alpaca IEX) ...")
    data = load_bars([symbol], start=start, end=end, interval="5m")
    if symbol not in data:
        print("FAIL: no intraday data")
        return
    intraday = data[symbol]
    print(f"  {len(intraday)} 5m bars")

    daily = resample_bars(intraday, "1D")
    print(f"  {len(daily)} daily bars (resampled)")
    print()

    rows = []

    bh = run_one("BUY_AND_HOLD", symbol, intraday, BuyAndHold(symbol), slippage_bps)
    if bh:
        rows.append(bh)

    orbo = ORBOStrategy(symbol, ORBOConfig(long_only=False))
    rows.append(run_one("orbo-bidirectional", symbol, intraday, orbo, slippage_bps))

    orbo_lo = ORBOStrategy(symbol, ORBOConfig(long_only=True))
    rows.append(run_one("orbo-long-only", symbol, intraday, orbo_lo, slippage_bps))

    orb_pivots = ORBPivotsStrategy(symbol, daily, ORBPivotsConfig())
    rows.append(run_one("orb-pivots-long-only", symbol, intraday, orb_pivots, slippage_bps))

    df_results = pd.DataFrame([r for r in rows if r])
    print(df_results.to_string(index=False))

    out_path = ROOT / "data" / "orb_family_results.csv"
    df_results.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")

    bh_row = df_results[df_results["label"] == "BUY_AND_HOLD"]
    if bh_row.empty:
        return
    bh = bh_row.iloc[0]
    print(f"\nBaseline: SPY B&H Sharpe {bh['sharpe']:.2f}, CAGR {bh['cagr_pct']:.2f}%, MaxDD {bh['max_dd_pct']:.2f}%")
    print()
    for _, row in df_results.iterrows():
        if row["label"] == "BUY_AND_HOLD":
            continue
        beats = row["sharpe"] >= bh["sharpe"] and row["max_dd_pct"] >= bh["max_dd_pct"]
        verdict = "PASS" if beats and row["trades"] >= 20 else ("FAIL" if row["trades"] >= 20 else "INSUFFICIENT_TRADES")
        print(f"  {row['label']:<25} -> {verdict}  (trades={row['trades']}, "
              f"Sharpe={row['sharpe']:.2f}, CAGR={row['cagr_pct']:.2f}%, MaxDD={row['max_dd_pct']:.2f}%)")


if __name__ == "__main__":
    main()
