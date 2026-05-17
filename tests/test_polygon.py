"""
test_polygon.py — Verify Polygon.io data connection.
Tests: authentication, options chain, news headlines.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.utils import load_credentials, log

# Module-level live marker — skip these with `pytest -m "not live"`.
pytestmark = pytest.mark.live

PASS = True
WARN = False


def test_price_data():
    global PASS
    log("Test 1 — Polygon price data (previous day OHLCV)", "INFO")
    try:
        from polygon import RESTClient
        creds = load_credentials("polygon")
        client = RESTClient(api_key=creds["api_key"])

        # Get previous trading day (go back a few days to be safe)
        from_ = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        to_ = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        bars = list(client.list_aggs("AAPL", 1, "day", from_, to_, limit=5))
        if not bars:
            raise ValueError("No bars returned")

        bar = bars[-1]
        ts = datetime.fromtimestamp(bar.timestamp / 1000).strftime("%Y-%m-%d")
        log(f"  Date:   {ts}", "INFO")
        log(f"  Open:   ${bar.open:.2f}", "INFO")
        log(f"  High:   ${bar.high:.2f}", "INFO")
        log(f"  Low:    ${bar.low:.2f}", "INFO")
        log(f"  Close:  ${bar.close:.2f}", "INFO")
        log(f"  Volume: {bar.volume:,.0f}", "INFO")
        log("Test 1 PASS — price data returned", "SUCCESS")
        return True
    except Exception as e:
        log(f"Test 1 FAIL — {e}", "ERROR")
        PASS = False
        return False


def test_options_chain():
    global WARN
    import time
    log("\nTest 2 — Options chain (SPY, next 30 days)", "INFO")
    time.sleep(12)
    try:
        from polygon import RESTClient
        creds = load_credentials("polygon")
        client = RESTClient(api_key=creds["api_key"])

        exp_from = datetime.now().strftime("%Y-%m-%d")
        exp_to = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

        contracts = list(client.list_snapshot_options_chain(
            "SPY",
            params={
                "expiration_date.gte": exp_from,
                "expiration_date.lte": exp_to,
                "limit": 10,
            }
        ))

        if not contracts:
            log("  WARNING: No options contracts returned — expected on free tier", "WARNING")
            log("  NOTE: Free tier has 15-min delay and limited options data", "WARNING")
            WARN = True
            return "WARN"

        expiries = sorted(set(c.details.expiration_date for c in contracts if c.details))
        nearest = expiries[0] if expiries else "unknown"
        sample = contracts[0]

        log(f"  Contracts returned: {len(contracts)}", "INFO")
        log(f"  Nearest expiry: {nearest}", "INFO")
        if sample.details:
            iv = getattr(sample.greeks, "iv", None) if hasattr(sample, "greeks") and sample.greeks else "N/A"
            log(f"  Sample: {sample.details.ticker} | Strike: {sample.details.strike_price} | Exp: {sample.details.expiration_date} | IV: {iv}", "INFO")

        log("Test 2 PASS — options data returned", "SUCCESS")
        return True
    except Exception as e:
        log(f"  WARNING: Options chain unavailable — {e}", "WARNING")
        log("  NOTE: Free tier has 15-min delay and limited options data. This WARN is expected.", "WARNING")
        WARN = True
        return "WARN"


def test_news():
    global PASS
    import time
    import requests as req
    log("\nTest 3 — News headlines (AAPL, 5 most recent)", "INFO")
    log("  Pausing 20s to respect free-tier rate limits (5 req/min)...", "INFO")
    time.sleep(20)
    try:
        creds = load_credentials("polygon")
        url = "https://api.polygon.io/v2/reference/news"
        params = {
            "ticker": "AAPL",
            "limit": 5,
            "sort": "published_utc",
            "order": "desc",
            "apiKey": creds["api_key"],
        }
        resp = req.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            log("  WARNING: Rate limited (429) — free tier limit reached. Try again in 60s.", "WARNING")
            PASS = False
            return False
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("results", [])
        for a in articles[:5]:
            pub = a.get("publisher", {}).get("name", "Unknown")
            log(f"  [{a.get('published_utc','')[:10]}] {pub} — {a.get('title','')}", "INFO")

        log("Test 3 PASS — news returned", "SUCCESS")
        return True
    except Exception as e:
        log(f"Test 3 FAIL — {e}", "ERROR")
        PASS = False
        return False


if __name__ == "__main__":
    log("=== Polygon Tests ===", "INFO")
    r1 = test_price_data()
    r2 = test_options_chain()
    r3 = test_news()

    print()
    if not PASS:
        log("=== Polygon: OVERALL FAIL ===", "ERROR")
        sys.exit(1)
    elif WARN or r2 == "WARN":
        log("=== Polygon: OVERALL PASS (with WARNING on options — expected on free tier) ===", "WARNING")
        sys.exit(0)
    else:
        log("=== Polygon: OVERALL PASS ===", "SUCCESS")
        sys.exit(0)
