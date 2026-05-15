"""
strategy.py — TJR Smart Money Concept strategy as a backtest Strategy.

Implements TJR's published multi-timeframe playbook (simplified for v1):

  1. Higher TF (1h) bias from swing structure (HH/HL = bull, LH/LL = bear)
  2. Wait for a liquidity sweep on 1h against the bias direction
  3. Wait for a Break of Structure on the 5m execution TF in bias direction
  4. Enter on first 5m bar that closes inside an active confluence
     (Fair Value Gap or Order Block) in the bias direction
  5. Stop = beyond the sweep extreme + ATR buffer
  6. Target = fixed R-multiple (default 2R)
  7. Position size = fixed % portfolio risk per trade
  8. One position at a time, bias-direction only

This is faithful to TJR's rules; the gates are deliberately mechanical so
the result is repeatable. Better entries (1m BOS), kill zones, and BPR
overlap are TODO for v2 if v1 shows any edge.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Literal, Optional

import pandas as pd

from backtest.data import resample_bars
from backtest.engine import Bar, Order
from backtest.portfolio import Portfolio
from strategies.smc.primitives import (
    FVG, OrderBlock, atr, fair_value_gaps, order_blocks, swing_points,
)
from strategies.smc.structure import (
    LiquiditySweep, bias_from_swings, detect_bos, detect_liquidity_sweep,
)


@dataclass
class TJRConfig:
    htf_rule: str = "1h"
    htf_swing_lookback: int = 2
    ltf_swing_lookback: int = 2
    require_htf_sweep: bool = True
    sweep_max_age_bars: int = 30
    fvg_min_size_atr: float = 0.30
    ob_displacement_atr: float = 1.5
    risk_per_trade_pct: float = 0.01
    r_multiple_target: float = 2.0
    stop_buffer_atr: float = 0.25
    max_bars_in_trade: int = 36
    setup_max_age_bars: int = 24


@dataclass
class _OpenTrade:
    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    entry_bar_index: int
    entry_price: float
    stop_price: float
    target_price: float


class TJRStrategy:
    """
    TJR multi-timeframe strategy. Construct with the execution-TF DataFrame
    (e.g. SPY 5m bars). HTF state is computed by resampling internally.
    """

    def __init__(
        self,
        symbol: str,
        ltf_df: pd.DataFrame,
        config: Optional[TJRConfig] = None,
    ):
        self.symbol = symbol
        self.cfg = config or TJRConfig()

        self.ltf_df = ltf_df.sort_index()
        self.htf_df = resample_bars(self.ltf_df, self.cfg.htf_rule)

        self.ltf_swings = swing_points(self.ltf_df, self.cfg.ltf_swing_lookback)
        self.htf_swings = swing_points(self.htf_df, self.cfg.htf_swing_lookback)
        self.ltf_atr = atr(self.ltf_df, period=14)

        self.htf_sweeps = detect_liquidity_sweep(
            self.htf_df, self.htf_swings,
            max_age_bars=self.cfg.sweep_max_age_bars,
        )
        self.ltf_bos = detect_bos(self.ltf_df, self.ltf_swings)
        self.ltf_fvgs = fair_value_gaps(
            self.ltf_df, min_size_atr=self.cfg.fvg_min_size_atr,
        )
        self.ltf_obs = order_blocks(
            self.ltf_df, swings=self.ltf_swings,
            displacement_atr=self.cfg.ob_displacement_atr,
        )

        self._htf_index_for: Dict[datetime, int] = {}
        for i, ts in enumerate(self.htf_df.index):
            self._htf_index_for[ts] = i
        self._htf_timestamps = list(self.htf_df.index)

        self._sweeps_by_ts: Dict[datetime, LiquiditySweep] = {
            s.timestamp: s for s in self.htf_sweeps
        }
        self._bos_by_ts = {b.timestamp: b for b in self.ltf_bos}

        self._ts_to_ltf_idx: Dict[datetime, int] = {
            ts: i for i, ts in enumerate(self.ltf_df.index)
        }

        self._open_trade: Optional[_OpenTrade] = None
        self._last_sweep: Optional[LiquiditySweep] = None
        self._last_sweep_bar_index: Optional[int] = None
        self._bos_after_sweep: bool = False
        self.trade_log: List[dict] = []

    def _htf_bias_at(self, ts: datetime) -> Literal["bull", "bear", "neutral"]:
        htf_i = self._most_recent_htf_index_at(ts)
        if htf_i is None or htf_i < 0:
            return "neutral"
        return bias_from_swings(self.htf_swings, as_of_index=htf_i)

    def _most_recent_htf_index_at(self, ts: datetime) -> Optional[int]:
        i = 0
        for j, htf_ts in enumerate(self._htf_timestamps):
            if htf_ts <= ts:
                i = j
            else:
                return i if j > 0 else None
        return i

    def _check_recent_sweep(self, ts: datetime, bias: str) -> Optional[LiquiditySweep]:
        if not self.cfg.require_htf_sweep:
            return LiquiditySweep(ts, 0.0, 0.0, "bull" if bias == "bull" else "bear", ts)

        wanted_dir = "bull" if bias == "bull" else "bear"
        cutoff = ts - timedelta(hours=4)
        for s in reversed(self.htf_sweeps):
            if s.timestamp > ts:
                continue
            if s.timestamp < cutoff:
                break
            if s.direction == wanted_dir:
                return s
        return None

    def _active_confluences(self, ts: datetime, price: float, bias: str) -> List:
        wanted = "bull" if bias == "bull" else "bear"
        out = []
        for f in self.ltf_fvgs:
            if f.direction != wanted:
                continue
            if f.end_ts >= ts:
                continue
            if f.contains(price):
                out.append(f)
        for o in self.ltf_obs:
            if o.direction != wanted:
                continue
            if o.timestamp >= ts:
                continue
            if o.contains(price):
                out.append(o)
        return out

    def on_bar(
        self,
        timestamp: datetime,
        bars: Dict[str, Bar],
        portfolio: Portfolio,
    ) -> List[Order]:
        bar = bars.get(self.symbol)
        if bar is None:
            return []

        if self._open_trade is not None:
            return self._manage_open_trade(timestamp, bar, portfolio)

        bias = self._htf_bias_at(timestamp)
        if bias == "neutral":
            return []

        ltf_idx = self._ts_to_ltf_idx.get(timestamp)
        if ltf_idx is None or ltf_idx < 20:
            return []

        sweep = self._check_recent_sweep(timestamp, bias)
        if sweep is None:
            return []
        if (self._last_sweep is None
                or self._last_sweep.timestamp != sweep.timestamp):
            self._last_sweep = sweep
            self._last_sweep_bar_index = ltf_idx
            self._bos_after_sweep = False

        bos = self._bos_by_ts.get(timestamp)
        if bos is not None and bos.direction == bias:
            self._bos_after_sweep = True
        if not self._bos_after_sweep:
            return []
        if (self._last_sweep_bar_index is not None
                and ltf_idx - self._last_sweep_bar_index > self.cfg.setup_max_age_bars):
            self._last_sweep = None
            self._bos_after_sweep = False
            return []

        confluences = self._active_confluences(timestamp, bar.close, bias)
        if not confluences:
            return []

        return self._submit_entry(timestamp, bar, bias, sweep, portfolio)

    def _submit_entry(
        self, ts, bar, bias, sweep, portfolio,
    ) -> List[Order]:
        cur_atr = float(self.ltf_atr.iloc[self._ts_to_ltf_idx[ts]])
        if cur_atr <= 0:
            return []

        if bias == "bull":
            stop = sweep.extreme_price - self.cfg.stop_buffer_atr * cur_atr
            entry_est = bar.close
            risk = entry_est - stop
            if risk <= 0:
                return []
            target = entry_est + self.cfg.r_multiple_target * risk
            side: Literal["buy", "sell"] = "buy"
        else:
            stop = sweep.extreme_price + self.cfg.stop_buffer_atr * cur_atr
            entry_est = bar.close
            risk = stop - entry_est
            if risk <= 0:
                return []
            target = entry_est - self.cfg.r_multiple_target * risk
            side = "sell"

        equity = portfolio.equity({self.symbol: bar.close})
        risk_dollars = equity * self.cfg.risk_per_trade_pct
        qty = max(int(risk_dollars / risk), 0)
        if qty == 0:
            return []

        notional = qty * entry_est
        if side == "buy" and notional > portfolio.cash:
            qty = int(portfolio.cash / entry_est)
            if qty == 0:
                return []
        if side == "sell":
            held = portfolio.qty(self.symbol)
            if held < qty:
                return []

        self._open_trade = _OpenTrade(
            symbol=self.symbol, side=side, qty=qty,
            entry_bar_index=self._ts_to_ltf_idx[ts] + 1,
            entry_price=entry_est, stop_price=stop, target_price=target,
        )
        self._last_sweep = None
        self._bos_after_sweep = False

        return [Order(symbol=self.symbol, qty=qty, side=side, type="market")]

    def _manage_open_trade(
        self, ts, bar, portfolio,
    ) -> List[Order]:
        ot = self._open_trade
        assert ot is not None
        ltf_idx = self._ts_to_ltf_idx.get(ts, -1)
        bars_held = ltf_idx - ot.entry_bar_index

        exit_reason = None
        if ot.side == "buy":
            if bar.low <= ot.stop_price:
                exit_reason = "stop"
            elif bar.high >= ot.target_price:
                exit_reason = "target"
        else:
            if bar.high >= ot.stop_price:
                exit_reason = "stop"
            elif bar.low <= ot.target_price:
                exit_reason = "target"

        if exit_reason is None and bars_held >= self.cfg.max_bars_in_trade:
            exit_reason = "time"

        if exit_reason is None:
            return []

        close_side: Literal["buy", "sell"] = "sell" if ot.side == "buy" else "buy"
        self.trade_log.append({
            "entry_ts": self.ltf_df.index[ot.entry_bar_index]
                        if ot.entry_bar_index < len(self.ltf_df) else ts,
            "exit_ts": ts,
            "side": ot.side,
            "qty": ot.qty,
            "entry_price": ot.entry_price,
            "stop": ot.stop_price,
            "target": ot.target_price,
            "exit_reason": exit_reason,
            "bars_held": bars_held,
        })
        self._open_trade = None
        return [Order(symbol=self.symbol, qty=ot.qty, side=close_side, type="market")]
