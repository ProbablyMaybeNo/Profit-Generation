"""
scanner.py — Ross Cameron's Five-Pillar momentum universe filter.

For Strategy 1 (drift baseline) we apply 3 of the 5 pillars:
  - Price between $1 and $20 (Pillar 1)
  - Daily gap up >= GAP_PCT vs prior close (Pillar 4)
  - Today's volume >= MIN_VOLUME (proxy for Pillar 5 relative volume)

Skipped for v1:
  - Float < 10M (yfinance lookup, expensive; will add as second-pass filter)
  - News catalyst (Polygon News, will add for Strategy 6)

Output: per-day DataFrame of qualifying tickers with the metrics that
qualified them, ready to feed into drift measurement.
"""

from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

from backtest.polygon_data import grouped_daily, trading_days


@dataclass
class ScannerConfig:
    min_price: float = 1.0
    max_price: float = 20.0
    min_gap_pct: float = 25.0
    max_gap_pct: float = 300.0
    min_volume: int = 1_000_000
    exclude_suffixes: tuple = ("W", "U", "R", "P")


def _is_common_stock(ticker: str, exclude: tuple) -> bool:
    """Filter warrants (W), units (U), rights (R), preferreds (P)."""
    if "." in ticker:
        return False
    if len(ticker) >= 5 and ticker[-1] in exclude:
        return False
    return True


def scan_day(
    today: str,
    prev_day: str,
    config: Optional[ScannerConfig] = None,
) -> pd.DataFrame:
    """
    Apply the filter to a single trading day.
    Returns DataFrame: ticker, open, prev_close, gap_pct, volume, close.
    Empty if nothing qualifies or data missing.
    """
    config = config or ScannerConfig()

    today_df = grouped_daily(today)
    prev_df = grouped_daily(prev_day)
    if today_df.empty or prev_df.empty:
        return pd.DataFrame()

    merged = today_df.merge(
        prev_df[["ticker", "close"]].rename(columns={"close": "prev_close"}),
        on="ticker",
        how="inner",
    )

    merged = merged[merged["prev_close"] > 0]
    merged["gap_pct"] = (merged["open"] - merged["prev_close"]) / merged["prev_close"] * 100

    common = merged["ticker"].map(lambda t: _is_common_stock(t, config.exclude_suffixes))
    qualifies = (
        common
        & (merged["open"] >= config.min_price)
        & (merged["open"] <= config.max_price)
        & (merged["gap_pct"] >= config.min_gap_pct)
        & (merged["gap_pct"] <= config.max_gap_pct)
        & (merged["volume"] >= config.min_volume)
    )

    return merged.loc[
        qualifies,
        ["ticker", "open", "prev_close", "gap_pct", "volume", "high", "low", "close"]
    ].sort_values("gap_pct", ascending=False).reset_index(drop=True)


def scan_range(
    start: str,
    end: str,
    config: Optional[ScannerConfig] = None,
) -> pd.DataFrame:
    """
    Scan every trading day in [start, end]. Returns long-format DataFrame
    with one row per qualifying (date, ticker).
    """
    days = trading_days(start, end)
    rows: List[pd.DataFrame] = []
    prev: Optional[str] = None
    for d in days:
        if prev is None:
            prev = d
            continue
        try:
            day_df = scan_day(d, prev, config)
        except Exception as e:
            print(f"  scan_day({d}) error: {e}")
            prev = d
            continue
        if not day_df.empty:
            day_df = day_df.copy()
            day_df.insert(0, "date", d)
            rows.append(day_df)
        prev = d
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


if __name__ == "__main__":
    import sys
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-06-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2024-06-30"
    print(f"Scanning {start} -> {end} ...")
    df = scan_range(start, end)
    print(f"\n{len(df)} qualifying (date, ticker) pairs over {len(df['date'].unique())} days")
    if not df.empty:
        per_day = df.groupby("date").size()
        print(f"Avg qualifiers per day: {per_day.mean():.1f}")
        print(f"Top 10 biggest gappers in window:")
        print(df.nlargest(10, "gap_pct")[
            ["date", "ticker", "open", "prev_close", "gap_pct", "volume"]
        ].to_string(index=False))
