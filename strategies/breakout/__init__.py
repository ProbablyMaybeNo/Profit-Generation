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
]

__all__ = ["compute_donchian_retest", "BREAKOUT_DECLARATIONS"]
