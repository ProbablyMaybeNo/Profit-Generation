"""
signal_ranker.py — Score fired signals so the capacity-capped scanner
picks the best ones (milestone 5.5.4.1).

When the wide-universe scanner fires 30+ entries on a strong trending
day, we can hold ~10 concurrent positions. The ranker assigns a
composite score per signal so the top-N (5.5.4.2) get submitted while
the rest are logged as SKIP_CAPACITY.

Score components (all multiplicative on a base of 1.0):
  - **regime_alignment** — ×1.5 if current market regime matches the
    strategy's active_in_regimes list. ×1.0 otherwise.
  - **volume_confirmation** — ×1.3 if today's volume > 150% of 20-day
    avg volume (we already compute rvol_vs_20d on snapshots; if the
    bar passed in has 20 days of volume history we compute it here).
  - **strategy_edge** — ×1.0 to ×1.5 by all-time Sharpe-ish band:
    Sharpe ≤ 0 → 1.0; 0–0.5 → 1.1; 0.5–1.0 → 1.25; >1.0 → 1.5.
  - **symbol_liquidity** — ×1.0 to ×1.2 by dollar-volume tier:
    < $100M → 1.0; $100M-$500M → 1.1; > $500M → 1.2.

Ties broken alphabetically by symbol.

Returns a copy of the input fire dicts with `score` and `score_breakdown`
keys appended, sorted descending. Pure function — no DB writes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402
from monitoring.regime_router import strategy_active_in_regime  # noqa: E402

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

REGIME_ALIGN_MULT = 1.5
VOLUME_CONFIRM_RATIO = 1.5
VOLUME_CONFIRM_MULT = 1.3

EDGE_BANDS = [
    # (sharpe_threshold, multiplier)
    (1.0, 1.5),
    (0.5, 1.25),
    (0.0, 1.1),
]
EDGE_DEFAULT = 1.0  # Sharpe ≤ 0 or unknown

LIQUIDITY_BANDS = [
    # (min_dollar_volume_usd, multiplier)
    (500_000_000, 1.2),
    (100_000_000, 1.1),
]
LIQUIDITY_DEFAULT = 1.0


# ---------------------------------------------------------------------------
# Component scorers
# ---------------------------------------------------------------------------


def _regime_multiplier(strategy_meta: Optional[dict], regime: str) -> float:
    if not strategy_meta:
        return 1.0
    if strategy_active_in_regime(strategy_meta, regime):
        return REGIME_ALIGN_MULT
    return 1.0


def _volume_multiplier(bars: Optional[pd.DataFrame]) -> float:
    """×1.3 if last-bar volume > 1.5× 20-day average."""
    if bars is None or "volume" not in bars.columns or len(bars) < 21:
        return 1.0
    vol = bars["volume"].astype(float)
    rolling_avg = vol.iloc[-21:-1].mean()  # 20 bars excluding current
    if rolling_avg is None or rolling_avg <= 0:
        return 1.0
    today = float(vol.iloc[-1])
    if today >= VOLUME_CONFIRM_RATIO * rolling_avg:
        return VOLUME_CONFIRM_MULT
    return 1.0


def _edge_multiplier(all_time_sharpe: Optional[float]) -> float:
    if all_time_sharpe is None:
        return EDGE_DEFAULT
    for threshold, mult in EDGE_BANDS:
        if all_time_sharpe > threshold:
            return mult
    return EDGE_DEFAULT


def _liquidity_multiplier(avg_dollar_volume: Optional[float]) -> float:
    if avg_dollar_volume is None or avg_dollar_volume <= 0:
        return LIQUIDITY_DEFAULT
    for threshold, mult in LIQUIDITY_BANDS:
        if avg_dollar_volume >= threshold:
            return mult
    return LIQUIDITY_DEFAULT


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rank_signals(
    fires: Iterable[dict],
    *,
    regime: str = "mixed",
    strategy_decls: Optional[Iterable[dict]] = None,
    bars_by_symbol: Optional[Dict[str, pd.DataFrame]] = None,
    sharpe_by_strategy: Optional[Dict[str, float]] = None,
    dollar_volume_by_symbol: Optional[Dict[str, float]] = None,
) -> List[dict]:
    """Rank scanner fires, return new list sorted DESC by score.

    Each output dict is a shallow copy of the input plus:
      - score (float)
      - score_breakdown (dict of component multipliers)

    Args:
      fires: iterable of fire dicts with at least strategy_id, symbol.
      regime: current market regime (use regime_router.latest_regime()).
      strategy_decls: TRACKED_STRATEGIES for regime lookup.
      bars_by_symbol: optional {symbol: bars_df} for volume confirm.
      sharpe_by_strategy: optional {strategy_id: all_time_sharpe}.
      dollar_volume_by_symbol: optional {symbol: avg_dollar_volume}.
    """
    decls_by_id: Dict[str, dict] = {}
    for d in (strategy_decls or []):
        if isinstance(d, dict) and "id" in d:
            decls_by_id[d["id"]] = d

    bars_by_symbol = bars_by_symbol or {}
    sharpe_by_strategy = sharpe_by_strategy or {}
    dollar_volume_by_symbol = dollar_volume_by_symbol or {}

    out: List[dict] = []
    for fire in fires:
        sid = fire.get("strategy_id")
        symbol = fire.get("symbol")
        meta = decls_by_id.get(sid)

        r_mult = _regime_multiplier(meta, regime)
        v_mult = _volume_multiplier(bars_by_symbol.get(symbol))
        e_mult = _edge_multiplier(sharpe_by_strategy.get(sid))
        l_mult = _liquidity_multiplier(dollar_volume_by_symbol.get(symbol))

        score = r_mult * v_mult * e_mult * l_mult
        ranked = dict(fire)
        ranked["score"] = round(score, 4)
        ranked["score_breakdown"] = {
            "regime": r_mult,
            "volume": v_mult,
            "edge": e_mult,
            "liquidity": l_mult,
        }
        out.append(ranked)

    # Sort: highest score first, ties broken by symbol then strategy_id
    out.sort(
        key=lambda r: (
            -float(r.get("score", 0.0)),
            str(r.get("symbol") or ""),
            str(r.get("strategy_id") or ""),
        )
    )
    return out


# ---------------------------------------------------------------------------
# Convenience: pre-fill sharpe + liquidity dicts from db
# ---------------------------------------------------------------------------


def sharpe_lookup_from_db(
    strategy_ids: Iterable[str], conn=None,
) -> Dict[str, float]:
    """Fetch all-time Sharpe-ish per strategy from outcomes table."""
    from monitoring.strategy_health import (
        _closed_returns_for_strategy, _sharpe_ish,
    )
    ids = list({s for s in strategy_ids if s})
    if not ids:
        return {}
    own_conn = conn is None
    if own_conn:
        conn = db.init_db()
    try:
        out: Dict[str, float] = {}
        for sid in ids:
            rets = _closed_returns_for_strategy(conn, sid)
            if len(rets) < 2:
                continue
            out[sid] = _sharpe_ish(rets)
        return out
    finally:
        if own_conn:
            conn.close()


def dollar_volume_lookup_from_db(
    symbols: Iterable[str], conn=None,
) -> Dict[str, float]:
    """Fetch avg_dollar_volume_20d per symbol from liquidity_snapshots."""
    syms = [s.upper() for s in symbols if s]
    if not syms:
        return {}
    own_conn = conn is None
    if own_conn:
        conn = db.init_db()
    try:
        snaps = db.get_liquidity_snapshots(conn, syms)
        return {
            sym: snap["avg_dollar_volume_20d"]
            for sym, snap in snaps.items()
            if snap.get("avg_dollar_volume_20d") is not None
        }
    finally:
        if own_conn:
            conn.close()
