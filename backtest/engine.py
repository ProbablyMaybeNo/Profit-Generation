"""
engine.py — Bar-by-bar backtest event loop.

Order semantics:
  - Orders submitted on bar t fill at bar t+1 open price (next-bar-open execution).
  - Market orders always fill (with slippage in bps).
  - Limit buys fill if next bar's low <= limit_price; fill price = min(open, limit_price).
  - Limit sells fill if next bar's high >= limit_price; fill price = max(open, limit_price).
  - Unfilled limit orders are dropped at end of next bar (no GTC).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Protocol

import pandas as pd

from backtest.portfolio import Fill, Portfolio


@dataclass
class Order:
    symbol: str
    qty: float
    side: str
    type: str = "market"
    limit_price: Optional[float] = None


@dataclass
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Strategy(Protocol):
    def on_bar(
        self,
        timestamp: datetime,
        bars: Dict[str, Bar],
        portfolio: Portfolio,
    ) -> List[Order]: ...


class BacktestEngine:
    def __init__(
        self,
        data: Dict[str, pd.DataFrame],
        strategy: Strategy,
        initial_cash: float = 100_000.0,
        slippage_bps: float = 5.0,
        commission_per_share: float = 0.0,
    ):
        if not data:
            raise ValueError("data must contain at least one symbol")
        self.data = {s: df.sort_index() for s, df in data.items()}
        self.strategy = strategy
        self.portfolio = Portfolio(cash=initial_cash)
        self.slippage = slippage_bps / 10_000.0
        self.commission_per_share = commission_per_share
        self.pending: List[Order] = []

        timestamps: set = set()
        for df in self.data.values():
            timestamps.update(df.index)
        self.timeline: List[datetime] = sorted(timestamps)

    def _bars_at(self, ts: datetime) -> Dict[str, Bar]:
        out: Dict[str, Bar] = {}
        for sym, df in self.data.items():
            if ts in df.index:
                row = df.loc[ts]
                out[sym] = Bar(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0.0)),
                )
        return out

    def _try_fill(self, order: Order, bar: Bar) -> Optional[Fill]:
        if order.type == "market":
            adj = 1.0 + self.slippage if order.side == "buy" else 1.0 - self.slippage
            price = bar.open * adj
        elif order.type == "limit":
            if order.limit_price is None:
                return None
            if order.side == "buy":
                if bar.low > order.limit_price:
                    return None
                price = min(bar.open, order.limit_price)
            else:
                if bar.high < order.limit_price:
                    return None
                price = max(bar.open, order.limit_price)
        else:
            return None

        return Fill(
            timestamp=bar.timestamp,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=price,
            commission=order.qty * self.commission_per_share,
        )

    def run(self) -> Portfolio:
        for ts in self.timeline:
            bars = self._bars_at(ts)

            for order in self.pending:
                bar = bars.get(order.symbol)
                if bar is None:
                    continue
                fill = self._try_fill(order, bar)
                if fill is not None:
                    self.portfolio.apply_fill(fill)
            self.pending = []

            new_orders = self.strategy.on_bar(ts, bars, self.portfolio)
            self.pending = list(new_orders or [])

            close_prices = {s: b.close for s, b in bars.items()}
            self.portfolio.mark(ts, close_prices)

        return self.portfolio
