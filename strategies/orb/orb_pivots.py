"""
orb_pivots.py — Long-only ORB with Pivot Points.
Source: TradingView strategy by VolumeVigilante (script_key 6kde9bla).

Rules:
  - Compute classic floor pivots from previous day's H/L/C.
  - Build OR during opening window (default 09:30-09:45).
  - Long entry when:
      bar.high > orHigh  AND  R1 > orHigh  AND  bar.open < orHigh
  - Initial stop: previous day's low.
  - Trailing stop walks up via half-pivot levels (R0.5, R1, R1.5, R2, ...).
  - Max 1 trade per day. EOD exit at session_end.

Pivot calc (classic floor):
  P  = (PH + PL + PC) / 3
  R1 = 2*P - PL          S1 = 2*P - PH
  R2 = P + (PH - PL)     S2 = P - (PH - PL)
  R3 = R1 + (PH - PL)    R4 = R3 + (PH - PL)    R5 = R4 + (PH - PL)
  Half-pivots: midpoints between adjacent levels.
"""

from dataclasses import dataclass
from datetime import date, time
from typing import Dict, List, Optional

import pandas as pd

from backtest.engine import Bar, Order
from backtest.portfolio import Portfolio


@dataclass
class ORBPivotsConfig:
    or_window_start: time = time(9, 30)
    or_window_end: time = time(9, 45)
    eod_exit: time = time(15, 55)
    risk_per_trade_pct: float = 0.01
    max_trades_per_day: int = 1


def compute_daily_pivots(daily_df: pd.DataFrame) -> pd.DataFrame:
    """
    Given a daily OHLCV DataFrame indexed by date, return a DataFrame indexed
    by the SAME dates with columns for today's pivots derived from PRIOR
    day's HLC. Pivots indexed by date D apply to trading day D.
    """
    out = pd.DataFrame(index=daily_df.index)
    ph = daily_df["high"].shift(1)
    pl = daily_df["low"].shift(1)
    pc = daily_df["close"].shift(1)
    out["pdh"] = ph
    out["pdl"] = pl
    out["pdc"] = pc
    p = (ph + pl + pc) / 3
    rng = ph - pl
    out["P"] = p
    out["R1"] = 2 * p - pl
    out["R2"] = p + rng
    out["R3"] = out["R1"] + rng
    out["R4"] = out["R3"] + rng
    out["R5"] = out["R4"] + rng
    out["S1"] = 2 * p - ph
    out["S2"] = p - rng
    out["R05"] = (p + out["R1"]) / 2
    out["R15"] = (out["R1"] + out["R2"]) / 2
    out["R25"] = (out["R2"] + out["R3"]) / 2
    out["R35"] = (out["R3"] + out["R4"]) / 2
    out["R45"] = (out["R4"] + out["R5"]) / 2
    return out


class ORBPivotsStrategy:
    def __init__(
        self,
        symbol: str,
        daily_df: pd.DataFrame,
        config: Optional[ORBPivotsConfig] = None,
    ):
        self.symbol = symbol
        self.cfg = config or ORBPivotsConfig()
        self.pivots = compute_daily_pivots(daily_df)
        self._pivot_lookup = {d.date(): row for d, row in self.pivots.iterrows()}

        self.current_date: Optional[date] = None
        self.or_high: Optional[float] = None
        self.or_low: Optional[float] = None
        self.or_complete: bool = False
        self.trades_today: int = 0
        self.in_position: bool = False
        self.entry_price: Optional[float] = None
        self.stop_price: Optional[float] = None
        self._trail_ladder: List[float] = []
        self._trail_idx: int = 0
        self._open_qty: int = 0
        self.trades_log: List[dict] = []

    def _reset_day(self, d: date):
        self.current_date = d
        self.or_high = None
        self.or_low = None
        self.or_complete = False
        self.trades_today = 0

    def _close_long(self, ts, price, reason: str) -> List[Order]:
        if not self.in_position:
            return []
        order = Order(symbol=self.symbol, qty=self._open_qty, side="sell", type="market")
        self.trades_log.append({
            "exit_ts": ts, "exit_price": price, "reason": reason,
            "entry_price": self.entry_price,
        })
        self.in_position = False
        self.entry_price = None
        self.stop_price = None
        self._trail_ladder = []
        self._trail_idx = 0
        self._open_qty = 0
        return [order]

    def _build_trail_ladder(self, today_pivots) -> List[float]:
        ladder_keys = ["R05", "R1", "R15", "R2", "R25", "R3", "R35", "R4", "R45", "R5"]
        levels = []
        for k in ladder_keys:
            v = today_pivots.get(k)
            if v is not None and not pd.isna(v):
                levels.append(float(v))
        levels.sort()
        return levels

    def on_bar(self, ts, bars: Dict[str, Bar], portfolio: Portfolio) -> List[Order]:
        if self.symbol not in bars:
            return []
        bar = bars[self.symbol]
        d = ts.date()
        t = ts.time()

        if self.current_date != d:
            if self.in_position:
                exits = self._close_long(ts, bar.open, "new_day_force_exit")
                self._reset_day(d)
                return exits
            self._reset_day(d)

        today_pivots = self._pivot_lookup.get(d)
        if today_pivots is None or pd.isna(today_pivots.get("R1")):
            return []

        if self.cfg.or_window_start <= t < self.cfg.or_window_end:
            self.or_high = bar.high if self.or_high is None else max(self.or_high, bar.high)
            self.or_low = bar.low if self.or_low is None else min(self.or_low, bar.low)
            return []

        if not self.or_complete and t >= self.cfg.or_window_end:
            if self.or_high is not None and self.or_low is not None:
                self.or_complete = True

        if t >= self.cfg.eod_exit and self.in_position:
            return self._close_long(ts, bar.close, "eod")

        if self.in_position:
            for i in range(self._trail_idx, len(self._trail_ladder)):
                lvl = self._trail_ladder[i]
                if bar.high >= lvl:
                    self._trail_idx = i + 1
                    new_stop = lvl
                    if i >= 1:
                        new_stop = self._trail_ladder[i - 1]
                    if new_stop > self.stop_price:
                        self.stop_price = new_stop
                else:
                    break
            if bar.low <= self.stop_price:
                return self._close_long(ts, self.stop_price, "trailing_stop")
            return []

        if not self.or_complete or self.trades_today >= self.cfg.max_trades_per_day:
            return []
        if t >= self.cfg.eod_exit:
            return []

        r1 = float(today_pivots["R1"])
        pdl = float(today_pivots["pdl"])
        if pd.isna(r1) or pd.isna(pdl):
            return []

        if (
            r1 > self.or_high
            and bar.open < self.or_high
            and bar.high > self.or_high
        ):
            entry_price = self.or_high
            stop_price = pdl
            if stop_price >= entry_price:
                stop_price = entry_price * 0.99
            risk_per_share = entry_price - stop_price
            if risk_per_share <= 0:
                return []
            equity = portfolio.equity({self.symbol: bar.close})
            risk_dollars = equity * self.cfg.risk_per_trade_pct
            qty = max(int(risk_dollars / risk_per_share), 0)
            qty = min(qty, int(portfolio.cash * 0.95 / entry_price))
            if qty == 0:
                return []
            self.in_position = True
            self.entry_price = entry_price
            self.stop_price = stop_price
            self.trades_today += 1
            self._trail_ladder = self._build_trail_ladder(today_pivots)
            self._trail_idx = 0
            self._open_qty = qty
            return [Order(symbol=self.symbol, qty=qty, side="buy", type="market")]

        return []
