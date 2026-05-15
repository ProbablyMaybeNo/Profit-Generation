"""
execution.py — Ross Cameron's micro-pullback strategy on 1m bars.

State machine:
    WAIT_IMPULSE  -- counting consecutive green candles. Need >= impulse_min.
    WAIT_PULLBACK -- saw impulse, counting red candles (1-3 allowed).
    ARMED         -- pullback complete; waiting for next bar to break the
                     prior candle's high (the entry trigger).
    IN_TRADE      -- position open, managing exits.

Exit rules (any one triggers):
    - Hard stop: bar low <= stop_price
    - Hard target: bar high >= target_price (entry + r_multiple_target * R)
    - Time stop: bars_in_trade >= max_bars_in_trade
    - 9 EMA break (after trade is in profit): close < EMA(9)

Slippage: entry pays slippage_bps over trigger, exit pays slippage_bps under
exit price. Combine with the pullback entry mechanic, this means a tight
trade can lose 1-2x slippage_bps just from the round-trip.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import List, Literal, Optional

import numpy as np
import pandas as pd

from backtest.data import load_bars


@dataclass
class StrategyConfig:
    impulse_min: int = 3
    max_pullback_bars: int = 3
    max_pullbacks_per_day: int = 2
    arm_window_bars: int = 4
    r_multiple_target: float = 2.0
    max_bars_in_trade: int = 30
    use_ema_trail: bool = True
    ema_period: int = 9
    slippage_bps_one_way: float = 25.0
    risk_per_trade_pct: float = 0.01
    initial_equity: float = 10_000.0
    session_start: time = time(9, 30)
    session_end: time = time(11, 0)
    require_volume_decline_in_pullback: bool = True


@dataclass
class Trade:
    date: str
    ticker: str
    entry_ts: datetime
    entry_price: float
    stop_price: float
    target_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str
    qty: int
    pullback_ordinal: int
    r_at_entry: float
    pnl_dollars: float
    pnl_r: float


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def run_one_day(
    ticker: str,
    date_iso: str,
    config: Optional[StrategyConfig] = None,
    equity: Optional[float] = None,
) -> List[Trade]:
    """Run the strategy for a single (ticker, date). Returns 0..N trades."""
    cfg = config or StrategyConfig()
    eq = equity if equity is not None else cfg.initial_equity

    d = datetime.fromisoformat(date_iso).date()
    start = datetime.combine(d, time(9, 0)).isoformat()
    end = datetime.combine(d, time(16, 30)).isoformat()
    try:
        data = load_bars([ticker], start=start, end=end, interval="1m")
    except Exception:
        return []
    if ticker not in data or data[ticker].empty:
        return []
    df = data[ticker]

    rth = df[(df.index.time >= cfg.session_start) & (df.index.time <= cfg.session_end)].copy()
    if len(rth) < 10:
        return []

    rth["ema9"] = _ema(rth["close"], cfg.ema_period)

    state: Literal["WAIT_IMPULSE", "WAIT_PULLBACK", "ARMED", "IN_TRADE"] = "WAIT_IMPULSE"
    impulse_count = 0
    pullback_lows: List[float] = []
    pullback_volumes: List[float] = []
    impulse_volumes: List[float] = []
    breakout_level: Optional[float] = None
    armed_for_bars = 0
    pullbacks_today = 0
    in_trade: dict = {}
    trades: List[Trade] = []

    bars = list(rth.itertuples())

    def reset_to_wait_impulse():
        nonlocal state, impulse_count, pullback_lows, pullback_volumes, impulse_volumes
        nonlocal breakout_level, armed_for_bars
        state = "WAIT_IMPULSE"
        impulse_count = 0
        pullback_lows = []
        pullback_volumes = []
        impulse_volumes = []
        breakout_level = None
        armed_for_bars = 0

    for i, bar in enumerate(bars):
        ts = bar.Index
        o, h, l, c, v = bar.open, bar.high, bar.low, bar.close, bar.volume
        ema9 = bar.ema9
        is_green = c > o
        is_red = c < o

        if state == "IN_TRADE":
            t = in_trade
            t["bars_in_trade"] += 1

            stop_hit = l <= t["stop_price"]
            target_hit = h >= t["target_price"]
            time_stop = t["bars_in_trade"] >= cfg.max_bars_in_trade
            ema_exit = (
                cfg.use_ema_trail
                and c > t["entry_price"]
                and not np.isnan(ema9)
                and c < ema9
                and t["bars_in_trade"] >= 2
            )

            exit_price: Optional[float] = None
            exit_reason: Optional[str] = None
            if stop_hit and target_hit:
                exit_price = t["stop_price"]
                exit_reason = "stop"
            elif stop_hit:
                exit_price = t["stop_price"]
                exit_reason = "stop"
            elif target_hit:
                exit_price = t["target_price"]
                exit_reason = "target"
            elif ema_exit:
                exit_price = c
                exit_reason = "ema_trail"
            elif time_stop:
                exit_price = c
                exit_reason = "time"

            if exit_price is not None:
                slip = cfg.slippage_bps_one_way / 10_000.0
                fill = exit_price * (1 - slip)
                pnl_dollar = (fill - t["entry_price"]) * t["qty"]
                pnl_r = (fill - t["entry_price"]) / max(t["r_at_entry"], 1e-9)
                trades.append(Trade(
                    date=date_iso, ticker=ticker,
                    entry_ts=t["entry_ts"], entry_price=t["entry_price"],
                    stop_price=t["stop_price"], target_price=t["target_price"],
                    exit_ts=ts, exit_price=fill, exit_reason=exit_reason,
                    qty=t["qty"], pullback_ordinal=t["pullback_ordinal"],
                    r_at_entry=t["r_at_entry"],
                    pnl_dollars=pnl_dollar, pnl_r=pnl_r,
                ))
                eq += pnl_dollar
                in_trade = {}
                reset_to_wait_impulse()
            continue

        if state == "WAIT_IMPULSE":
            if is_green:
                impulse_count += 1
                impulse_volumes.append(v)
                if impulse_count >= cfg.impulse_min:
                    state = "WAIT_PULLBACK"
            else:
                impulse_count = 0
                impulse_volumes = []
            continue

        if state == "WAIT_PULLBACK":
            if is_red:
                pullback_lows.append(l)
                pullback_volumes.append(v)
                if len(pullback_lows) > cfg.max_pullback_bars:
                    reset_to_wait_impulse()
                    continue
            elif is_green:
                if not pullback_lows:
                    impulse_count += 1
                    impulse_volumes.append(v)
                    continue
                if cfg.require_volume_decline_in_pullback and impulse_volumes:
                    avg_imp_vol = np.mean(impulse_volumes)
                    avg_pb_vol = np.mean(pullback_volumes) if pullback_volumes else avg_imp_vol
                    if avg_pb_vol >= avg_imp_vol:
                        reset_to_wait_impulse()
                        continue
                state = "ARMED"
                breakout_level = h
                armed_for_bars = 0
            continue

        if state == "ARMED":
            armed_for_bars += 1
            if h > breakout_level:
                if pullbacks_today >= cfg.max_pullbacks_per_day:
                    reset_to_wait_impulse()
                    continue
                slip = cfg.slippage_bps_one_way / 10_000.0
                trigger_price = breakout_level
                entry_fill = trigger_price * (1 + slip)
                stop_price = min(pullback_lows)
                if stop_price >= entry_fill:
                    reset_to_wait_impulse()
                    continue
                r = entry_fill - stop_price
                target_price = entry_fill + cfg.r_multiple_target * r
                risk_dollars = eq * cfg.risk_per_trade_pct
                qty = max(int(risk_dollars / r), 0)
                if qty == 0:
                    reset_to_wait_impulse()
                    continue
                pullbacks_today += 1
                in_trade = {
                    "entry_ts": ts, "entry_price": entry_fill,
                    "stop_price": stop_price, "target_price": target_price,
                    "qty": qty, "r_at_entry": r, "bars_in_trade": 0,
                    "pullback_ordinal": pullbacks_today,
                }
                state = "IN_TRADE"
                continue
            if armed_for_bars >= cfg.arm_window_bars:
                reset_to_wait_impulse()
                continue
            if is_red:
                reset_to_wait_impulse()
                continue
    return trades


def run_universe(
    qualifiers: pd.DataFrame,
    config: Optional[StrategyConfig] = None,
) -> List[Trade]:
    cfg = config or StrategyConfig()
    eq = cfg.initial_equity
    all_trades: List[Trade] = []
    for _, q in qualifiers.iterrows():
        day_trades = run_one_day(q["ticker"], q["date"], cfg, equity=eq)
        for t in day_trades:
            eq += t.pnl_dollars
        all_trades.extend(day_trades)
    return all_trades


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MLGO"
    d = sys.argv[2] if len(sys.argv) > 2 else "2024-06-04"
    trades = run_one_day(ticker, d)
    print(f"Trades on {ticker} {d}: {len(trades)}")
    for t in trades:
        print(f"  entry {t.entry_ts.time()} ${t.entry_price:.2f}  stop ${t.stop_price:.2f}  "
              f"target ${t.target_price:.2f}  exit {t.exit_ts.time()} ${t.exit_price:.2f}  "
              f"reason={t.exit_reason}  qty={t.qty}  pnl=${t.pnl_dollars:+.2f} ({t.pnl_r:+.2f}R)  "
              f"pullback#{t.pullback_ordinal}")
