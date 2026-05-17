"""
test_yfinance.py — Verify yfinance data access.
Tests: price history, multiple ETFs.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.utils import log

# Module-level live marker — skip these with `pytest -m "not live"`.
pytestmark = pytest.mark.live

PASS = True


def test_spy_history():
    global PASS
    log("Test 1 — SPY 6-month price history", "INFO")
    try:
        import yfinance as yf
        spy = yf.download("SPY", period="6mo", auto_adjust=True, progress=False)
        if spy.empty:
            raise ValueError("No data returned for SPY")

        first_date = spy.index[0].strftime("%Y-%m-%d")
        last_date = spy.index[-1].strftime("%Y-%m-%d")
        rows = len(spy)
        close_col = spy["Close"]
        # yfinance >= 0.2.x returns multi-level columns for single tickers too
        if hasattr(close_col, "columns"):
            close_col = close_col.iloc[:, 0]
        latest_close = float(close_col.iloc[-1])

        log(f"  First date:    {first_date}", "INFO")
        log(f"  Last date:     {last_date}", "INFO")
        log(f"  Rows:          {rows}", "INFO")
        log(f"  Latest close:  ${latest_close:.2f}", "INFO")
        log("Test 1 PASS — SPY history returned", "SUCCESS")
        return True
    except Exception as e:
        log(f"Test 1 FAIL — {e}", "ERROR")
        PASS = False
        return False


def test_etf_basket():
    global PASS
    log("\nTest 2 — ETF basket (SPY, QQQ, IWM, GLD, TLT) — 3 months", "INFO")
    tickers = ["SPY", "QQQ", "IWM", "GLD", "TLT"]
    try:
        import yfinance as yf
        data = yf.download(tickers, period="3mo", auto_adjust=True, progress=False)
        closes = data["Close"]
        all_ok = True
        for ticker in tickers:
            try:
                latest = float(closes[ticker].dropna().iloc[-1])
                log(f"  {ticker}: ${latest:.2f}", "INFO")
            except Exception as e:
                log(f"  {ticker}: FAIL — {e}", "ERROR")
                all_ok = False
                PASS = False
        if all_ok:
            log("Test 2 PASS — all ETF data returned", "SUCCESS")
        else:
            log("Test 2 PARTIAL FAIL — some tickers missing", "WARNING")
        return all_ok
    except Exception as e:
        log(f"Test 2 FAIL — {e}", "ERROR")
        PASS = False
        return False


def test_reliability_note():
    log("\nTest 3 — Reliability note", "INFO")
    log("  WARNING: yfinance is an unofficial library that scrapes Yahoo Finance.", "WARNING")
    log("  It may break without notice when Yahoo changes its API.", "WARNING")
    log("  For production use, prefer Polygon.io for price data.", "WARNING")
    return True


if __name__ == "__main__":
    log("=== yfinance Tests ===", "INFO")
    r1 = test_spy_history()
    r2 = test_etf_basket()
    r3 = test_reliability_note()
    overall = r1 and r2
    print()
    if overall:
        log("=== yfinance: OVERALL PASS ===", "SUCCESS")
    else:
        log("=== yfinance: OVERALL FAIL ===", "ERROR")
    sys.exit(0 if overall else 1)
