"""
orbo.py — Session Opening Range Breakout (ORBO).
Source: TradingView strategy by AIScripts (script_key y6cgga73).

Rules:
  - Build OR during a configured session window (default 09:30-09:50 ET).
  - After window closes:
      Long  if close > orHigh
      Short if close < orLow
  - Single position per day. EOD flat at session_end.
  - Stop = opposite side of OR (forces tight risk).
  - Long-only mode supported via `long_only=True` (skip short entries).
"""

from dataclasses import dataclass
from datetime import date, time
from typing import Dict, List, Literal, Optional

from backtest.engine import Bar, Order
from backtest.portfolio import Portfolio


@dataclass
class ORBOConfig:
    or_window_start: time = time(9, 30)
    or_window_end: time = time(9, 50)
    eod_exit: time = time(15, 55)
    long_only: bool = False
    risk_per_trade_pct: float = 0.01


class ORBOStrategy:
    def __init__(self, symbol: str, config: Optional[ORBOConfig] = None):
        self.symbol = symbol
        self.cfg = config or ORBOConfig()
        self.current_date: Optional[date] = None
        self.or_high: Optional[float] = None
        self.or_low: Optional[float] = None
        self.or_complete: bool = False
        self.has_traded_today: bool = False
        self.position_side: Optional[Literal["long", "short"]] = None
        self.entry_price: Optional[float] = None
        self.stop_price: Optional[float] = None
        self.trades_log: List[dict] = []

    def _reset_day(self, d: date):
        self.current_date = d
        self.or_high = None
        self.or_low = None
        self.or_complete = False
        self.has_traded_today = False

    def _close_position(self, ts, price, reason: str) -> List[Order]:
        if self.position_side is None:
            return []
        side: Literal["buy", "sell"] = "sell" if self.position_side == "long" else "buy"
        self.trades_log.append({
            "exit_ts": ts, "exit_price": price, "exit_reason": reason,
            "entry_price": self.entry_price, "side": self.position_side,
        })
        order = Order(symbol=self.symbol, qty=self._open_qty, side=side, type="market")
        self.position_side = None
        self.entry_price = None
        self.stop_price = None
        self._open_qty = 0
        return [order]

    def on_bar(self, ts, bars: Dict[str, Bar], portfolio: Portfolio) -> List[Order]:
        if self.symbol not in bars:
            return []
        bar = bars[self.symbol]
        d = ts.date()
        t = ts.time()

        if self.current_date != d:
            if self.position_side is not None:
                exits = self._close_position(ts, bar.open, "new_day_force_exit")
                self._reset_day(d)
                return exits
            self._reset_day(d)

        if self.cfg.or_window_start <= t < self.cfg.or_window_end:
            self.or_high = bar.high if self.or_high is None else max(self.or_high, bar.high)
            self.or_low = bar.low if self.or_low is None else min(self.or_low, bar.low)
            return []

        if not self.or_complete and t >= self.cfg.or_window_end:
            if self.or_high is not None and self.or_low is not None:
                self.or_complete = True

        if t >= self.cfg.eod_exit and self.position_side is not None:
            return self._close_position(ts, bar.close, "eod")

        if self.position_side is not None:
            if self.position_side == "long" and bar.low <= self.stop_price:
                return self._close_position(ts, self.stop_price, "stop")
            if self.position_side == "short" and bar.high >= self.stop_price:
                return self._close_position(ts, self.stop_price, "stop")
            return []

        if not self.or_complete or self.has_traded_today:
            return []
        if t >= self.cfg.eod_exit:
            return []

        if bar.close > self.or_high:
            risk_per_share = bar.close - self.or_low
            if risk_per_share <= 0:
                return []
            equity = portfolio.equity({self.symbol: bar.close})
            risk_dollars = equity * self.cfg.risk_per_trade_pct
            qty = max(int(risk_dollars / risk_per_share), 0)
            if qty == 0:
                return []
            qty = min(qty, int(portfolio.cash * 0.95 / bar.close))
            if qty == 0:
                return []
            self.position_side = "long"
            self.entry_price = bar.close
            self.stop_price = self.or_low
            self.has_traded_today = True
            self._open_qty = qty
            return [Order(symbol=self.symbol, qty=qty, side="buy", type="market")]

        if not self.cfg.long_only and bar.close < self.or_low:
            risk_per_share = self.or_high - bar.close
            if risk_per_share <= 0:
                return []
            equity = portfolio.equity({self.symbol: bar.close})
            risk_dollars = equity * self.cfg.risk_per_trade_pct
            qty = max(int(risk_dollars / risk_per_share), 0)
            qty = min(qty, int(portfolio.cash * 0.95 / bar.close))
            if qty == 0:
                return []
            self.position_side = "short"
            self.entry_price = bar.close
            self.stop_price = self.or_high
            self.has_traded_today = True
            self._open_qty = qty
            return [Order(symbol=self.symbol, qty=qty, side="sell", type="market")]

        return []
