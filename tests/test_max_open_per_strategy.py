import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _winner_settings(**overrides):
    s = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 1, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.0,
        "max_position_usd": 1000,
    }
    s.update(overrides)
    return s


def _seed_winner(conn, *, returns):
    db.upsert_strategy(conn, {"extra": {"strategy_id": "winner"}})
    for i, ret in enumerate(returns):
        sid = db.record_signal(
            conn, strategy_id="winner", symbol="X",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )


def _seed_paper_buy(conn, *, strategy_id, symbol, submitted_at,
                     status="filled", qty=10):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    sid = db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=submitted_at[:10], signal_type="long_entry",
        close=100.0, bar_interval="1d",
    )
    db.record_paper_trade(conn, {
        "alpaca_order_id": f"o-{strategy_id}-{symbol}-{submitted_at}",
        "signal_id": sid,
        "strategy_id": strategy_id, "symbol": symbol,
        "side": "buy", "qty": qty, "order_type": "market",
        "submitted_at": submitted_at, "status": status,
        "fill_price": 100.0,
    })


# ---- Coercion --------------------------------------------------------------

def test_coerce_max_open_per_strategy_default():
    assert at._coerce_max_open_per_strategy(None) == at.DEFAULT_MAX_OPEN_PER_STRATEGY


def test_coerce_max_open_per_strategy_zero_disables():
    assert at._coerce_max_open_per_strategy(0) == 0


def test_coerce_max_open_per_strategy_negative_disables():
    assert at._coerce_max_open_per_strategy(-5) == 0


def test_coerce_max_open_per_strategy_garbage_defaults():
    assert at._coerce_max_open_per_strategy("garbage") == at.DEFAULT_MAX_OPEN_PER_STRATEGY


# ---- Open-position counter -----------------------------------------------

def test_open_count_empty(isolated_db):
    conn = db.init_db()
    assert at._open_position_count_per_strategy(conn) == {}


def test_open_count_single_strategy_multiple_symbols(isolated_db):
    conn = db.init_db()
    _seed_paper_buy(conn, strategy_id="s1", symbol="GDX",
                     submitted_at="2026-05-01T13:30:00Z")
    _seed_paper_buy(conn, strategy_id="s1", symbol="KRE",
                     submitted_at="2026-05-02T13:30:00Z")
    _seed_paper_buy(conn, strategy_id="s1", symbol="XLF",
                     submitted_at="2026-05-03T13:30:00Z")
    counts = at._open_position_count_per_strategy(conn)
    assert counts == {"s1": 3}


def test_open_count_excludes_closed_pairs(isolated_db):
    conn = db.init_db()
    _seed_paper_buy(conn, strategy_id="s1", symbol="GDX",
                     submitted_at="2026-05-01T13:30:00Z")
    # Add a SELL for the same (sid, sym) → that position is closed.
    sid = db.record_signal(conn, strategy_id="s1", symbol="GDX",
                            bar_ts="2026-05-05", signal_type="long_exit",
                            close=110.0, bar_interval="1d")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "o-sell-gdx",
        "signal_id": sid, "strategy_id": "s1", "symbol": "GDX",
        "side": "sell", "qty": 10, "order_type": "market",
        "submitted_at": "2026-05-05T13:30:00Z", "status": "filled",
        "fill_price": 110.0,
    })
    counts = at._open_position_count_per_strategy(conn)
    assert counts == {}


def test_open_count_excludes_rejected(isolated_db):
    conn = db.init_db()
    _seed_paper_buy(conn, strategy_id="s1", symbol="GDX",
                     submitted_at="2026-05-01T13:30:00Z", status="rejected")
    counts = at._open_position_count_per_strategy(conn)
    assert counts == {}


def test_open_count_multiple_strategies(isolated_db):
    conn = db.init_db()
    _seed_paper_buy(conn, strategy_id="s1", symbol="GDX",
                     submitted_at="2026-05-01T13:30:00Z")
    _seed_paper_buy(conn, strategy_id="s1", symbol="KRE",
                     submitted_at="2026-05-02T13:30:00Z")
    _seed_paper_buy(conn, strategy_id="s2", symbol="XLF",
                     submitted_at="2026-05-02T13:30:00Z")
    counts = at._open_position_count_per_strategy(conn)
    assert counts == {"s1": 2, "s2": 1}


# ---- Integration with process_signals ------------------------------------

def test_cap_blocks_entry_when_at_limit(isolated_db):
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    # Seed 3 existing open positions for "winner".
    _seed_paper_buy(conn, strategy_id="winner", symbol="A",
                     submitted_at="2026-05-01T13:30:00Z")
    _seed_paper_buy(conn, strategy_id="winner", symbol="B",
                     submitted_at="2026-05-02T13:30:00Z")
    _seed_paper_buy(conn, strategy_id="winner", symbol="C",
                     submitted_at="2026-05-03T13:30:00Z")
    # New entry signal should be skipped.
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = _winner_settings(risk={"max_open_per_strategy": 3})
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    skipped = [a for a in res["actions"]
                if a.get("action") == "SKIP_MAX_OPEN_PER_STRATEGY"]
    assert len(skipped) == 1
    assert skipped[0]["open_count"] == 3
    assert skipped[0]["cap"] == 3


def test_cap_allows_when_below_limit(isolated_db):
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    _seed_paper_buy(conn, strategy_id="winner", symbol="A",
                     submitted_at="2026-05-01T13:30:00Z")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = _winner_settings(risk={"max_open_per_strategy": 3})
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    assert len(buys) == 1


def test_cap_does_not_block_exits(isolated_db):
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    # 3 open BUYs + an EXIT signal for one of them.
    for sym in ("A", "B", "C"):
        _seed_paper_buy(conn, strategy_id="winner", symbol=sym,
                         submitted_at=f"2026-05-0{ord(sym)-ord('A')+1}T13:30:00Z")
    db.record_signal(conn, strategy_id="winner", symbol="A",
                     bar_ts="2026-05-14", signal_type="long_exit",
                     close=110.0, bar_interval="1d")
    settings = _winner_settings(risk={"max_open_per_strategy": 3},
                                  dry_run=False)
    sells = []
    def fake_submit(client, *, symbol, qty, side, client_order_id=None):
        sells.append((symbol, side))
        order = MagicMock(); order.id = "x"
        order.status = "accepted"
        order.submitted_at = "2026-05-14T20:30:00Z"
        return order
    import monitoring.auto_trader as at_mod
    orig = at_mod._submit_market_order
    at_mod._submit_market_order = fake_submit
    try:
        res = at.process_signals(conn, asof=date(2026, 5, 14),
                                  settings=settings, client=MagicMock())
    finally:
        at_mod._submit_market_order = orig
    sell_actions = [a for a in res["actions"]
                     if a.get("action") == "SELL"]
    assert len(sell_actions) == 1
    assert ("A", "sell") in sells


def test_cap_zero_disables(isolated_db):
    """max_open_per_strategy=0 → no enforcement (even with many open positions)."""
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    for sym in ("A", "B", "C", "D", "E"):
        _seed_paper_buy(conn, strategy_id="winner", symbol=sym,
                         submitted_at=f"2026-05-{ord(sym)-ord('A')+1:02d}T13:30:00Z")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = _winner_settings(risk={"max_open_per_strategy": 0})
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    assert len(buys) == 1


def test_cap_other_strategies_unaffected(isolated_db):
    """A capped strategy doesn't block other strategies' signals."""
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    db.upsert_strategy(conn, {"extra": {"strategy_id": "other"}})
    # Seed enough "other" outcomes to be eligible.
    for i in range(5):
        sid = db.record_signal(
            conn, strategy_id="other", symbol="Y",
            bar_ts=f"2024-02-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-02-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-02-{i+2:02d}",
            exit_price=102.0, exit_reason="long_exit_signal", bars_held=1,
        )
    # Cap winner with 3 open positions; other has none.
    for sym in ("A", "B", "C"):
        _seed_paper_buy(conn, strategy_id="winner", symbol=sym,
                         submitted_at=f"2026-05-0{ord(sym)-ord('A')+1}T13:30:00Z")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="other", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=80.0, bar_interval="1d")
    settings = _winner_settings(risk={"max_open_per_strategy": 3})
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    actions = {a["strategy_id"]: a["action"] for a in res["actions"]}
    assert actions["winner"] == "SKIP_MAX_OPEN_PER_STRATEGY"
    assert actions["other"] == "DRY_BUY"


def test_cap_in_run_increment_blocks_extra_entries(isolated_db):
    """Two new entries for the same strategy: cap=1 means the second is
    blocked even though no DB-side open positions existed at start."""
    conn = db.init_db()
    _seed_winner(conn, returns=[2.0, 1.0])
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="winner", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=68.0, bar_interval="1d")
    settings = _winner_settings(risk={"max_open_per_strategy": 1})
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    skipped = [a for a in res["actions"]
                if a.get("action") == "SKIP_MAX_OPEN_PER_STRATEGY"]
    assert len(buys) == 1
    assert len(skipped) == 1
