"""
test_flatten_unintended_shorts.py — Sprint 2 / M2 unintended-short cover tool.

Proves:
  - dry-run lists every qty<0 position with the correct cover qty (abs(qty))
    and places NO orders.
  - --execute submits EXACT buy-to-cover quantities for shorts and nothing for
    long/flat symbols (mocked broker).
  - long-only invariant: cover qty never exceeds abs(short) (no flip to long).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import flatten_unintended_shorts as fus  # noqa: E402


class FakePos:
    def __init__(self, symbol, qty, qty_available=0):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty_available


class FakeBroker:
    def __init__(self, positions):
        self._positions = {p.symbol: p for p in positions}
        self.submitted = []

    def get_all_positions(self):
        return list(self._positions.values())

    def get_open_position(self, symbol):
        if symbol not in self._positions:
            raise Exception("position does not exist")
        return self._positions[symbol]

    def get_orders(self, filter=None):
        return []

    def cancel_order_by_id(self, oid):
        pass


class FakeOrder:
    def __init__(self, symbol, qty, side):
        self.id = f"cover-{symbol}"
        self.symbol = symbol
        self.qty = qty
        self.side = side


def _submit(client, *, symbol, qty, side):
    client.submitted.append({"symbol": symbol, "qty": qty, "side": side})
    return FakeOrder(symbol, qty, side)


def _broker():
    return FakeBroker([
        FakePos("AAPL", -100),    # short → cover 100
        FakePos("META", -40),     # short → cover 40
        FakePos("MSFT", 200),     # long  → untouched
        FakePos("XLE", 0),        # flat  → untouched (and qty 0)
    ])


def test_detect_shorts_lists_only_negatives_with_cover_qty():
    shorts = fus.detect_shorts(_broker())
    assert shorts == [
        {"symbol": "AAPL", "qty": -100.0, "cover_qty": 100},
        {"symbol": "META", "qty": -40.0, "cover_qty": 40},
    ]


def test_dry_run_lists_without_placing_orders():
    broker = _broker()
    res = fus.flatten_unintended_shorts(client=broker, execute=False,
                                        submit_fn=_submit)
    assert res["dry_run"] is True
    assert {s["symbol"] for s in res["shorts"]} == {"AAPL", "META"}
    assert res["covered"] == []
    assert broker.submitted == []  # critical: nothing placed in dry-run


def test_execute_covers_exact_qty_for_shorts_only():
    broker = _broker()
    res = fus.flatten_unintended_shorts(client=broker, execute=True,
                                        submit_fn=_submit)
    assert res["dry_run"] is False
    # Exactly the two shorts covered, exact quantities, all BUY.
    assert broker.submitted == [
        {"symbol": "AAPL", "qty": 100, "side": "buy"},
        {"symbol": "META", "qty": 40, "side": "buy"},
    ]
    covered = {c["symbol"]: c["qty"] for c in res["covered"]}
    assert covered == {"AAPL": 100, "META": 40}


def test_execute_noop_when_no_shorts():
    broker = FakeBroker([FakePos("MSFT", 200), FakePos("SPY", 50)])
    res = fus.flatten_unintended_shorts(client=broker, execute=True,
                                        submit_fn=_submit)
    assert res["shorts"] == []
    assert broker.submitted == []
