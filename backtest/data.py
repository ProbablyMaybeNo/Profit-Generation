"""
data.py — Load historical bars for backtesting.
yfinance for daily, Alpaca for intraday (free IEX feed has more history than yfinance).
"""

from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd

from config.cache import cached
from config.utils import load_credentials


_TF_MAP = {
    "1m": ("Minute", 1),
    "5m": ("Minute", 5),
    "15m": ("Minute", 15),
    "30m": ("Minute", 30),
    "1h": ("Hour", 1),
    "4h": ("Hour", 4),
}


@cached(ttl=12 * 3600, namespace="bars.yf")
def _download_one_yf(symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(
        symbol,
        start=start,
        end=end,
        interval=interval,
        progress=False,
        auto_adjust=True,
        threads=False,
    )
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index)
    return df[["open", "high", "low", "close", "volume"]]


@cached(ttl=24 * 3600, namespace="bars.alpaca")
def _download_one_alpaca(symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    if interval not in _TF_MAP:
        raise ValueError(f"unsupported interval {interval}; choose from {list(_TF_MAP)}")
    unit_name, amount = _TF_MAP[interval]
    tf = TimeFrame(amount, getattr(TimeFrameUnit, unit_name))

    creds = load_credentials("alpaca")
    client = StockHistoricalDataClient(creds["api_key"], creds["secret_key"])

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        feed="iex",
    )
    bars = client.get_stock_bars(req)
    df = bars.df
    if df.empty:
        return df

    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    df.index = df.index.tz_convert("America/New_York").tz_localize(None)
    df = df.rename(columns={"open": "open", "high": "high", "low": "low",
                            "close": "close", "volume": "volume"})
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def load_bars(
    symbols: List[str],
    start: str,
    end: str,
    interval: str = "1d",
    source: str = "auto",
) -> Dict[str, pd.DataFrame]:
    """
    Return {symbol: DataFrame(index=ts, cols=open/high/low/close/volume)}.
    Cached. Drops symbols that come back empty.

    source:
      "auto"   — yfinance for 1d, Alpaca for intraday
      "yf"     — yfinance only
      "alpaca" — Alpaca only
    """
    use_alpaca = (
        source == "alpaca"
        or (source == "auto" and interval in _TF_MAP)
    )
    fetch = _download_one_alpaca if use_alpaca else _download_one_yf

    out: Dict[str, pd.DataFrame] = {}
    for s in symbols:
        df = fetch(s, start, end, interval)
        if not df.empty:
            out[s] = df
    return out


def resample_bars(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Resample OHLCV bars to a higher timeframe.
    rule examples: '5min', '15min', '1H', '4H', '1D'.
    """
    agg = {
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }
    out = df.resample(rule, label="right", closed="right").agg(agg)
    return out.dropna(subset=["open"])
