"""
data.py — Load historical bars for backtesting.
yfinance for daily, Alpaca for intraday (free IEX feed has more history than yfinance).
"""

from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import pandas as pd

from config.cache import cache_get, cache_set, cached
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


def _iso_to_utc(s: str) -> datetime:
    """Parse an ISO timestamp to an aware UTC datetime. A tz-aware string
    (e.g. the intraday window built from ET-aware `now`) is converted by its
    real offset; a naive string (the daily date path) is assumed UTC, which
    preserves the long-standing behaviour for whole-day windows."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
        start=_iso_to_utc(start),
        end=_iso_to_utc(end),
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


_INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h"}

_INTERVAL_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240,
}


def _last_closed_bar_ts(now: datetime, interval: str) -> datetime:
    """Floor `now` to the most recent completed bar boundary for `interval`.

    Used as the cache key so repeated calls within the same bar window
    return the cached result instead of re-fetching. Naive datetimes are
    treated as local clock; tz-aware datetimes preserve their tz.
    """
    minutes = _INTERVAL_MINUTES[interval]
    floor = now.replace(second=0, microsecond=0)
    bucket = (floor.hour * 60 + floor.minute) // minutes * minutes
    floor = floor.replace(hour=bucket // 60, minute=bucket % 60)
    return floor


def load_intraday_bars(
    symbols: List[str],
    interval: str,
    lookback_bars: int,
    *,
    now: Optional[datetime] = None,
    fetcher: Optional[Callable[[str, str, str, str], pd.DataFrame]] = None,
) -> Dict[str, pd.DataFrame]:
    """
    Load the N most recent intraday bars at `interval` per symbol.

    Cached per (symbol, interval, last_closed_bar_ts) so repeat calls
    within the same bar window (e.g. the every-15-min schtask firing
    twice on jitter) return the cached frame instead of re-hitting the
    Alpaca bars API. Cache TTL is one bar duration — the next bar's
    close invalidates automatically.

    Parameters
    ----------
    symbols : list of str
        Equity tickers (crypto goes through crypto_adapter instead).
    interval : str
        One of: 1m, 5m, 15m, 30m, 1h, 4h.
    lookback_bars : int
        How many recent bars to return per symbol. Used to size the
        Alpaca request window with enough buffer for weekends / halts.
    now : datetime, optional
        Override "now" for tests. Defaults to datetime.now().
    fetcher : callable, optional
        Injected (symbol, start, end, interval) -> DataFrame for tests.
        Defaults to the alpaca-py path used by load_bars.

    Returns
    -------
    {symbol: DataFrame} — index=bar timestamp (America/New_York, naive),
    columns=open/high/low/close/volume, sorted ascending, length<=lookback_bars.
    Empty symbols are dropped.
    """
    if interval not in _INTRADAY_INTERVALS:
        raise ValueError(
            f"unsupported intraday interval {interval!r}; "
            f"choose from {sorted(_INTRADAY_INTERVALS)}"
        )
    if lookback_bars <= 0:
        raise ValueError(f"lookback_bars must be positive, got {lookback_bars}")

    now = now or datetime.now(timezone.utc)
    # A tz-aware `now` (the live path passes ET-aware time) is carried in UTC
    # so the start/end window strings are absolute. Without this, a naive
    # local wall-clock gets stamped UTC downstream and shifts the fetch window
    # by the machine's offset — on a PDT box that pulled premarket bars during
    # the live session, starving every intraday strategy of RTH data. Naive
    # `now` is left untouched so the unit suite's relative cache-bucket
    # behaviour is unchanged.
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    bar_close_ts = _last_closed_bar_ts(now, interval)
    bar_minutes = _INTERVAL_MINUTES[interval]
    cache_ttl = bar_minutes * 60.0

    # Window covers ~3x the requested span to absorb weekends, market
    # holidays, and overnight gaps for intraday data. Alpaca will only
    # return real bars regardless.
    span_minutes = lookback_bars * bar_minutes * 3 + 60 * 24
    start_dt = bar_close_ts - timedelta(minutes=span_minutes)
    end_dt = bar_close_ts + timedelta(minutes=bar_minutes)
    start = start_dt.isoformat(timespec="seconds")
    end = end_dt.isoformat(timespec="seconds")

    fetch = fetcher or _download_one_alpaca

    out: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        cache_key = (
            f"bars.intraday:{symbol}:{interval}:"
            f"{bar_close_ts.isoformat(timespec='minutes')}"
        )
        cached_df = cache_get(cache_key)
        if cached_df is not None:
            if not cached_df.empty:
                out[symbol] = cached_df.tail(lookback_bars).copy()
            continue
        try:
            df = fetch(symbol, start, end, interval)
        except Exception:
            df = pd.DataFrame()
        if df is None or df.empty:
            cache_set(cache_key, pd.DataFrame(), cache_ttl)
            continue
        df = df.sort_index().tail(lookback_bars)
        cache_set(cache_key, df, cache_ttl)
        out[symbol] = df.copy()
    return out
