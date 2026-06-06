"""test_exit_gating_m4.py — Sprint 3 / M4: exit-signal gating to owned holdings.

The intraday/trend scanners emitted a long_exit signal on EVERY bar in the scan
window where the exit rule was true, for EVERY (strategy, symbol) — regardless of
whether the strategy actually held the position. That recorded thousands of
phantom long_exit signals per run (the exit spam) and handed non-owners a SELL
signal against a position they don't own.

M4: only RECORD a long_exit when the strategy is the live OWNER of the symbol
(per M1/M2). These tests drive the REAL production scanner
(intraday_fires.check_intraday_fires) and prove a positionless strategy records
ZERO exits while a real holding records its single exit. FAILS on pre-M4 code,
which recorded the phantom exit unconditionally.
"""

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import intraday_fires as ifires  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_strategy(sid):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    conn.close()


def _seed_open_buy(sid, sym, qty, *, ts="2026-05-14T13:30:00"):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    buy_sig = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=ts,
                               signal_type="long_entry", close=100.0,
                               bar_interval="5m")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?)",
        (f"b-{sid}-{sym}", buy_sig, sid, sym, qty, ts, ts),
    )
    conn.commit()
    conn.close()


def _exit_bars(n=60, exit_idx=55, interval_min=5):
    idx = pd.date_range(start="2026-05-14 09:30", periods=n,
                        freq=f"{interval_min}min")
    df = pd.DataFrame({
        "open": [100.0] * n, "high": [100.5] * n,
        "low": [99.5] * n, "close": [100.0] * n,
        "volume": [10_000.0] * n,
    }, index=idx)
    df["long_entry"] = [False] * n
    long_exit = [False] * n
    long_exit[exit_idx] = True
    df["long_exit"] = long_exit
    return df


def _install_fake_compute(monkeypatch, name, df):
    def _resolver(compute_name):
        if compute_name == name:
            return lambda d: df
        raise ValueError(compute_name)
    monkeypatch.setattr(ifires, "_resolve_compute_fn", _resolver)


def _decl(sid, compute, interval, symbols):
    return {"id": sid, "compute": compute, "bar_interval": interval,
            "active_on": symbols}


def test_positionless_strategy_records_no_exit(isolated_db, monkeypatch):
    """A strategy with NO open position records ZERO long_exit signals even
    when the exit rule fires — kills the spam. Pre-M4 it recorded the exit."""
    sid = "intraday-orbo-5m"
    _seed_strategy(sid)
    decls = [_decl(sid, "compute_fake_5m", "5m", ["NVDA"])]
    bars = _exit_bars()
    _install_fake_compute(monkeypatch, "compute_fake_5m", bars)
    loader = lambda symbols, interval, lookback, *, now: {  # noqa: E731
        s: bars.drop(columns=["long_entry", "long_exit"]) for s in symbols
    }
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 10, 30),
        declarations=decls, bar_loader=loader,
    )
    exits = [f for f in fires if f["signal_type"] == "long_exit"]
    assert exits == [], (
        f"positionless strategy recorded {len(exits)} phantom exits "
        f"(pre-M4 exit spam)")
    # And nothing landed in the signals table either.
    conn = db.init_db()
    n = conn.execute(
        "SELECT COUNT(*) c FROM signals WHERE signal_type='long_exit'"
    ).fetchone()["c"]
    conn.close()
    assert n == 0


def test_owned_holding_records_its_single_exit(isolated_db, monkeypatch):
    """A strategy that OWNS the symbol (holds an open buy) records its one
    real long_exit when the rule fires."""
    sid = "intraday-orbo-5m"
    sym = "NVDA"
    _seed_open_buy(sid, sym, 8)
    decls = [_decl(sid, "compute_fake_5m", "5m", [sym])]
    bars = _exit_bars()
    _install_fake_compute(monkeypatch, "compute_fake_5m", bars)
    loader = lambda symbols, interval, lookback, *, now: {  # noqa: E731
        s: bars.drop(columns=["long_entry", "long_exit"]) for s in symbols
    }
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 10, 30),
        declarations=decls, bar_loader=loader,
    )
    exits = [f for f in fires
             if f["signal_type"] == "long_exit" and f["signal_id"] is not None]
    assert len(exits) == 1, f"owner should record exactly one exit, got {exits}"


def test_non_owner_on_shared_symbol_records_no_exit(isolated_db, monkeypatch):
    """Legacy shared symbol: strategy A owns IWM (older buy), B also holds. The
    scanner running for B records NO exit — only the owner A may exit."""
    sym = "IWM"
    owner = "intraday-orb-pivots-5m"
    other = "intraday-orbo-5m"
    _seed_open_buy(owner, sym, 10, ts="2026-05-14T13:00:00")
    _seed_open_buy(other, sym, 10, ts="2026-05-14T13:05:00")
    decls = [_decl(other, "compute_fake_5m", "5m", [sym])]
    bars = _exit_bars()
    _install_fake_compute(monkeypatch, "compute_fake_5m", bars)
    loader = lambda symbols, interval, lookback, *, now: {  # noqa: E731
        s: bars.drop(columns=["long_entry", "long_exit"]) for s in symbols
    }
    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 5, 14, 10, 30),
        declarations=decls, bar_loader=loader,
    )
    exits = [f for f in fires if f["signal_type"] == "long_exit"]
    assert exits == [], "non-owner recorded an exit on a shared symbol"
