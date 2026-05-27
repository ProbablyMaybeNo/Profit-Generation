"""
config.py — Tracked tickers, Notion DB IDs, schedule.
"""

from datetime import time

# Stock universe — what we scan for gappers and run strategy fires against
TRACKED_STOCKS = ["SPY", "QQQ", "IWM"]
TRACKED_SECTORS = ["XLE", "XOP", "XBI", "KRE", "XME", "GDX", "XHB"]

# Crypto — 24/7 markets, treated separately
TRACKED_CRYPTO = ["BTC-USD", "ETH-USD", "SOL-USD"]

ALL_TRACKED = TRACKED_STOCKS + TRACKED_SECTORS + TRACKED_CRYPTO

# Notion identifiers (created 2026-04-26 via MCP).
# The REST /pages endpoint takes the database_id; data_source_id is preserved
# here only because the MCP tool used it.
NOTION_DAILY_REPORTS_DB_ID = "38b8012b-9278-4d30-8806-e0f4ce92624e"
NOTION_DAILY_REPORTS_DS    = "fad83551-4866-4cc0-b78e-8c3bf9dd87cd"
NOTION_PATTERNS_DB_ID      = "a5013bd6-7c26-48a5-8029-ac101b9801bf"
NOTION_PATTERNS_DS         = "5b0d18f3-d7cc-4af0-906c-26dc429a1ee4"
NOTION_PARENT_PAGE_ID      = "24ac5770777180bda375eb9ae8e53194"

# Tracked strategies that have a non-FAIL verdict — these get fire-checked daily
from strategies.trend import TREND_DECLARATIONS
from strategies.breakout import BREAKOUT_DECLARATIONS

# 5.3.1 — Promote mean_reversion_intraday (3-bar-low port) to TRACKED_STRATEGIES.
# Fires on 15-min bars during market hours via monitoring.intraday_fires.
# Grace period: yes (no prior intraday outcomes). Not pyramidable —
# mean-reversion entries are single-shot.
INTRADAY_MR_DECLARATIONS = [
    {
        "id": "intraday-mr-3bar-low-15m",
        "compute": "compute_3bar_low_intraday",
        "module": "strategies.intraday.mean_reversion_intraday",
        "strategy_class": "mean_reversion",
        "bar_interval": "15m",
        "active_on": ["SPY", "QQQ", "IWM"],
        "grace_period": True,
        "pyramidable": False,
    },
]

# 5.3.2 — Opening-Range Breakout (ORBO). Fires on 5-min bars during the
# opening hour only — first breakout above the 09:30-09:50 ET range
# triggers a single long entry; EOD or stop closes it. Grace period
# enabled (no prior intraday outcomes). Not pyramidable — single-shot per day.
INTRADAY_ORB_DECLARATIONS = [
    {
        "id": "intraday-orbo-5m",
        "compute": "compute_orbo_intraday",
        "module": "strategies.orb.orbo_intraday",
        "strategy_class": "breakout",
        "bar_interval": "5m",
        "active_on": ["SPY", "QQQ", "IWM", "NVDA", "TSLA"],
        "active_in_window": ["09:35-10:30 ET"],
        "grace_period": True,
        "pyramidable": False,
    },
    # 5.3.3 — Opening-Range Breakout with classic floor-pivot R1
    # confirmation. Same 5m / opening-hour window as ORBO but only enters
    # when R1 confirms breakout strength (R1 > or_high AND bar.open < or_high).
    # Lower fire rate, higher quality. Initial stop = prior-day low.
    {
        "id": "intraday-orb-pivots-5m",
        "compute": "compute_orb_pivots_intraday",
        "module": "strategies.orb.orb_pivots_intraday",
        "strategy_class": "breakout",
        "bar_interval": "5m",
        "active_on": ["SPY", "QQQ", "IWM", "NVDA", "TSLA"],
        "active_in_window": ["09:35-10:30 ET"],
        "grace_period": True,
        "pyramidable": False,
    },
]

# 7.5.5 — Three 1-minute-native strategies that read from the
# intraday_bars table (7.5.1 ingestion). All three run alongside
# existing strategies via the standard auto_trader paper path;
# max_position_usd capped at 20% of normal ($200) while the new
# strategies prove themselves.
#
# 7.6 — Universe expanded from [SPY, QQQ, IWM] to 20 liquid names.
# Strategies fetch bars via Alpaca REST (load_intraday_bars), not via
# the IEX WebSocket — so universe expansion does not increase IEX
# bandwidth pressure (TRACKED_STOCKS stays at 3). Cap math: 20 names
# × 3 strategies × $200 = $12K maximum simultaneous exposure, leaves
# $88K for EOD swing entries.
INTRADAY_1M_UNIVERSE = [
    # Index ETFs (broad-market reflexivity)
    "SPY", "QQQ", "IWM",
    # Sector ETFs (correlated baskets, clean opens)
    "XLK", "SMH", "XLE", "XBI", "KRE", "GDX",
    # Large-cap tech (liquid, volatile, clean breakouts)
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    # Semis individual (high beta, clean momentum)
    "AMD", "AVGO",
    # High-vol growth
    "NFLX", "COIN",
]

INTRADAY_1M_DECLARATIONS = [
    {
        "id": "intraday-1m-orb",
        "compute": "compute_intraday_1m_orb",
        "module": "strategies.intraday.orb_1m",
        "strategy_class": "breakout",
        "bar_interval": "1m",
        "active_on": list(INTRADAY_1M_UNIVERSE),
        "active_in_window": ["09:35-15:55 ET"],
        "grace_period": True,
        "pyramidable": False,
        "max_position_usd": 200,
    },
    {
        "id": "intraday-1m-momentum",
        "compute": "compute_intraday_1m_momentum",
        "module": "strategies.intraday.momentum_1m",
        "strategy_class": "momentum",
        "bar_interval": "1m",
        "active_on": list(INTRADAY_1M_UNIVERSE),
        "grace_period": True,
        "pyramidable": False,
        "max_position_usd": 200,
    },
    {
        "id": "intraday-1m-vwap-reclaim",
        "compute": "compute_intraday_1m_vwap_reclaim",
        "module": "strategies.intraday.vwap_reclaim_1m",
        "strategy_class": "mean_reversion",
        "bar_interval": "1m",
        "active_on": list(INTRADAY_1M_UNIVERSE),
        "grace_period": True,
        "pyramidable": False,
        "max_position_usd": 200,
    },
]

# 6.1.2 — All legacy botnet101 strategies are mean-reversion (3-bar-low,
# 5-day-low, consec-bearish, 4-bar reversal, consec-below-EMA) or calendar-
# effect mean-reversion (turn-of-month, turn-around-tuesday). Declaring
# strategy_class="mean_reversion" lets the auto-trader apply MR-specific
# stop policy (tighter k=2.0 ATR multiplier per 6.1.2).
TRACKED_STRATEGIES = [
    {"id": "botnet101-3-bar-low",            "compute": "compute_3bar_low",                  "strategy_class": "mean_reversion", "active_on": ["QQQ", "IWM", "XLE", "KRE", "XHB"]},
    {"id": "botnet101-buy-5day-low",         "compute": "compute_5day_low",                   "strategy_class": "mean_reversion", "active_on": ["XBI", "KRE", "XHB", "GDX"]},
    {"id": "botnet101-consec-bearish",       "compute": "compute_consecutive_bearish",        "strategy_class": "mean_reversion", "active_on": ["IWM", "KRE", "XHB"]},
    {"id": "botnet101-4bar-momentum-reversal","compute": "compute_4bar_momentum_reversal",    "strategy_class": "mean_reversion", "active_on": ["IWM", "XBI", "XME", "GDX"]},
    {"id": "botnet101-consec-below-ema",     "compute": "compute_consecutive_below_ema",      "strategy_class": "mean_reversion", "active_on": ["XOP", "XBI", "KRE", "XME", "GDX"]},
    {"id": "botnet101-turn-around-tuesday",  "compute": "compute_turn_around_tuesday",        "strategy_class": "mean_reversion", "active_on": ["XOP", "XME", "GDX"]},
    {"id": "botnet101-turn-of-month",        "compute": "compute_turn_of_month",              "strategy_class": "mean_reversion", "active_on": ["XME", "GDX"]},
    *TREND_DECLARATIONS,
    *INTRADAY_MR_DECLARATIONS,
    *INTRADAY_ORB_DECLARATIONS,
    *INTRADAY_1M_DECLARATIONS,
    *BREAKOUT_DECLARATIONS,
]

# Schedule (Eastern Time). Cron-style trigger times.
PRE_MARKET_RUN = time(9, 0)
POST_CLOSE_RUN = time(16, 30)
