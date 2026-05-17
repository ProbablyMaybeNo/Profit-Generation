"""
portfolio.py — Tracks cash, positions, and the equity curve during a backtest.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Tuple


@dataclass
class Fill:
    timestamp: datetime
    symbol: str
    side: str
    qty: float
    price: float
    commission: float


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, float] = field(default_factory=dict)
    avg_cost: Dict[str, float] = field(default_factory=dict)
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)
    fills: List[Fill] = field(default_factory=list)

    def qty(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def equity(self, prices: Dict[str, float]) -> float:
        mkt = sum(q * prices.get(sym, self.avg_cost.get(sym, 0.0))
                  for sym, q in self.positions.items())
        return self.cash + mkt

    def apply_fill(self, fill: Fill) -> None:
        sym = fill.symbol
        prev_qty = self.positions.get(sym, 0.0)

        # PG-013 (3.5.1): backtest portfolio is long-only. A sell larger
        # than the current position would imply implicit shorts; cap the
        # sell at prev_qty so simulation matches live risk constraints.
        if fill.side != "buy" and fill.qty > prev_qty:
            if prev_qty <= 0:
                # Nothing to sell — drop the fill silently. Capturing it
                # in self.fills would make P&L reports lie.
                return
            fill = Fill(
                timestamp=fill.timestamp, symbol=sym, side=fill.side,
                qty=prev_qty, price=fill.price, commission=fill.commission,
            )

        signed_qty = fill.qty if fill.side == "buy" else -fill.qty
        new_qty = prev_qty + signed_qty

        if fill.side == "buy":
            cost = fill.qty * fill.price + fill.commission
            self.cash -= cost
            if prev_qty <= 0 and new_qty > 0:
                self.avg_cost[sym] = fill.price
            elif new_qty > 0:
                total_cost = prev_qty * self.avg_cost.get(sym, fill.price) + fill.qty * fill.price
                self.avg_cost[sym] = total_cost / new_qty
        else:
            proceeds = fill.qty * fill.price - fill.commission
            self.cash += proceeds

        if abs(new_qty) < 1e-9:
            self.positions.pop(sym, None)
            self.avg_cost.pop(sym, None)
        else:
            self.positions[sym] = new_qty

        self.fills.append(fill)

    def mark(self, timestamp: datetime, prices: Dict[str, float]) -> None:
        self.equity_curve.append((timestamp, self.equity(prices)))
