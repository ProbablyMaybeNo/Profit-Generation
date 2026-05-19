"""Breakout strategy implementations (Phase 6.3).

This module hosts breakout-and-retest strategies — strategies that
wait for a breakout AND a retest of the broken level before entering.
The retest entry gives up some hit-rate vs trend-following breakouts
(which buy on the breakout itself) in exchange for a much tighter
stop and higher R:R.

Strategies declare:
  - strategy_class: "breakout"
  - pyramidable: False (the retest IS the entry — no add-on tiers)
  - active_in_regimes: matched to trending markets only

See `donchian_retest.py` for the long-side implementation (6.3.1).
"""
from strategies.breakout.donchian_retest import compute_donchian_retest
from strategies.breakout.donchian_retest_short import (
    compute_donchian_retest_short,
)

BREAKOUT_DECLARATIONS = [
    {
        "id": "breakout-donchian-retest-20",
        "compute": "compute_donchian_retest",
        "module": "strategies.breakout.donchian_retest",
        "strategy_class": "breakout",
        # Project vocabulary maps "bull / trend" intent to:
        #   trending_up — clear uptrend
        #   mixed       — neutral / unclassified (better to enter than skip)
        # We exclude trending_down (bear) and choppy / low_vol (no-trend).
        "active_in_regimes": ["trending_up", "mixed"],
        "pyramidable": False,
        "grace_period": True,
        "active_on": ["SPY", "QQQ", "IWM"],
        # Initial stop: tight ATR(14) stop with k=1.0 per 6.3.1.
        # Routes through the 6.1.1 atr_initial mechanism via per_strategy
        # override (see config/settings.json stops.per_strategy).
        "initial_stop": {"method": "atr_initial", "multiplier": 1.0,
                         "atr_period": 14},
    },
    {
        # 6.3.2 — Mirror of 6.3.1 for the short side. Active only in
        # trending_down (bear) and mixed regimes. Borrow costs +
        # unlimited-loss exposure mean we cap position size at 50% of
        # the long equivalent and never pyramid.
        "id": "breakout-donchian-retest-short-20",
        "compute": "compute_donchian_retest_short",
        "module": "strategies.breakout.donchian_retest_short",
        "strategy_class": "breakout",
        # "bear / trend" intent — project vocab maps to trending_down
        # (clear downtrend) plus mixed (no-signal). Excludes
        # trending_up / choppy / low_vol.
        "active_in_regimes": ["trending_down", "mixed"],
        "pyramidable": False,
        "grace_period": True,
        "active_on": ["SPY", "QQQ", "IWM"],
        "side": "short",
        "initial_stop": {"method": "atr_initial", "multiplier": 1.0,
                         "atr_period": 14},
        # Risk caveat: 50% of the long-side equivalent. Auto-trader's
        # short routing path reads `max_position_usd_multiplier` if
        # present and applies it after the regular sizing chain.
        "max_position_usd_multiplier": 0.5,
    },
]

__all__ = [
    "compute_donchian_retest",
    "compute_donchian_retest_short",
    "BREAKOUT_DECLARATIONS",
]
