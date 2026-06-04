"""Trend-following strategy implementations (milestone 4.6.3).

Three classical Turtle-style strategies:
  - donchian_breakout_20   — channel breakout
  - ma_cross_20_50         — fast/slow EMA cross
  - new_high_volume        — 52-week high with volume confirmation

All three declare:
  - pyramidable: True
  - active_in_regimes: ["trending_up", "low_vol", "mixed"]
    (matches regime_router's KNOWN_REGIMES vocabulary — strategies skip
     when current regime is "trending_down" or "choppy")
  - strategy_class: "trend"

They use the same df → df contract as the existing mean-reversion
modules: add boolean columns `long_entry` and `long_exit`, both
.shift(1)-ed so signals depend only on PRIOR bars.

Expected behavior per Phase 4 plan: win rate 30-40%, but the avg
winner is 5-10× the avg loser (classic Turtle profile). Long flat
periods between trends are normal.
"""

from strategies.trend.donchian_breakout_20 import compute_donchian_breakout_20
from strategies.trend.ma_cross_20_50 import compute_ma_cross_20_50
from strategies.trend.new_high_volume import compute_new_high_volume

TREND_DECLARATIONS = [
    {
        "id": "trend-donchian-breakout-20",
        "compute": "compute_donchian_breakout_20",
        "module": "strategies.trend.donchian_breakout_20",
        "strategy_class": "trend",
        "active_in_regimes": ["trending_up", "low_vol", "mixed"],
        "pyramidable": True,
        "grace_period": True,
        "active_on": ["SPY", "QQQ", "IWM"],
        "trailing_stop": {"method": "atr_trail", "multiplier": 2.5, "atr_period": 14},
        # A5 (audit 2026-06-03): bounded model exit. The donchian/MA exit
        # signal fires only on a rare channel/MA breakdown; 153/154 open
        # donchian outcomes had no later exit, so an outcome stayed open even
        # after the position was gone. A time_stop closes the outcome (and
        # flattens the position) when held beyond max_days_held, so a trend
        # outcome can't sit open indefinitely. The ATR trailing stop still
        # wins whenever it trips first (checked before the time-stop pass).
        "time_stop": {"max_days_held": 90},
        # 6.4.2 — observe-only SAR overlay for 30-day A/B. Records a
        # parallel paper_trades_sar_overlay row whenever SAR would have
        # fired; live exit decision is unchanged.
        "sar_overlay": "shadow",
        "intraday_confirm": "shadow",
    },
    {
        "id": "trend-ma-cross-20-50",
        "compute": "compute_ma_cross_20_50",
        "module": "strategies.trend.ma_cross_20_50",
        "strategy_class": "trend",
        "active_in_regimes": ["trending_up", "low_vol", "mixed"],
        "pyramidable": True,
        "grace_period": True,
        "active_on": ["SPY", "QQQ", "IWM"],
        "trailing_stop": {"method": "atr_trail", "multiplier": 2.5, "atr_period": 14},
        "time_stop": {"max_days_held": 90},
        "sar_overlay": "shadow",
        "intraday_confirm": "shadow",
    },
    {
        "id": "trend-new-high-volume",
        "compute": "compute_new_high_volume",
        "module": "strategies.trend.new_high_volume",
        "strategy_class": "trend",
        "active_in_regimes": ["trending_up", "low_vol", "mixed"],
        "pyramidable": True,
        "grace_period": True,
        "active_on": ["SPY", "QQQ", "IWM"],
        "trailing_stop": {"method": "atr_trail", "multiplier": 2.5, "atr_period": 14},
        "time_stop": {"max_days_held": 90},
        "sar_overlay": "shadow",
        "intraday_confirm": "shadow",
    },
]

__all__ = [
    "compute_donchian_breakout_20",
    "compute_ma_cross_20_50",
    "compute_new_high_volume",
    "TREND_DECLARATIONS",
]
