"""
test_fred.py — Verify FRED macro data connection.
Tests: authentication and 5 key macro indicators.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.utils import load_credentials, log

PASS = True

INDICATORS = {
    "FEDFUNDS": "Federal Funds Rate",
    "CPIAUCSL": "CPI (inflation)",
    "UNRATE":   "Unemployment rate",
    "T10Y2Y":   "10Y-2Y yield spread (recession indicator)",
    "SP500":    "S&P 500 level (90-day lag on free tier)",
}


def test_fred_auth():
    global PASS
    log("Test 1 — FRED authentication (Federal Funds Rate)", "INFO")
    try:
        from fredapi import Fred
        creds = load_credentials("fred")
        fred = Fred(api_key=creds["api_key"])

        series = fred.get_series("FEDFUNDS")
        latest_date = series.index[-1].strftime("%Y-%m-%d")
        latest_val = series.iloc[-1]

        log(f"  Series:      Federal Funds Rate", "INFO")
        log(f"  Latest date: {latest_date}", "INFO")
        log(f"  Latest val:  {latest_val:.2f}%", "INFO")
        log("Test 1 PASS — FRED auth successful", "SUCCESS")
        return True
    except Exception as e:
        log(f"Test 1 FAIL — {e}", "ERROR")
        PASS = False
        return False


def test_macro_indicators():
    global PASS
    log("\nTest 2 — Macro indicators", "INFO")
    try:
        from fredapi import Fred
        creds = load_credentials("fred")
        fred = Fred(api_key=creds["api_key"])

        all_ok = True
        for series_id, label in INDICATORS.items():
            try:
                series = fred.get_series(series_id)
                if series.empty:
                    log(f"  [{series_id}]: No data returned", "WARNING")
                    continue
                latest_date = series.index[-1].strftime("%Y-%m-%d")
                latest_val = series.iloc[-1]
                log(f"  [{series_id}] {label}: {latest_val} as of {latest_date}", "INFO")
            except Exception as e:
                log(f"  [{series_id}] FAIL — {e}", "ERROR")
                all_ok = False
                PASS = False

        if all_ok:
            log("Test 2 PASS — all macro indicators returned", "SUCCESS")
        else:
            log("Test 2 PARTIAL FAIL — some indicators unavailable", "WARNING")
        return all_ok
    except Exception as e:
        log(f"Test 2 FAIL — {e}", "ERROR")
        PASS = False
        return False


if __name__ == "__main__":
    log("=== FRED Tests ===", "INFO")
    r1 = test_fred_auth()
    r2 = test_macro_indicators()
    overall = r1 and r2
    print()
    if overall:
        log("=== FRED: OVERALL PASS ===", "SUCCESS")
    else:
        log("=== FRED: OVERALL FAIL ===", "ERROR")
    sys.exit(0 if overall else 1)
