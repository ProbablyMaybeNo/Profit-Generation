"""Tests for monitoring.trailing_stops (milestone 4.6.1).

Three-formula engine with ratchet semantics + persistence.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import trailing_stops as ts  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _bars(*highs_lows_closes):
    """Compact OHLC factory. Pass (high, low, close) triples."""
    out = []
    for h, l, c in highs_lows_closes:
        out.append({"high": h, "low": l, "close": c})
    return out


def _flat_bars(n: int, value: float = 100.0):
    return _bars(*((value + 0.5, value - 0.5, value) for _ in range(n)))


# ---------------------------------------------------------------------------
# atr_trail formula
# ---------------------------------------------------------------------------

def test_atr_trail_needs_enough_bars():
    # period=14 requires 15+ bars. 10 bars → None.
    out = ts.compute_atr_trail_stop(
        _flat_bars(10), entry_price=100.0,
    )
    assert out is None


def test_atr_trail_long_subtracts_atr_from_hh():
    bars = _flat_bars(20)  # ATR == 1.0 (range = 1.0 each bar)
    out = ts.compute_atr_trail_stop(
        bars, entry_price=100.0, multiplier=3.0,
    )
    assert out is not None
    # HH = 100.5, ATR = 1.0, stop = 100.5 - 3*1.0 = 97.5
    assert out["extreme_price"] == pytest.approx(100.5)
    assert out["stop_price"] == pytest.approx(97.5, abs=0.05)


def test_atr_trail_short_adds_atr_to_ll():
    bars = _flat_bars(20)
    out = ts.compute_atr_trail_stop(
        bars, entry_price=100.0, multiplier=3.0, side="short",
    )
    assert out is not None
    # LL = 99.5, stop = 99.5 + 3*1.0 = 102.5
    assert out["extreme_price"] == pytest.approx(99.5)
    assert out["stop_price"] == pytest.approx(102.5, abs=0.05)


def test_atr_trail_zero_atr_returns_none():
    # All bars identical → ATR = 0
    bars = _bars(*((100.0, 100.0, 100.0) for _ in range(20)))
    out = ts.compute_atr_trail_stop(bars, entry_price=100.0)
    assert out is None


# ---------------------------------------------------------------------------
# chandelier formula
# ---------------------------------------------------------------------------

def test_chandelier_uses_fixed_lookback():
    """Stops at HH over last `lookback` bars regardless of position age."""
    # 30 bars: bars 0-9 cluster around 100, bars 10-29 cluster around 110.
    bars = (
        _bars(*((100.5, 99.5, 100.0) for _ in range(10)))
        + _bars(*((110.5, 109.5, 110.0) for _ in range(20)))
    )
    out = ts.compute_chandelier_stop(
        bars, lookback=22, multiplier=3.0, period=22,
    )
    assert out is not None
    # HH over last 22 bars = 110.5 (the recent cluster dominates)
    assert out["extreme_price"] == pytest.approx(110.5)


def test_chandelier_insufficient_bars_returns_none():
    bars = _flat_bars(15)  # < lookback (22)
    out = ts.compute_chandelier_stop(bars, lookback=22, period=22)
    assert out is None


# ---------------------------------------------------------------------------
# percent_trail formula
# ---------------------------------------------------------------------------

def test_percent_trail_long_simple_math():
    bars = _bars((100.0, 95.0, 99.0), (105.0, 100.0, 104.0),
                  (102.0, 101.0, 102.0))
    out = ts.compute_percent_trail_stop(bars, pct=0.10)
    assert out["extreme_price"] == pytest.approx(105.0)
    assert out["stop_price"] == pytest.approx(105.0 * 0.9)


def test_percent_trail_rejects_zero_or_one_pct():
    bars = _flat_bars(5)
    assert ts.compute_percent_trail_stop(bars, pct=0.0) is None
    assert ts.compute_percent_trail_stop(bars, pct=1.0) is None


def test_percent_trail_short_above_extreme():
    bars = _bars((105.0, 100.0, 104.0), (100.0, 95.0, 99.0))
    out = ts.compute_percent_trail_stop(bars, pct=0.10, side="short")
    assert out["extreme_price"] == pytest.approx(95.0)
    assert out["stop_price"] == pytest.approx(95.0 * 1.1)


# ---------------------------------------------------------------------------
# Dispatch + unknown-method handling
# ---------------------------------------------------------------------------

def test_compute_stop_dispatches_to_named_method():
    bars = _flat_bars(25)
    a = ts.compute_stop("atr_trail", bars, entry_price=100.0)
    c = ts.compute_stop("chandelier", bars, entry_price=100.0)
    p = ts.compute_stop("percent_trail", bars, entry_price=100.0)
    assert a is not None
    assert c is not None
    assert p is not None


def test_compute_stop_unknown_method_raises():
    with pytest.raises(ValueError):
        ts.compute_stop("magic", _flat_bars(20), entry_price=100.0)


# ---------------------------------------------------------------------------
# Ratchet
# ---------------------------------------------------------------------------

def test_ratchet_long_only_moves_up():
    assert ts.ratchet(95.0, 97.0, side="long") == 97.0
    assert ts.ratchet(97.0, 95.0, side="long") == 97.0  # no loosen
    assert ts.ratchet(97.0, 97.0, side="long") == 97.0


def test_ratchet_short_only_moves_down():
    assert ts.ratchet(105.0, 103.0, side="short") == 103.0
    assert ts.ratchet(103.0, 105.0, side="short") == 103.0  # no loosen


def test_ratchet_initial_stop_when_none():
    assert ts.ratchet(None, 95.0, side="long") == 95.0


# ---------------------------------------------------------------------------
# Persistence — upsert / get / clear
# ---------------------------------------------------------------------------

def test_get_stop_returns_none_when_absent(isolated_db):
    conn = db.init_db()
    try:
        assert ts.get_stop(conn, strategy_id="s", symbol="X") is None
    finally:
        conn.close()


def test_upsert_then_get(isolated_db):
    conn = db.init_db()
    try:
        ts.upsert_stop(
            conn, strategy_id="s", symbol="X",
            method="atr_trail", stop_price=95.0, extreme_price=100.0,
        )
        out = ts.get_stop(conn, strategy_id="s", symbol="X")
        assert out["stop_price"] == 95.0
        assert out["extreme_price"] == 100.0
        assert out["method"] == "atr_trail"
        assert out["side"] == "long"
    finally:
        conn.close()


def test_upsert_idempotent(isolated_db):
    conn = db.init_db()
    try:
        ts.upsert_stop(
            conn, strategy_id="s", symbol="X",
            method="atr_trail", stop_price=95.0, extreme_price=100.0,
        )
        ts.upsert_stop(
            conn, strategy_id="s", symbol="X",
            method="atr_trail", stop_price=97.0, extreme_price=102.0,
        )
        # Only one row, with the latest values.
        rows = conn.execute(
            "SELECT COUNT(*) FROM trailing_stops"
        ).fetchone()
        assert rows[0] == 1
        out = ts.get_stop(conn, strategy_id="s", symbol="X")
        assert out["stop_price"] == 97.0
    finally:
        conn.close()


def test_clear_stop_removes_row(isolated_db):
    conn = db.init_db()
    try:
        ts.upsert_stop(
            conn, strategy_id="s", symbol="X",
            method="atr_trail", stop_price=95.0, extreme_price=100.0,
        )
        assert ts.clear_stop(conn, strategy_id="s", symbol="X") is True
        assert ts.get_stop(conn, strategy_id="s", symbol="X") is None
        # Second call is a no-op (returns False).
        assert ts.clear_stop(conn, strategy_id="s", symbol="X") is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# advance_stop — bar-close ratchet through the persistence layer
# ---------------------------------------------------------------------------

def test_advance_stop_initial_insert(isolated_db):
    conn = db.init_db()
    try:
        bars = _flat_bars(20)  # ATR=1, HH=100.5, stop=97.5
        out = ts.advance_stop(
            conn, strategy_id="s", symbol="X",
            entry_price=100.0, bars=bars,
            method="atr_trail", multiplier=3.0,
        )
        assert out is not None
        assert out["stop_price"] == pytest.approx(97.5, abs=0.05)
        # Row persisted.
        assert ts.get_stop(conn, strategy_id="s", symbol="X") is not None
    finally:
        conn.close()


def test_advance_stop_ratchets_up_with_higher_hh(isolated_db):
    conn = db.init_db()
    try:
        bars = _flat_bars(20)
        ts.advance_stop(
            conn, strategy_id="s", symbol="X",
            entry_price=100.0, bars=bars,
            method="atr_trail", multiplier=3.0,
        )
        first = ts.get_stop(conn, strategy_id="s", symbol="X")
        # Append bars with higher highs — stop should rise.
        bars2 = bars + _bars(*((110.5, 109.5, 110.0) for _ in range(5)))
        out2 = ts.advance_stop(
            conn, strategy_id="s", symbol="X",
            entry_price=100.0, bars=bars2,
            method="atr_trail", multiplier=3.0,
        )
        assert out2["stop_price"] > first["stop_price"]
    finally:
        conn.close()


def test_advance_stop_does_not_loosen_on_flat_bar(isolated_db):
    conn = db.init_db()
    try:
        bars = _flat_bars(20) + _bars(*((110.5, 109.5, 110.0)
                                          for _ in range(5)))
        ts.advance_stop(
            conn, strategy_id="s", symbol="X",
            entry_price=100.0, bars=bars,
            method="atr_trail", multiplier=3.0,
        )
        higher_stop = ts.get_stop(conn, strategy_id="s",
                                    symbol="X")["stop_price"]

        # Now append bars with LOWER highs — stop must NOT loosen.
        bars2 = bars + _bars(*((105.5, 104.5, 105.0) for _ in range(5)))
        out2 = ts.advance_stop(
            conn, strategy_id="s", symbol="X",
            entry_price=100.0, bars=bars2,
            method="atr_trail", multiplier=3.0,
        )
        assert out2["stop_price"] == higher_stop
    finally:
        conn.close()


def test_advance_stop_insufficient_data_leaves_existing(isolated_db):
    conn = db.init_db()
    try:
        ts.upsert_stop(
            conn, strategy_id="s", symbol="X",
            method="atr_trail", stop_price=95.0, extreme_price=100.0,
        )
        # Now call advance_stop with too few bars — returns None,
        # existing row untouched.
        out = ts.advance_stop(
            conn, strategy_id="s", symbol="X",
            entry_price=100.0, bars=_flat_bars(5),
            method="atr_trail",
        )
        assert out is None
        existing = ts.get_stop(conn, strategy_id="s", symbol="X")
        assert existing["stop_price"] == 95.0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# should_exit_on_trailing_stop — auto-trader integration predicate
# ---------------------------------------------------------------------------

def test_exit_predicate_returns_false_when_no_stop(isolated_db):
    conn = db.init_db()
    try:
        assert ts.should_exit_on_trailing_stop(
            conn, strategy_id="s", symbol="X", current_price=80.0,
        ) is False
    finally:
        conn.close()


def test_exit_predicate_long_triggers_at_or_below_stop(isolated_db):
    conn = db.init_db()
    try:
        ts.upsert_stop(
            conn, strategy_id="s", symbol="X",
            method="atr_trail", stop_price=95.0, extreme_price=100.0,
        )
        assert ts.should_exit_on_trailing_stop(
            conn, strategy_id="s", symbol="X", current_price=95.0,
        ) is True
        assert ts.should_exit_on_trailing_stop(
            conn, strategy_id="s", symbol="X", current_price=94.9,
        ) is True
        assert ts.should_exit_on_trailing_stop(
            conn, strategy_id="s", symbol="X", current_price=95.01,
        ) is False
    finally:
        conn.close()


def test_exit_predicate_short_triggers_at_or_above_stop(isolated_db):
    conn = db.init_db()
    try:
        ts.upsert_stop(
            conn, strategy_id="s", symbol="X",
            method="atr_trail", stop_price=105.0, extreme_price=100.0,
            side="short",
        )
        assert ts.should_exit_on_trailing_stop(
            conn, strategy_id="s", symbol="X", current_price=105.0,
        ) is True
        assert ts.should_exit_on_trailing_stop(
            conn, strategy_id="s", symbol="X", current_price=104.99,
        ) is False
    finally:
        conn.close()
