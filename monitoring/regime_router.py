"""regime_router.py — Per-strategy regime gating.

The daily report writes `market_regime` to the `daily_reports` table each
EOD run (one of: trending_up, trending_down, low_vol, choppy, mixed).
This module reads the latest known regime and decides whether a given
strategy is allowed to enter trades in that regime.

Each entry in `monitoring.config.TRACKED_STRATEGIES` may optionally
declare `active_in_regimes=["choppy", "low_vol", "mixed"]`. Strategies
without that key are treated as active in every regime (back-compat
default — opt-in gating).

The auto-trader consults this module between the existing concentration
and max-open-per-strategy checks; a regime mismatch produces a
`SKIP_REGIME_MISMATCH` action without touching Alpaca.
"""

import sys
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402

DEFAULT_REGIME = "mixed"
KNOWN_REGIMES = {
    "trending_up", "trending_down", "low_vol", "choppy", "mixed",
}


def latest_regime(conn) -> str:
    """Return the most recent `market_regime` from `daily_reports`.

    Falls back to `DEFAULT_REGIME` ('mixed') if the table is empty or
    holds NULLs only — that's the most-conservative default since 'mixed'
    is included in any normal active_in_regimes set.
    """
    row = conn.execute(
        "SELECT market_regime FROM daily_reports "
        " WHERE market_regime IS NOT NULL "
        " ORDER BY report_date DESC LIMIT 1"
    ).fetchone()
    if row is None or not row["market_regime"]:
        return DEFAULT_REGIME
    regime = str(row["market_regime"])
    if regime not in KNOWN_REGIMES:
        log(f"regime_router: unknown regime '{regime}' in daily_reports; "
            f"treating as default '{DEFAULT_REGIME}'", "WARNING")
        return DEFAULT_REGIME
    return regime


def _coerce_regimes(raw) -> Optional[Iterable[str]]:
    """Normalize a tracked-strategy's active_in_regimes field.

    Returns:
      None        — undeclared / empty → active in ALL regimes
      frozenset   — declared subset of KNOWN_REGIMES
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        log(f"regime_router: active_in_regimes must be a list, got "
            f"{type(raw).__name__}; treating as undeclared", "WARNING")
        return None
    cleaned = {str(r).strip().lower() for r in raw if str(r).strip()}
    if not cleaned:
        return None
    unknown = cleaned - KNOWN_REGIMES
    if unknown:
        log(f"regime_router: ignoring unknown regimes {sorted(unknown)} "
            f"in active_in_regimes", "WARNING")
        cleaned = cleaned - unknown
    if not cleaned:
        # All declared regimes were unknown; back off to undeclared (safe).
        return None
    return frozenset(cleaned)


def strategy_active_in_regime(strategy_meta: dict, regime: str) -> bool:
    """True when the strategy is permitted to enter in the given regime.

    `strategy_meta` is an entry from TRACKED_STRATEGIES (or any dict that
    may include an `active_in_regimes` field). Undeclared = active in all.
    """
    if not isinstance(strategy_meta, dict):
        return True
    regimes = _coerce_regimes(strategy_meta.get("active_in_regimes"))
    if regimes is None:
        return True
    return regime in regimes


def regime_skip(
    strategy_id: str,
    *,
    regime: str,
    tracked_strategies: Iterable[dict],
) -> Optional[dict]:
    """Return a skip-action dict iff this strategy is gated out of the
    current regime; otherwise None.

    `tracked_strategies` is typically `monitoring.config.TRACKED_STRATEGIES`.
    Strategies not present in the list are NOT skipped — they fall through
    to the auto-trader's other eligibility checks unchanged.
    """
    for meta in tracked_strategies or []:
        if not isinstance(meta, dict):
            continue
        if meta.get("id") != strategy_id:
            continue
        regimes = _coerce_regimes(meta.get("active_in_regimes"))
        if regimes is None:
            return None
        if regime in regimes:
            return None
        return {
            "current_regime": regime,
            "allowed_regimes": sorted(regimes),
            "reason": (
                f"strategy {strategy_id!r} is gated to regimes "
                f"{sorted(regimes)} but current regime is {regime!r}"
            ),
        }
    return None
