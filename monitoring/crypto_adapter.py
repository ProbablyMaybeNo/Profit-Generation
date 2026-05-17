"""crypto_adapter.py — Crypto support via the Alpaca Crypto API.

Phase 3.4.1 adds first-class crypto handling without disturbing the
equity-trading path:

  - Symbol detection (`is_crypto_symbol`): treats anything in
    `monitoring.config.TRACKED_CRYPTO` as crypto.
  - Symbol normalization (`normalize_crypto_symbol`): converts our
    yfinance-style "BTC-USD" into Alpaca's "BTC/USD".
  - 24/7 bar loader (`load_crypto_bars`): wraps
    `alpaca.data.historical.CryptoHistoricalDataClient`. Lazy import so
    the unit suite never needs the alpaca-py module installed in the
    py-3.13 env (alpaca-py lives in the trading conda env).
  - Order submission (`submit_crypto_order`): wraps
    `alpaca.trading.requests.MarketOrderRequest` and the trading client.
  - Sizing override (`crypto_max_position_usd`): reads
    `settings.crypto.max_position_usd` (default 500.0) so wider crypto
    spreads don't blow through the equity-side cap.

The auto-trader calls `is_crypto_symbol` per signal; when True, the
sizing override is honored and orders route through `submit_crypto_order`
instead of the stock market-order path. Everything else (eligibility,
cool-down, regime gate, pause check, kill switch) is unchanged.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from monitoring.config import TRACKED_CRYPTO  # noqa: E402

DEFAULT_CRYPTO_MAX_POSITION_USD = 500.0


def is_crypto_symbol(symbol: str) -> bool:
    """True iff the symbol is in TRACKED_CRYPTO. Case-insensitive on the
    coin prefix; the suffix matters (USD vs USDT etc)."""
    if not symbol:
        return False
    s = str(symbol).strip().upper()
    return s in {c.upper() for c in TRACKED_CRYPTO}


def normalize_crypto_symbol(symbol: str) -> str:
    """`BTC-USD` → `BTC/USD`. Alpaca's crypto API uses slash-separated
    pairs; we store dash-separated on disk for parity with yfinance."""
    s = str(symbol).strip().upper()
    if "/" in s:
        return s
    if "-" in s:
        return s.replace("-", "/", 1)
    return s


def crypto_max_position_usd(settings: Dict) -> float:
    """Read `settings.crypto.max_position_usd` with a 500.0 default."""
    crypto_cfg = (settings or {}).get("crypto") or {}
    raw = crypto_cfg.get("max_position_usd", DEFAULT_CRYPTO_MAX_POSITION_USD)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        log(f"crypto.max_position_usd '{raw}' is not numeric; using default "
            f"{DEFAULT_CRYPTO_MAX_POSITION_USD}", "WARNING")
        return DEFAULT_CRYPTO_MAX_POSITION_USD
    if val <= 0:
        log(f"crypto.max_position_usd {val} <= 0 makes no sense; using "
            f"default {DEFAULT_CRYPTO_MAX_POSITION_USD}", "WARNING")
        return DEFAULT_CRYPTO_MAX_POSITION_USD
    return val


def crypto_symbols(settings: Optional[Dict] = None) -> List[str]:
    """The active crypto universe. Today: TRACKED_CRYPTO. A future
    settings.crypto.symbols list could override; for now we surface
    the static list so the scheduler/scan know what to ask for."""
    if settings and isinstance(settings.get("crypto"), dict):
        override = settings["crypto"].get("symbols")
        if isinstance(override, list) and override:
            return [str(s).strip().upper() for s in override if s]
    return list(TRACKED_CRYPTO)


# ---------------------------------------------------------------------------
# Alpaca-touching code paths (lazy imports — no side effects on bare import).
# ---------------------------------------------------------------------------


def _get_crypto_data_client():
    """Lazy import + construct the Alpaca CryptoHistoricalDataClient.
    The unit-test path never calls this — it injects a fake."""
    from alpaca.data.historical import CryptoHistoricalDataClient
    return CryptoHistoricalDataClient()  # no creds needed for public data


def load_crypto_bars(
    symbols: List[str],
    *,
    start: str,
    end: str,
    interval: str = "1d",
    client=None,
) -> Dict[str, "Any"]:
    """Return {symbol: pd.DataFrame(OHLCV)} for one or more crypto symbols.

    Mirrors `backtest.data.load_bars` shape. Empty dict if Alpaca returns
    nothing. The `client` arg accepts an injected fake; production code
    leaves it None so the lazy real client is constructed.
    """
    import pandas as pd
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    tf_map = {
        "1m":  (TimeFrameUnit.Minute, 1),
        "5m":  (TimeFrameUnit.Minute, 5),
        "15m": (TimeFrameUnit.Minute, 15),
        "1h":  (TimeFrameUnit.Hour,   1),
        "1d":  (TimeFrameUnit.Day,    1),
    }
    if interval not in tf_map:
        raise ValueError(f"unsupported crypto interval {interval!r}; "
                         f"choose from {list(tf_map)}")
    unit, amount = tf_map[interval]

    normalized = [normalize_crypto_symbol(s) for s in symbols]
    if client is None:
        client = _get_crypto_data_client()
    req = CryptoBarsRequest(
        symbol_or_symbols=normalized,
        timeframe=TimeFrame(amount, unit),
        start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
    )
    bars = client.get_crypto_bars(req)
    df_all = bars.df
    out: Dict[str, "pd.DataFrame"] = {}
    if df_all is None or df_all.empty:
        return out
    # MultiIndex (symbol, ts) → split per symbol.
    if isinstance(df_all.index, pd.MultiIndex):
        for sym in df_all.index.get_level_values(0).unique():
            sub = df_all.xs(sym, level=0).sort_index()
            sub = sub.rename(columns={c: c.lower() for c in sub.columns})
            cols = [c for c in ("open", "high", "low", "close", "volume")
                    if c in sub.columns]
            out[sym] = sub[cols]
    else:
        sym = normalized[0]
        sub = df_all.sort_index()
        sub = sub.rename(columns={c: c.lower() for c in sub.columns})
        cols = [c for c in ("open", "high", "low", "close", "volume")
                if c in sub.columns]
        out[sym] = sub[cols]
    return out


def build_crypto_market_order(
    *, symbol: str, qty: float, side: str,
    client_order_id: Optional[str] = None,
):
    """Build an Alpaca crypto MarketOrderRequest. Exposed for tests so the
    request shape can be asserted without a live trading client."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    side_enum = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
    return MarketOrderRequest(
        symbol=normalize_crypto_symbol(symbol),
        qty=float(qty),
        side=side_enum,
        time_in_force=TimeInForce.GTC,  # crypto requires GTC, not DAY
        client_order_id=client_order_id,
    )


def submit_crypto_order(
    client,
    *,
    symbol: str,
    qty: float,
    side: str,
    client_order_id: Optional[str] = None,
    builder: Optional[Callable] = None,
):
    """Submit a crypto market order via the trading client. Returns the
    Alpaca order object. `builder` lets tests inject a dummy request
    factory; production leaves it None to use `build_crypto_market_order`.
    """
    build = builder or build_crypto_market_order
    req = build(symbol=symbol, qty=qty, side=side,
                client_order_id=client_order_id)
    return client.submit_order(req)
