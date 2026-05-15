"""
drift.py — Measure post-open drift on Five-Pillar gappers.

For each (date, ticker) qualifying pair, fetch 1m bars and compute returns at
several horizons from the 9:30 open. Also computes max favorable / max adverse
excursion to characterize the path, not just the endpoints.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

import pandas as pd

from backtest.data import load_bars


@dataclass
class DriftResult:
    date: str
    ticker: str
    open_930: float
    ret_30min_pct: Optional[float]
    ret_60min_pct: Optional[float]
    ret_2h_pct: Optional[float]
    ret_close_pct: Optional[float]
    max_favorable_pct: Optional[float]
    max_adverse_pct: Optional[float]
    halted_or_missing: bool


def _bar_at_or_after(df: pd.DataFrame, target_dt: datetime) -> Optional[pd.Series]:
    after = df[df.index >= target_dt]
    if after.empty:
        return None
    return after.iloc[0]


def _bar_at_or_before(df: pd.DataFrame, target_dt: datetime) -> Optional[pd.Series]:
    before = df[df.index <= target_dt]
    if before.empty:
        return None
    return before.iloc[-1]


def measure_one(
    ticker: str,
    date_iso: str,
    intraday_only: bool = True,
) -> DriftResult:
    """
    Measure drift for a single (ticker, date).

    Anchors to the 9:30 ET open. Returns are bid/ask-blind close prices
    (no slippage modeled at this layer — that's for the next strategy).
    """
    d = datetime.fromisoformat(date_iso).date()
    start = datetime.combine(d, time(9, 0)).isoformat()
    end = datetime.combine(d, time(16, 30)).isoformat()

    try:
        bars_dict = load_bars([ticker], start=start, end=end, interval="1m")
    except Exception:
        return DriftResult(
            date=date_iso, ticker=ticker, open_930=float("nan"),
            ret_30min_pct=None, ret_60min_pct=None, ret_2h_pct=None,
            ret_close_pct=None, max_favorable_pct=None, max_adverse_pct=None,
            halted_or_missing=True,
        )

    if ticker not in bars_dict or bars_dict[ticker].empty:
        return DriftResult(
            date=date_iso, ticker=ticker, open_930=float("nan"),
            ret_30min_pct=None, ret_60min_pct=None, ret_2h_pct=None,
            ret_close_pct=None, max_favorable_pct=None, max_adverse_pct=None,
            halted_or_missing=True,
        )

    df = bars_dict[ticker]

    rth = df[(df.index.time >= time(9, 30)) & (df.index.time <= time(16, 0))]
    if rth.empty:
        return DriftResult(
            date=date_iso, ticker=ticker, open_930=float("nan"),
            ret_30min_pct=None, ret_60min_pct=None, ret_2h_pct=None,
            ret_close_pct=None, max_favorable_pct=None, max_adverse_pct=None,
            halted_or_missing=True,
        )

    open_bar = _bar_at_or_after(rth, datetime.combine(d, time(9, 30)))
    if open_bar is None:
        return DriftResult(
            date=date_iso, ticker=ticker, open_930=float("nan"),
            ret_30min_pct=None, ret_60min_pct=None, ret_2h_pct=None,
            ret_close_pct=None, max_favorable_pct=None, max_adverse_pct=None,
            halted_or_missing=True,
        )
    open_price = float(open_bar["open"])

    def ret_at(target_dt: datetime) -> Optional[float]:
        bar = _bar_at_or_before(rth, target_dt)
        if bar is None:
            return None
        return (float(bar["close"]) - open_price) / open_price * 100.0

    r30 = ret_at(datetime.combine(d, time(10, 0)))
    r60 = ret_at(datetime.combine(d, time(10, 30)))
    r2h = ret_at(datetime.combine(d, time(11, 30)))
    rc = ret_at(datetime.combine(d, time(15, 59)))

    after_open = rth[rth.index >= datetime.combine(d, time(9, 30))]
    if after_open.empty:
        max_fav = max_adv = None
    else:
        max_high = after_open["high"].max()
        min_low = after_open["low"].min()
        max_fav = (max_high - open_price) / open_price * 100.0
        max_adv = (min_low - open_price) / open_price * 100.0

    return DriftResult(
        date=date_iso, ticker=ticker, open_930=open_price,
        ret_30min_pct=r30, ret_60min_pct=r60, ret_2h_pct=r2h,
        ret_close_pct=rc,
        max_favorable_pct=max_fav, max_adverse_pct=max_adv,
        halted_or_missing=False,
    )


def measure_universe(qualifiers: pd.DataFrame) -> pd.DataFrame:
    """
    qualifiers: DataFrame with columns date, ticker (from scanner.scan_range).
    Returns: DataFrame with one row per (date, ticker) and all drift metrics.
    """
    rows: List[dict] = []
    for _, q in qualifiers.iterrows():
        r = measure_one(q["ticker"], q["date"])
        rows.append(r.__dict__)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "VERO"
    d = sys.argv[2] if len(sys.argv) > 2 else "2024-06-07"
    r = measure_one(ticker, d)
    for k, v in r.__dict__.items():
        print(f"  {k}: {v}")
