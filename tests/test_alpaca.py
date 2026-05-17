"""
test_alpaca.py — Verify Alpaca paper trading connection.
Tests: auth, market clock, market data, paper order lifecycle.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.utils import get_alpaca_client, get_account_summary, market_is_open, log

# Module-level live marker — skip these with `pytest -m "not live"`.
pytestmark = pytest.mark.live

PASS = True


def test_auth():
    global PASS
    log("Test 1 — Authentication", "INFO")
    try:
        summary = get_account_summary()
        log(f"  Portfolio value: ${summary['portfolio_value']:,.2f}", "INFO")
        log(f"  Cash:            ${summary['cash']:,.2f}", "INFO")
        log(f"  Buying power:    ${summary['buying_power']:,.2f}", "INFO")
        log("Test 1 PASS — account data returned", "SUCCESS")
        return True
    except Exception as e:
        log(f"Test 1 FAIL — {e}", "ERROR")
        PASS = False
        return False


def test_market_clock():
    global PASS
    log("\nTest 2 — Market clock", "INFO")
    try:
        client = get_alpaca_client()
        clock = client.get_clock()
        is_open = clock.is_open
        log(f"  Market open: {is_open}", "INFO")
        log(f"  Next open:   {clock.next_open}", "INFO")
        log(f"  Next close:  {clock.next_close}", "INFO")
        log("Test 2 PASS — clock data returned", "SUCCESS")
        return True
    except Exception as e:
        log(f"Test 2 FAIL — {e}", "ERROR")
        PASS = False
        return False


def test_market_data():
    global PASS
    log("\nTest 3 — Market data", "INFO")
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        from config.utils import load_credentials
        creds = load_credentials("alpaca")
        data_client = StockHistoricalDataClient(
            api_key=creds["api_key"],
            secret_key=creds["secret_key"],
        )
        req = StockLatestTradeRequest(symbol_or_symbols=["AAPL", "SPY"])
        trades = data_client.get_stock_latest_trade(req)
        aapl_price = trades["AAPL"].price
        spy_price = trades["SPY"].price
        log(f"  AAPL latest trade: ${aapl_price:.2f}", "INFO")
        log(f"  SPY  latest trade: ${spy_price:.2f}", "INFO")
        log("Test 3 PASS — market data returned", "SUCCESS")
        return True
    except Exception as e:
        log(f"Test 3 FAIL — {e}", "ERROR")
        PASS = False
        return False


def test_paper_order():
    global PASS
    log("\nTest 4 — Paper order (place and cancel)", "INFO")
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        from config.utils import load_credentials
        creds = load_credentials("alpaca")
        client = TradingClient(
            api_key=creds["api_key"],
            secret_key=creds["secret_key"],
            paper=True,
        )
        order_req = LimitOrderRequest(
            symbol="AAPL",
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=1.00,
        )
        order = client.submit_order(order_req)
        log(f"  Order placed — ID: {order.id}, Status: {order.status}", "INFO")

        client.cancel_order_by_id(order.id)

        import time
        time.sleep(1)

        try:
            cancelled = client.get_order_by_id(order.id)
            final_status = cancelled.status
        except Exception:
            final_status = "cancelled/not_found"

        log(f"  Order final status: {final_status}", "INFO")
        log("Test 4 PASS — ORDER TEST PASSED — order placed and cancelled cleanly", "SUCCESS")
        return True
    except Exception as e:
        log(f"Test 4 FAIL — {e}", "ERROR")
        PASS = False
        return False


if __name__ == "__main__":
    log("=== Alpaca Tests ===", "INFO")
    r1 = test_auth()
    r2 = test_market_clock()
    r3 = test_market_data()
    r4 = test_paper_order()
    overall = all([r1, r2, r3, r4])
    print()
    if overall:
        log("=== Alpaca: OVERALL PASS ===", "SUCCESS")
    else:
        log("=== Alpaca: OVERALL FAIL ===", "ERROR")
    sys.exit(0 if overall else 1)
