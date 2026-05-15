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
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "high-sharpe"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "mid-sharpe"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "low-sharpe"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _seed_outcomes(strategy_id, returns, sym_prefix="OLDA"):
    conn = db.init_db()
    for i, ret in enumerate(returns):
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol=sym_prefix,
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
    return conn


def _winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000, "skip_intraday_signals": True,
    }


# ---------------------------------------------------------------------------
# _max_pct_per_symbol
# ---------------------------------------------------------------------------

def test_max_pct_default_when_missing():
    assert at._max_pct_per_symbol({}) == at.DEFAULT_MAX_PCT_PER_SYMBOL


def test_max_pct_from_risk_section():
    assert at._max_pct_per_symbol({"risk": {"max_pct_per_symbol": 0.50}}) == 0.50


def test_max_pct_back_compat_top_level():
    assert at._max_pct_per_symbol({"max_pct_per_symbol": 0.20}) == 0.20


def test_max_pct_clamps_out_of_range():
    assert at._max_pct_per_symbol({"risk": {"max_pct_per_symbol": -1}}) \
        == at.DEFAULT_MAX_PCT_PER_SYMBOL
    assert at._max_pct_per_symbol({"risk": {"max_pct_per_symbol": 5}}) \
        == at.DEFAULT_MAX_PCT_PER_SYMBOL
    assert at._max_pct_per_symbol({"risk": {"max_pct_per_symbol": "bad"}}) \
        == at.DEFAULT_MAX_PCT_PER_SYMBOL


# ---------------------------------------------------------------------------
# _open_notional_by_symbol
# ---------------------------------------------------------------------------

def test_open_notional_by_symbol_sums_fills(isolated_db):
    conn = db.init_db()
    db.record_paper_trade(conn, {
        "alpaca_order_id": "o1", "strategy_id": "x",
        "symbol": "KRE", "side": "buy", "qty": 10,
        "order_type": "market", "fill_price": 60.0,
        "submitted_at": "2026-05-14", "status": "filled",
    })
    db.record_paper_trade(conn, {
        "alpaca_order_id": "o2", "strategy_id": "y",
        "symbol": "KRE", "side": "buy", "qty": 5,
        "order_type": "market", "fill_price": 60.0,
        "submitted_at": "2026-05-14", "status": "filled",
    })
    db.record_paper_trade(conn, {
        "alpaca_order_id": "o3", "strategy_id": "z",
        "symbol": "GDX", "side": "buy", "qty": 20,
        "order_type": "market", "fill_price": 30.0,
        "submitted_at": "2026-05-14", "status": "filled",
    })
    out = at._open_notional_by_symbol(conn)
    assert out["KRE"] == pytest.approx(15 * 60.0)
    assert out["GDX"] == pytest.approx(20 * 30.0)


def test_open_notional_skips_sells(isolated_db):
    conn = db.init_db()
    db.record_paper_trade(conn, {
        "alpaca_order_id": "o1", "strategy_id": "x",
        "symbol": "KRE", "side": "sell", "qty": 10,
        "order_type": "market", "fill_price": 60.0,
        "submitted_at": "2026-05-14", "status": "filled",
    })
    out = at._open_notional_by_symbol(conn)
    assert out == {}


# ---------------------------------------------------------------------------
# Concentration block map: ranking by sharpe
# ---------------------------------------------------------------------------

def test_concentration_no_blocks_when_only_one_strategy_per_symbol(isolated_db):
    _seed_outcomes("high-sharpe", [2.0, 1.0] * 18)
    conn = db.init_db()
    db.record_signal(conn, strategy_id="high-sharpe", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    sigs = conn.execute("SELECT * FROM signals WHERE bar_ts='2026-05-14'").fetchall()
    blocks = at._concentration_block_map(
        conn, _winner_settings(), sigs, portfolio_value=10_000.0,
    )
    assert blocks == {}


def test_concentration_blocks_lower_sharpe_when_cap_hit(isolated_db):
    # high-sharpe: stable +2% / -1% → high sharpe
    _seed_outcomes("high-sharpe", [2.0, -1.0] * 18)
    # low-sharpe: wide +5 / -5 → near-zero sharpe
    _seed_outcomes("low-sharpe", [5.0, -5.0] * 18)
    conn = db.init_db()
    # Both want to buy KRE today.
    sig_high = db.record_signal(
        conn, strategy_id="high-sharpe", symbol="KRE",
        bar_ts="2026-05-14", signal_type="long_entry",
        close=60.0, bar_interval="1d",
    )
    sig_low = db.record_signal(
        conn, strategy_id="low-sharpe", symbol="KRE",
        bar_ts="2026-05-14", signal_type="long_entry",
        close=60.0, bar_interval="1d",
    )
    sigs = conn.execute("SELECT * FROM signals WHERE bar_ts='2026-05-14'").fetchall()
    # Portfolio 5000, cap 30% = 1500; max_position=1000 → exactly one
    # entry fits (1000 < 1500 < 2000). high-sharpe wins.
    settings = {**_winner_settings(),
                "risk": {"max_pct_per_symbol": 0.30}}
    blocks = at._concentration_block_map(
        conn, settings, sigs, portfolio_value=5000.0,
    )
    assert sig_high not in blocks
    assert sig_low in blocks
    blk = blocks[sig_low]
    assert blk["action"] == "SKIP_CONCENTRATION_CAP"
    assert blk["symbol"] == "KRE"
    assert blk["max_pct_per_symbol"] == 0.30
    assert blk["cap_usd"] == 1500.0


def test_concentration_admits_multiple_when_room_available(isolated_db):
    _seed_outcomes("high-sharpe", [2.0, -1.0] * 18)
    _seed_outcomes("mid-sharpe", [1.0, -0.5] * 18)
    conn = db.init_db()
    db.record_signal(conn, strategy_id="high-sharpe", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="mid-sharpe", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    sigs = conn.execute("SELECT * FROM signals WHERE bar_ts='2026-05-14'").fetchall()
    # Cap 30% of 10_000 = 3000; max_position=1000 → up to 3 fit.
    settings = {**_winner_settings(),
                "risk": {"max_pct_per_symbol": 0.30}}
    blocks = at._concentration_block_map(
        conn, settings, sigs, portfolio_value=10_000.0,
    )
    assert blocks == {}


def test_concentration_counts_existing_open_positions(isolated_db):
    _seed_outcomes("high-sharpe", [2.0, -1.0] * 18)
    conn = db.init_db()
    # Existing $900 of KRE already on the books.
    db.record_paper_trade(conn, {
        "alpaca_order_id": "o-old", "strategy_id": "other",
        "symbol": "KRE", "side": "buy", "qty": 15,
        "order_type": "market", "fill_price": 60.0,
        "submitted_at": "2026-05-13", "status": "filled",
    })
    sig = db.record_signal(
        conn, strategy_id="high-sharpe", symbol="KRE",
        bar_ts="2026-05-14", signal_type="long_entry",
        close=60.0, bar_interval="1d",
    )
    sigs = conn.execute("SELECT * FROM signals WHERE bar_ts='2026-05-14'").fetchall()
    # Cap 30% of 3000 = 900; existing $900 used → new $1000 won't fit.
    settings = {**_winner_settings(),
                "risk": {"max_pct_per_symbol": 0.30}}
    blocks = at._concentration_block_map(
        conn, settings, sigs, portfolio_value=3000.0,
    )
    assert sig in blocks
    assert blocks[sig]["used_usd"] == pytest.approx(900.0)


def test_concentration_different_symbols_dont_compete(isolated_db):
    _seed_outcomes("high-sharpe", [2.0, -1.0] * 18)
    _seed_outcomes("low-sharpe", [5.0, -5.0] * 18)
    conn = db.init_db()
    # Different symbols → both allowed even when cap is tight per-symbol.
    db.record_signal(conn, strategy_id="high-sharpe", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="low-sharpe", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=30.0, bar_interval="1d")
    sigs = conn.execute("SELECT * FROM signals WHERE bar_ts='2026-05-14'").fetchall()
    settings = {**_winner_settings(),
                "risk": {"max_pct_per_symbol": 0.30}}
    # 5000 portfolio → per-symbol cap 1500 ≥ max_position 1000.
    blocks = at._concentration_block_map(
        conn, settings, sigs, portfolio_value=5000.0,
    )
    assert blocks == {}


def test_concentration_no_portfolio_disables_blocking(isolated_db):
    _seed_outcomes("high-sharpe", [2.0, -1.0] * 18)
    _seed_outcomes("low-sharpe", [5.0, -5.0] * 18)
    conn = db.init_db()
    db.record_signal(conn, strategy_id="high-sharpe", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="low-sharpe", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    sigs = conn.execute("SELECT * FROM signals WHERE bar_ts='2026-05-14'").fetchall()
    blocks = at._concentration_block_map(
        conn, {**_winner_settings(),
                "risk": {"max_pct_per_symbol": 0.30}},
        sigs, portfolio_value=None,
    )
    assert blocks == {}


# ---------------------------------------------------------------------------
# End-to-end through process_signals (kelly off so portfolio_value still
# flows in via account_summary_fn for the cap math)
# ---------------------------------------------------------------------------

def test_process_signals_skips_lower_sharpe_with_concentration_cap(isolated_db):
    _seed_outcomes("high-sharpe", [2.0, -1.0] * 18)
    _seed_outcomes("low-sharpe", [5.0, -5.0] * 18)
    conn = db.init_db()
    db.record_signal(conn, strategy_id="high-sharpe", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="low-sharpe", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "min_sharpe_ish": -10.0,  # let low-sharpe pass eligibility
                "min_mean_ret_pct": -10.0,
                "risk": {"max_pct_per_symbol": 0.30}}
    # 5000 portfolio, cap 1500, max_position 1000 → exactly one entry fits.
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {"portfolio_value": 5000.0},
    )
    actions = {a["strategy_id"]: a for a in res["actions"]
               if a["strategy_id"] in ("high-sharpe", "low-sharpe")}
    assert actions["high-sharpe"]["action"] == "DRY_BUY"
    assert actions["low-sharpe"]["action"] == "SKIP_CONCENTRATION_CAP"


def test_process_signals_single_strategy_unaffected_by_cap(isolated_db):
    _seed_outcomes("high-sharpe", [2.0, -1.0] * 18)
    conn = db.init_db()
    db.record_signal(conn, strategy_id="high-sharpe", symbol="KRE",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=60.0, bar_interval="1d")
    settings = {**_winner_settings(),
                "risk": {"max_pct_per_symbol": 0.30}}
    # 5000 portfolio, cap 1500 → one entry of 1000 fits comfortably.
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        account_summary_fn=lambda: {"portfolio_value": 5000.0},
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "high-sharpe"]
    assert actions[0]["action"] == "DRY_BUY"
