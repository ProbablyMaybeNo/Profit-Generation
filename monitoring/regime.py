"""regime.py — Daily pre-market market-risk regime score (Stage 2.1).

This is the *risk-environment* axis (risk_on / transitional / risk_off),
distinct from `regime_router.market_regime` (the trend-character axis:
trending_up / choppy / low_vol / ...). Both can be live at once: this score
decides how aggressively to deploy capital and which strategy families to
admit, the router decides per-strategy trend gating.

The score combines two rules-based, no-ML inputs (the cleanest documented
solo-operator edge — a VIX-200d-MA gate cut max DD -55%->-22% while
preserving returns in a 2005-2025 backtest):

  1. VIX vs its 200-day moving average — fear relative to its own trend.
     VIX below the 200d-MA = calm tape (risk-on bias); above = stress
     (risk-off bias).
  2. ADX(14) of a broad-market proxy (default SPY) — directional conviction.
     ADX < ~22 at the open = range/mean-reversion day; ADX > ~30 = a strong
     directional/trend day.

Both feed `score_regime`, which returns one of three labels plus a
`risk_scale` (1.0 / 0.5 / 0.25) that the sizing path multiplies into the
per-trade risk %, and a `confidence` in [0, 1].

Scoring is a pure function of its numeric inputs (offline-testable). The
`compute_and_persist_regime` wrapper wires the real VIX (from the `macro`
table, populated by macro_fetcher) and ADX (from an injected daily-bars
fetcher) and upserts one row per date into `regime_scores`. Failures never
raise — a missing input degrades to a conservative `transitional` default
so the rest of the pipeline keeps running.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402


# Regime labels.
RISK_ON = "risk_on"
TRANSITIONAL = "transitional"
RISK_OFF = "risk_off"
KNOWN_REGIMES = (RISK_ON, TRANSITIONAL, RISK_OFF)

# Per-regime sizing scale — multiplied into the per-trade risk %.
RISK_SCALE = {
    RISK_ON: 1.0,
    TRANSITIONAL: 0.5,
    RISK_OFF: 0.25,
}

# When we can't compute a score (no VIX / no ADX), default to the middle
# regime — half-size, neither fully on nor flat. Conservative but live.
DEFAULT_REGIME = TRANSITIONAL

# ADX thresholds (Wilder, period 14). Below LOW = range/chop; above HIGH =
# strong directional conviction.
ADX_PERIOD = 14
ADX_LOW = 22.0
ADX_HIGH = 30.0

# VIX moving-average window for the regime gate.
VIX_MA_WINDOW = 200

# Default broad-market proxy for ADX.
DEFAULT_PROXY_SYMBOL = "SPY"


# ---------------------------------------------------------------------------
# Pure indicators
# ---------------------------------------------------------------------------

def moving_average(values, window: int) -> Optional[float]:
    """Trailing simple MA over the last `window` values; None if too few."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < window or window <= 0:
        return None
    return sum(vals[-window:]) / window


def compute_adx(bars, period: int = ADX_PERIOD) -> Optional[float]:
    """Wilder's ADX over daily bars; None if there aren't enough bars.

    Accepts a pandas DataFrame (high/low/close, case-insensitive) or a list
    of dicts with the same keys. Mirrors the bar-normalization in stops.py.
    Needs at least 2*period+1 bars (period for the DI smoothing, period for
    the ADX smoothing) to return a value.
    """
    rows = _rows_from(bars)
    if len(rows) < 2 * period + 1:
        return None
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    trs: List[float] = []
    for i in range(1, len(rows)):
        up_move = rows[i]["high"] - rows[i - 1]["high"]
        down_move = rows[i - 1]["low"] - rows[i]["low"]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(
            down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr = max(
            rows[i]["high"] - rows[i]["low"],
            abs(rows[i]["high"] - rows[i - 1]["close"]),
            abs(rows[i]["low"] - rows[i - 1]["close"]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None

    # Wilder smoothing of TR, +DM, -DM.
    atr = sum(trs[:period])
    sm_plus = sum(plus_dm[:period])
    sm_minus = sum(minus_dm[:period])
    dxs: List[float] = []
    _append_dx(dxs, atr, sm_plus, sm_minus)
    for i in range(period, len(trs)):
        atr = atr - (atr / period) + trs[i]
        sm_plus = sm_plus - (sm_plus / period) + plus_dm[i]
        sm_minus = sm_minus - (sm_minus / period) + minus_dm[i]
        _append_dx(dxs, atr, sm_plus, sm_minus)
    if len(dxs) < period:
        return None
    # ADX = Wilder-smoothed average of DX.
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx, 4)


def _append_dx(dxs, atr, sm_plus, sm_minus) -> None:
    if atr <= 0:
        dxs.append(0.0)
        return
    plus_di = 100.0 * (sm_plus / atr)
    minus_di = 100.0 * (sm_minus / atr)
    denom = plus_di + minus_di
    if denom <= 0:
        dxs.append(0.0)
        return
    dxs.append(100.0 * abs(plus_di - minus_di) / denom)


def _rows_from(bars) -> List[Dict]:
    """Normalize bars to a list of dicts keyed high / low / close."""
    if bars is None:
        return []
    if hasattr(bars, "iterrows"):
        cols = {c.lower(): c for c in bars.columns}
        if not all(k in cols for k in ("high", "low", "close")):
            return []
        out: List[Dict] = []
        for _, row in bars.iterrows():
            try:
                out.append({
                    "high": float(row[cols["high"]]),
                    "low": float(row[cols["low"]]),
                    "close": float(row[cols["close"]]),
                })
            except (TypeError, ValueError):
                return []
        return out
    if isinstance(bars, list):
        out = []
        for b in bars:
            if not isinstance(b, dict):
                continue
            try:
                out.append({
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                })
            except (KeyError, TypeError, ValueError):
                return []
        return out
    return []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_regime(
    *,
    vix: Optional[float],
    vix_200dma: Optional[float],
    adx: Optional[float],
    adx_low: float = ADX_LOW,
    adx_high: float = ADX_HIGH,
) -> dict:
    """Combine the VIX-vs-200dMA gate and ADX into a regime label.

    Returns: {regime, risk_scale, confidence, vix_below_ma, detail}.

    Logic:
      - VIX gate: vix < vix_200dma  -> calm   (+1)
                  vix > vix_200dma  -> stress (-1)
                  unknown           ->  0
      - ADX gate: adx >= adx_high   -> strong directional conviction (+1)
                  adx < adx_low     -> range / chop (0, neutral for risk)
                  in between        -> mild (+0.5)
      - The two combine: a calm tape (VIX below MA) is risk_on; a stress
        tape (VIX above MA) is risk_off; everything else is transitional.
        A strong ADX on a calm tape reinforces risk_on confidence; a strong
        ADX on a stress tape (volatile selloff) keeps it risk_off.
      - When the VIX gate is unknown, ADX alone can only ever reach
        transitional (we never go full risk_on/off on one missing input).
    """
    vix_below_ma: Optional[bool] = None
    vix_signal = 0.0
    if vix is not None and vix_200dma is not None and vix_200dma > 0:
        vix_below_ma = float(vix) < float(vix_200dma)
        vix_signal = 1.0 if vix_below_ma else -1.0

    adx_signal: Optional[float] = None
    if adx is not None:
        if adx >= adx_high:
            adx_signal = 1.0
        elif adx < adx_low:
            adx_signal = 0.0
        else:
            adx_signal = 0.5

    if vix_signal > 0:
        regime = RISK_ON
    elif vix_signal < 0:
        regime = RISK_OFF
    else:
        regime = DEFAULT_REGIME  # VIX unknown -> transitional regardless of ADX

    # Confidence: 0.5 base for a known VIX gate, +up to 0.5 from ADX
    # agreement. A missing VIX gate caps confidence at 0.4 (we defaulted).
    if vix_below_ma is None:
        confidence = 0.4 if adx_signal is not None else 0.3
    else:
        confidence = 0.5
        if adx_signal is not None:
            # Strong directional conviction sharpens the read either way.
            confidence += 0.5 * adx_signal

    confidence = round(_clamp(confidence, 0.0, 1.0), 4)
    risk_scale = RISK_SCALE[regime]
    detail = (
        f"vix={_fmt(vix)} vix200ma={_fmt(vix_200dma)} "
        f"adx={_fmt(adx)} -> {regime} (scale {risk_scale})"
    )
    return {
        "regime": regime,
        "risk_scale": risk_scale,
        "confidence": confidence,
        "vix_below_ma": vix_below_ma,
        "detail": detail,
    }


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _fmt(v) -> str:
    return "n/a" if v is None else f"{float(v):.2f}"


# ---------------------------------------------------------------------------
# Inputs from the live system
# ---------------------------------------------------------------------------

def vix_inputs(conn) -> Dict[str, Optional[float]]:
    """Latest VIX close and its trailing 200d-MA from the macro table."""
    try:
        rows = conn.execute(
            "SELECT bar_date, value FROM macro "
            " WHERE series_id IN ('VIXCLS', 'VIX') AND value IS NOT NULL "
            " ORDER BY bar_date ASC"
        ).fetchall()
    except Exception:
        return {"vix": None, "vix_200dma": None}
    if not rows:
        return {"vix": None, "vix_200dma": None}
    values = [float(r["value"]) for r in rows]
    return {
        "vix": values[-1],
        "vix_200dma": moving_average(values, VIX_MA_WINDOW),
    }


def compute_and_persist_regime(
    conn,
    *,
    asof: Optional[date] = None,
    bars_fetcher: Optional[Callable] = None,
    proxy_symbol: str = DEFAULT_PROXY_SYMBOL,
) -> dict:
    """Compute today's regime score and upsert it to regime_scores.

    `bars_fetcher` is the same daily-bars fetcher the auto-trader uses
    (symbol -> bars). When absent or it fails, ADX is None and the score
    falls back to the VIX-only read. Returns the score dict (the same shape
    as `score_regime`) plus `score_date`.
    """
    asof_d = asof or date.today()
    vix = vix_inputs(conn)
    adx: Optional[float] = None
    if bars_fetcher is not None:
        try:
            bars = bars_fetcher(proxy_symbol)
            adx = compute_adx(bars, period=ADX_PERIOD)
        except Exception as e:
            log(f"regime: ADX fetch for {proxy_symbol} failed ({e})", "WARNING")
    score = score_regime(
        vix=vix["vix"], vix_200dma=vix["vix_200dma"], adx=adx,
    )
    score["score_date"] = asof_d.isoformat()
    try:
        db.upsert_regime_score(
            conn,
            score_date=asof_d.isoformat(),
            regime=score["regime"],
            risk_scale=score["risk_scale"],
            vix=vix["vix"],
            vix_200dma=vix["vix_200dma"],
            adx=adx,
            confidence=score["confidence"],
            detail=score["detail"],
        )
    except Exception as e:
        log(f"regime: persist failed ({e})", "WARNING")
    return score


def latest_regime_score(conn) -> dict:
    """Read the most recent persisted regime score.

    Returns the same shape as `score_regime` (regime / risk_scale /
    confidence / detail). Falls back to the conservative DEFAULT_REGIME when
    the table is empty so callers always get a usable scale.
    """
    try:
        row = db.latest_regime_score(conn)
    except Exception:
        row = None
    if row is None:
        return {
            "regime": DEFAULT_REGIME,
            "risk_scale": RISK_SCALE[DEFAULT_REGIME],
            "confidence": 0.0,
            "vix_below_ma": None,
            "detail": "no regime score persisted; defaulting to transitional",
            "score_date": None,
        }
    regime = str(row["regime"])
    if regime not in KNOWN_REGIMES:
        regime = DEFAULT_REGIME
    return {
        "regime": regime,
        "risk_scale": float(row["risk_scale"]),
        "confidence": (None if row["confidence"] is None
                       else float(row["confidence"])),
        "vix_below_ma": None,
        "detail": row["detail"],
        "score_date": row["score_date"],
    }


if __name__ == "__main__":
    conn = db.init_db()
    try:
        from monitoring.auto_trader import _build_default_bars_fetcher
        fetcher = _build_default_bars_fetcher(lookback_bars=60)
    except Exception:
        fetcher = None
    try:
        score = compute_and_persist_regime(conn, bars_fetcher=fetcher)
        log(f"Regime: {score['detail']}  conf={score['confidence']}", "INFO")
    finally:
        conn.close()
