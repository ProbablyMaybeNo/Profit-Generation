"""
report.py — Backtest performance metrics.
Sharpe, MaxDD, total return, CAGR, num trades, win rate.
"""

from dataclasses import dataclass
import math
from typing import List

import pandas as pd

from backtest.portfolio import Fill, Portfolio


@dataclass
class Report:
    initial_equity: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float
    num_fills: int
    num_round_trips: int
    win_rate_pct: float
    bars: int

    def __str__(self) -> str:
        return (
            f"Equity:        ${self.initial_equity:,.2f} -> ${self.final_equity:,.2f}\n"
            f"Total return:  {self.total_return_pct:+.2f}%\n"
            f"CAGR:          {self.cagr_pct:+.2f}%\n"
            f"Sharpe (252):  {self.sharpe:.2f}\n"
            f"Max drawdown:  {self.max_drawdown_pct:.2f}%\n"
            f"Fills:         {self.num_fills}\n"
            f"Round-trips:   {self.num_round_trips} (win rate {self.win_rate_pct:.1f}%)\n"
            f"Bars:          {self.bars}"
        )


def _round_trips(fills: List[Fill]) -> List[float]:
    open_lots: dict[str, list[tuple[float, float]]] = {}
    pnl: list[float] = []
    for f in fills:
        lots = open_lots.setdefault(f.symbol, [])
        if f.side == "buy":
            lots.append((f.qty, f.price))
        else:
            remaining = f.qty
            while remaining > 1e-9 and lots:
                lot_qty, lot_price = lots[0]
                used = min(lot_qty, remaining)
                pnl.append(used * (f.price - lot_price) - f.commission)
                lot_qty -= used
                remaining -= used
                if lot_qty < 1e-9:
                    lots.pop(0)
                else:
                    lots[0] = (lot_qty, lot_price)
    return pnl


def summarize(portfolio: Portfolio, periods_per_year: int = 252) -> Report:
    if not portfolio.equity_curve:
        raise ValueError("portfolio.equity_curve is empty — did the backtest run?")

    df = pd.DataFrame(portfolio.equity_curve, columns=["ts", "equity"]).set_index("ts")
    eq = df["equity"]
    initial = float(eq.iloc[0])
    final = float(eq.iloc[-1])
    total_return = final / initial - 1.0

    rets = eq.pct_change().dropna()
    if len(rets) > 1 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * math.sqrt(periods_per_year))
    else:
        sharpe = 0.0

    running_max = eq.cummax()
    drawdown = eq / running_max - 1.0
    max_dd = float(drawdown.min())

    n_bars = len(eq)
    years = n_bars / periods_per_year if periods_per_year else 0
    cagr = ((final / initial) ** (1 / years) - 1.0) if years > 0 else 0.0

    pnls = _round_trips(portfolio.fills)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = (wins / len(pnls) * 100.0) if pnls else 0.0

    return Report(
        initial_equity=initial,
        final_equity=final,
        total_return_pct=total_return * 100.0,
        cagr_pct=cagr * 100.0,
        sharpe=sharpe,
        max_drawdown_pct=max_dd * 100.0,
        num_fills=len(portfolio.fills),
        num_round_trips=len(pnls),
        win_rate_pct=win_rate,
        bars=n_bars,
    )
