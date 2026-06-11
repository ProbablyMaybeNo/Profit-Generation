"""
candle_continuation.py — intraday long-only continuation strategy (Stage 3
of docs/INTRADAY_TREND_BUILD_PLAN.md).

The candle is the TRIGGER, not the edge. Per docs/INTRADAY_RESEARCH_FINDINGS.md
(Morning Star + basic filters = PF 0.79 loser; Three White Soldiers + RSI<35 =
83% WR), an entry requires a bullish pattern AND a minimum number of
confirmation gates:

  Gate 1  trend  : close > EMA_slow AND EMA_fast > EMA_slow (intraday uptrend)
  Gate 2  vwap   : close > session VWAP (riding above the day's fair value)
  Gate 3  level  : the bar TOUCHED a dynamic level (EMA_fast / EMA_slow / VWAP)
                   — i.e. it's a pullback entry, not chasing open air
  Gate 4  volume : pattern-bar volume > vol_mult × rolling-average volume
  Gate 5  pattern: a bullish pattern completed on this bar  (mandatory)

Entry = pattern fired AND (gates satisfied >= min_confirms) AND inside an
active time window. Time-of-day is a HARD filter (Quantpedia SPY 2010-2024):
allow 09:30-11:00 and 12:00-14:00 ET; block the 11:00-12:00 lull and (by
default) anything after 14:30.

Exit (long_exit) is the strategy's fast secondary exit; the execution layer's
ratcheting trailing stop remains the primary exit. long_exit fires on a
bearish reversal pattern (bearish engulfing / evening star — NOT shooting
star alone) or a close back below EMA_fast (trend break).

Output matches the codebase compute_fn contract: the same OHLCV frame with
boolean `long_entry` / `long_exit` columns. All inputs are causal (current +
prior bars only), so signals carry no lookahead; the engine acts next bar.
"""

from __future__ import annotations

from datetime import time as dtime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from strategies.intraday import candle_patterns as cp

DEFAULTS: Dict = {
    "ema_fast": 9,
    "ema_slow": 20,
    "vol_window": 20,
    "vol_mult": 1.0,
    "min_confirms": 3,
    "active_windows": [("09:30", "11:00"), ("12:00", "14:00")],
    "exit_on_ema_break": True,
    "exit_on_vwap_break": False,
}


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP. Resets each calendar day when the index is a
    DatetimeIndex; otherwise treats the whole frame as one session."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df.get("volume")
    if vol is None:
        return pd.Series(index=df.index, dtype="float64")
    tpv = typical * vol
    if isinstance(df.index, pd.DatetimeIndex):
        keys = df.index.normalize()
        cum_tpv = tpv.groupby(keys).cumsum()
        cum_vol = vol.groupby(keys).cumsum()
    else:
        cum_tpv = tpv.cumsum()
        cum_vol = vol.cumsum()
    return cum_tpv / cum_vol.replace(0, pd.NA)


def _parse_windows(windows: List[Tuple[str, str]]) -> List[Tuple[dtime, dtime]]:
    out = []
    for start, end in windows:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        out.append((dtime(sh, sm), dtime(eh, em)))
    return out


def time_mask(index, windows: List[Tuple[str, str]]) -> pd.Series:
    """Boolean Series: True where the bar's time-of-day is inside any window
    [start, end). Non-datetime indexes are unconstrained (all True) so offline
    fixtures aren't blocked."""
    if not isinstance(index, pd.DatetimeIndex):
        return pd.Series(True, index=index)
    parsed = _parse_windows(windows)
    times = index.time
    vals = [any(s <= t < e for s, e in parsed) for t in times]
    return pd.Series(vals, index=index)


def bullish_pattern_any(df: pd.DataFrame,
                        patterns: Optional[List[str]] = None) -> pd.Series:
    names = patterns or list(cp.BULLISH_PATTERNS)
    acc = pd.Series(False, index=df.index)
    for n in names:
        acc = acc | cp.BULLISH_PATTERNS[n](df)
    return acc


def bearish_exit_any(df: pd.DataFrame) -> pd.Series:
    """Reliable bearish exit triggers only — shooting_star is excluded (59%,
    near-random; never an exit on its own)."""
    return cp.bearish_engulfing(df) | cp.evening_star(df)


def combine_entry(pattern_any: pd.Series, trend: pd.Series, vwap_ok: pd.Series,
                  level_ok: pd.Series, vol_ok: pd.Series, time_ok: pd.Series,
                  min_confirms: int = 3) -> pd.Series:
    """Entry = pattern (mandatory) AND >= min_confirms gates AND in-window.
    The pattern itself counts toward the gate total (it is gate 5)."""
    gates = (trend.astype(int) + vwap_ok.astype(int) + level_ok.astype(int)
             + vol_ok.astype(int) + pattern_any.astype(int))
    return (pattern_any & (gates >= min_confirms) & time_ok).fillna(False)


def compute_candle_continuation(df: pd.DataFrame, **overrides) -> pd.DataFrame:
    cfg = {**DEFAULTS, **overrides}
    out = df.copy()

    ema_f = _ema(df["close"], cfg["ema_fast"])
    ema_s = _ema(df["close"], cfg["ema_slow"])
    vwap = session_vwap(df)

    trend = (df["close"] > ema_s) & (ema_f > ema_s)
    vwap_ok = (df["close"] > vwap).fillna(False)

    # level touch: bar's range straddles a dynamic level (pullback wick).
    def _touch(level: pd.Series) -> pd.Series:
        return ((df["low"] <= level) & (df["high"] >= level)).fillna(False)
    level_ok = _touch(ema_f) | _touch(ema_s) | _touch(vwap)

    vol = df.get("volume")
    if vol is None:
        vol_ok = pd.Series(False, index=df.index)
    else:
        avg = vol.rolling(cfg["vol_window"]).mean()
        vol_ok = (vol > cfg["vol_mult"] * avg).fillna(False)

    pattern_any = bullish_pattern_any(df, cfg.get("patterns"))
    time_ok = time_mask(df.index, cfg["active_windows"])

    out["long_entry"] = combine_entry(
        pattern_any, trend, vwap_ok, level_ok, vol_ok, time_ok,
        min_confirms=cfg["min_confirms"])

    exit_sig = bearish_exit_any(df)
    if cfg["exit_on_ema_break"]:
        exit_sig = exit_sig | (df["close"] < ema_f)
    if cfg["exit_on_vwap_break"]:
        exit_sig = exit_sig | (df["close"] < vwap)
    out["long_exit"] = exit_sig.fillna(False)

    out["ema_fast"] = ema_f
    out["ema_slow"] = ema_s
    out["vwap"] = vwap
    return out
