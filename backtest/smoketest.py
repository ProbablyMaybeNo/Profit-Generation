"""
smoketest.py — End-to-end backtest of buy-and-hold SPY.
Validates engine + portfolio + report + data loader.

Run:  python -m backtest.smoketest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest import BacktestEngine, Order, load_bars, summarize


class BuyAndHoldSPY:
    """Buy SPY on day 1 with all available cash, never sell."""

    def __init__(self):
        self.bought = False

    def on_bar(self, ts, bars, portfolio):
        if self.bought or "SPY" not in bars:
            return []
        bar = bars["SPY"]
        qty = int(portfolio.cash * 0.99 / bar.close)
        if qty <= 0:
            return []
        self.bought = True
        return [Order(symbol="SPY", qty=qty, side="buy", type="market")]


def main():
    print("Loading SPY 2022-01-01 -> 2025-12-31 ...")
    data = load_bars(["SPY"], start="2022-01-01", end="2025-12-31")
    if "SPY" not in data:
        print("FAIL: no SPY data returned")
        sys.exit(1)
    print(f"Loaded {len(data['SPY'])} bars")

    engine = BacktestEngine(
        data=data,
        strategy=BuyAndHoldSPY(),
        initial_cash=100_000.0,
        slippage_bps=5.0,
    )
    portfolio = engine.run()
    report = summarize(portfolio)

    print("\n=== Buy-and-Hold SPY ===")
    print(report)
    print(f"\nFinal positions: {portfolio.positions}")
    print(f"Cash remaining:  ${portfolio.cash:,.2f}")

    assert report.num_fills == 1, f"expected 1 fill, got {report.num_fills}"
    assert report.bars == len(data["SPY"]), "equity curve length mismatch"
    assert portfolio.equity_curve, "equity curve empty"
    print("\nSMOKE TEST PASSED")


if __name__ == "__main__":
    main()
