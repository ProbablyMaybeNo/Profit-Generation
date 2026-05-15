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
TRACKED_STRATEGIES = [
    {"id": "botnet101-3-bar-low",            "compute": "compute_3bar_low",                  "active_on": ["QQQ", "IWM", "XLE", "KRE", "XHB"]},
    {"id": "botnet101-buy-5day-low",         "compute": "compute_5day_low",                   "active_on": ["XBI", "KRE", "XHB", "GDX"]},
    {"id": "botnet101-consec-bearish",       "compute": "compute_consecutive_bearish",        "active_on": ["IWM", "KRE", "XHB"]},
    {"id": "botnet101-4bar-momentum-reversal","compute": "compute_4bar_momentum_reversal",    "active_on": ["IWM", "XBI", "XME", "GDX"]},
    {"id": "botnet101-consec-below-ema",     "compute": "compute_consecutive_below_ema",      "active_on": ["XOP", "XBI", "KRE", "XME", "GDX"]},
    {"id": "botnet101-turn-around-tuesday",  "compute": "compute_turn_around_tuesday",        "active_on": ["XOP", "XME", "GDX"]},
    {"id": "botnet101-turn-of-month",        "compute": "compute_turn_of_month",              "active_on": ["XME", "GDX"]},
]

# Schedule (Eastern Time). Cron-style trigger times.
PRE_MARKET_RUN = time(9, 0)
POST_CLOSE_RUN = time(16, 30)
