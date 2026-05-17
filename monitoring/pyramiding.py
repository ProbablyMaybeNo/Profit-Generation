"""
pyramiding.py — Add-on entry logic for trend-following strategies
(milestone 4.6.2).

When a position is already open and a NEW confirming signal fires
from the same strategy + same direction AND the regime is still
trend-aligned, submit an *add-on* order sized by a tier schedule
(default `[1.0, 0.5, 0.25, 0.125]` — initial entry full size, then
each add-on halving the previous tier).

Wiring contract (consumed by auto_trader):

  is_pyramidable(strategy_id, declarations) -> bool
    Mean-reversion strategies must NOT be pyramidable. The
    `pyramidable: true` declaration in TRACKED_STRATEGIES (or the
    strategy record's `extra.pyramidable`) gates this.

  current_tier(conn, strategy_id, symbol) -> int
    Counts existing add-ons. Tier 0 == initial entry, tier 1+ ==
    pyramids.

  next_addon_size(*, initial_qty, current_tier, tier_schedule,
                   max_tiers) -> Optional[int]
    Returns the add-on share count, or None when the next tier
    exceeds max_tiers OR the schedule is exhausted.

  regime_allows_addon(regime, strategy_direction) -> bool
    Veto add-ons when the regime classifier has rotated away from
    the strategy's direction.

  record_addon_tier(conn, paper_trade_id, tier) -> None
    Writes the tier to paper_trades.pyramid_tier.

Schema: paper_trades gains a `pyramid_tier INTEGER` column. Existing
rows default to NULL (tier 0 = initial entry). Schema version 4.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional, Sequence

DEFAULT_TIER_SCHEDULE: Sequence[float] = (1.0, 0.5, 0.25, 0.125)
DEFAULT_MAX_TIERS = 4

# Direction-friendly regime mapping. Trend strategies should NOT
# add on during chop/correction; mean-reversion ones should not
# pyramid at all.
TREND_FRIENDLY_REGIMES = {"bull", "trend"}
MEAN_REVERSION_REGIMES = {"chop", "mean_reversion", "range"}


# ---------------------------------------------------------------------------
# Declarations + regime gate
# ---------------------------------------------------------------------------

def is_pyramidable(declaration: Optional[Dict]) -> bool:
    """True iff the strategy's TRACKED_STRATEGIES entry (or its record's
    `extra` block) sets `pyramidable: true`. Conservative default:
    False — opt-in only."""
    if not declaration:
        return False
    return bool(declaration.get("pyramidable", False))


def regime_allows_addon(regime: Optional[str], *,
                         direction: str = "long",
                         strategy_class: str = "trend") -> bool:
    """Veto add-ons when the regime is hostile.

    Trend strategies need a friendly trend regime. Mean-reversion
    strategies should never pyramid regardless of regime.
    """
    if strategy_class != "trend":
        return False
    r = (regime or "").lower()
    return r in TREND_FRIENDLY_REGIMES


# ---------------------------------------------------------------------------
# Tier math
# ---------------------------------------------------------------------------

def next_addon_size(
    *,
    initial_qty: int,
    current_tier: int,
    tier_schedule: Sequence[float] = DEFAULT_TIER_SCHEDULE,
    max_tiers: int = DEFAULT_MAX_TIERS,
) -> Optional[int]:
    """Return the add-on share count for the NEXT tier (current_tier + 1),
    or None when:
      - current_tier + 1 >= max_tiers
      - current_tier + 1 >= len(tier_schedule)
      - the resulting share count rounds to 0

    Tier 0 = initial entry → never sizes via this function. The first
    pyramid is tier 1.
    """
    next_t = current_tier + 1
    if next_t >= max_tiers:
        return None
    if next_t >= len(tier_schedule):
        return None
    multiplier = float(tier_schedule[next_t])
    qty = int(round(initial_qty * multiplier))
    if qty <= 0:
        return None
    return qty


def tier_progression(
    *,
    initial_qty: int,
    tier_schedule: Sequence[float] = DEFAULT_TIER_SCHEDULE,
    max_tiers: int = DEFAULT_MAX_TIERS,
) -> List[int]:
    """Inspectable expansion: list of per-tier share counts. Useful for
    diagnostics + tests."""
    out: List[int] = []
    for t in range(min(max_tiers, len(tier_schedule))):
        out.append(int(round(initial_qty * float(tier_schedule[t]))))
    return out


# ---------------------------------------------------------------------------
# DB seam — current_tier + record_addon_tier
# ---------------------------------------------------------------------------

def current_tier(
    conn: sqlite3.Connection, *,
    strategy_id: str, symbol: str,
) -> int:
    """Highest pyramid_tier among OPEN paper_trades buys for the
    (strategy_id, symbol) pair. Returns 0 when only the initial entry
    is open (pyramid_tier=NULL maps to tier 0)."""
    row = conn.execute(
        "SELECT MAX(COALESCE(pyramid_tier, 0)) AS t "
        "  FROM paper_trades "
        " WHERE strategy_id=? AND symbol=? AND side='buy' "
        "   AND status IN ('filled', 'partially_filled', 'accepted', 'new')",
        (strategy_id, symbol),
    ).fetchone()
    if row is None or row["t"] is None:
        return 0
    return int(row["t"])


def record_addon_tier(
    conn: sqlite3.Connection, *,
    paper_trade_id: int, tier: int,
) -> bool:
    """Write the tier on an existing paper_trades row. Returns True iff
    a row was updated."""
    with conn:
        cur = conn.execute(
            "UPDATE paper_trades SET pyramid_tier=? WHERE id=?",
            (int(tier), int(paper_trade_id)),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Eligibility — the full chain the auto-trader calls per signal
# ---------------------------------------------------------------------------

def evaluate_addon(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    symbol: str,
    initial_qty: int,
    regime: Optional[str],
    declaration: Optional[Dict],
    direction: str = "long",
    tier_schedule: Sequence[float] = DEFAULT_TIER_SCHEDULE,
    max_tiers: int = DEFAULT_MAX_TIERS,
    strategy_class: str = "trend",
) -> Dict:
    """End-to-end eligibility + sizing for the NEXT add-on.

    Shape:
      {action, tier, qty, reason}

      action ∈ {"ADDON", "VETO_NOT_PYRAMIDABLE", "VETO_REGIME",
                 "VETO_MAX_TIERS"}
    """
    if not is_pyramidable(declaration):
        return {"action": "VETO_NOT_PYRAMIDABLE",
                "tier": None, "qty": 0,
                "reason": "strategy declaration has no pyramidable=true"}
    if not regime_allows_addon(regime, direction=direction,
                                strategy_class=strategy_class):
        return {"action": "VETO_REGIME", "tier": None, "qty": 0,
                "reason": f"regime {regime!r} is not in "
                           f"{sorted(TREND_FRIENDLY_REGIMES)}"}
    tier = current_tier(conn, strategy_id=strategy_id, symbol=symbol)
    qty = next_addon_size(
        initial_qty=initial_qty, current_tier=tier,
        tier_schedule=tier_schedule, max_tiers=max_tiers,
    )
    if qty is None:
        return {"action": "VETO_MAX_TIERS",
                "tier": tier, "qty": 0,
                "reason": f"tier {tier+1} exceeds max_tiers={max_tiers} "
                          f"(or schedule exhausted)"}
    return {"action": "ADDON",
            "tier": tier + 1, "qty": qty,
            "reason": f"add-on tier {tier+1} = {qty} shares"}
