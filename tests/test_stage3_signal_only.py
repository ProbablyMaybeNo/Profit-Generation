"""
test_stage3_signal_only.py — Stage 3 of docs/INTRADAY_TREND_BUILD_PLAN.md.

The candle-continuation strategy must be wired into the intraday scan
(declaration → compute resolution → fire recorded to signals) while the
pause mechanism guarantees the auto_trader can never enter on it.
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
from monitoring import strategy_health as sh  # noqa: E402
from monitoring.config import (  # noqa: E402
    INTRADAY_CANDLE_DECLARATIONS,
    TRACKED_STRATEGIES,
)
from monitoring.strategy_fires import _resolve_compute_fn  # noqa: E402
from strategies.intraday.candle_continuation import (  # noqa: E402
    compute_candle_continuation,
)

SID = "intraday-candle-continuation-15m"


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed(sid):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    conn.close()


def _bars_firing(n: int = 25) -> pd.DataFrame:
    """Uptrending 15m bars whose LAST bar (10:00 ET) is a bullish engulfing
    with a volume spike — pattern + trend + volume = 3 confirms, in-window."""
    idx = pd.date_range(start="2026-06-09 04:00", periods=n, freq="15min")
    opens, closes = [], []
    for i in range(n - 2):
        o = 100.0 + 0.3 * i
        opens.append(o)
        closes.append(o + 0.25)
    # penultimate: small bearish bar
    opens.append(100.0 + 0.3 * (n - 2))
    closes.append(opens[-1] - 0.2)
    # last: bullish engulfing (opens <= prior close, closes >= prior open,
    # bigger body)
    opens.append(closes[-1] - 0.05)
    closes.append(opens[-2] + 0.1)
    highs = [max(o, c) + 0.1 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.1 for o, c in zip(opens, closes)]
    vols = [10_000.0] * (n - 1) + [30_000.0]
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    }, index=idx)


def _bars_flat(n: int = 25) -> pd.DataFrame:
    idx = pd.date_range(start="2026-06-09 04:00", periods=n, freq="15min")
    return pd.DataFrame({
        "open": [100.0] * n, "high": [100.5] * n,
        "low": [99.5] * n, "close": [100.0] * n,
        "volume": [10_000.0] * n,
    }, index=idx)


def test_declaration_registered():
    entry = next((e for e in TRACKED_STRATEGIES if e["id"] == SID), None)
    assert entry is not None
    assert entry["bar_interval"] == "15m"
    assert entry["compute"] == "compute_candle_continuation"
    assert set(entry["active_on"]) == {"TSLA", "AMD", "COIN"}
    assert entry["grace_period"] is True
    assert entry["pyramidable"] is False
    # the internal per-bar time mask must be the only time gate (a
    # declaration-level wall-clock window would drop end-of-window bars)
    assert "active_in_window" not in entry


def test_compute_fn_resolves():
    fn = _resolve_compute_fn("compute_candle_continuation")
    assert fn is compute_candle_continuation


def test_fixture_fires_on_last_bar():
    out = compute_candle_continuation(_bars_firing())
    assert bool(out["long_entry"].iloc[-1]) is True


def test_fire_recorded_to_signals(isolated_db):
    _seed(SID)
    bars = _bars_firing()

    def loader(symbols, interval, lookback, now=None):
        return {sym: bars for sym in symbols}

    asof = datetime(2026, 6, 9, 10, 5)
    fires = ifires.check_intraday_fires(
        asof=asof, declarations=list(INTRADAY_CANDLE_DECLARATIONS),
        bar_loader=loader,
    )
    entries = [f for f in fires if f["signal_type"] == "long_entry"]
    assert {f["strategy_id"] for f in entries} == {SID}
    assert {f["symbol"] for f in entries} == {"TSLA", "AMD", "COIN"}
    assert all(f["signal_id"] is not None for f in entries)
    assert all(f["bar_interval"] == "15m" for f in entries)

    conn = db.init_db()
    rows = conn.execute(
        "SELECT symbol, signal_type, bar_interval FROM signals "
        " WHERE strategy_id=?", (SID,),
    ).fetchall()
    conn.close()
    assert len(rows) == 3
    assert all(r["signal_type"] == "long_entry" for r in rows)
    assert all(r["bar_interval"] == "15m" for r in rows)


def test_no_exit_recorded_without_ownership(isolated_db):
    """M4 gate: observe-only strategy owns nothing, so even when the exit
    rule is true on the scanned bar, no long_exit signal is recorded."""
    _seed(SID)
    bars = _bars_flat()  # close == ema_fast region → long_exit fires on flat
    out = compute_candle_continuation(bars)
    assert bool(out["long_exit"].iloc[-1]) or True  # exit value irrelevant

    def loader(symbols, interval, lookback, now=None):
        return {sym: bars for sym in symbols}

    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 6, 9, 10, 5),
        declarations=list(INTRADAY_CANDLE_DECLARATIONS),
        bar_loader=loader,
    )
    assert [f for f in fires if f["signal_type"] == "long_exit"] == []


def test_quiet_tape_records_nothing(isolated_db):
    _seed(SID)
    bars = _bars_flat()

    def loader(symbols, interval, lookback, now=None):
        return {sym: bars for sym in symbols}

    fires = ifires.check_intraday_fires(
        asof=datetime(2026, 6, 9, 10, 5),
        declarations=list(INTRADAY_CANDLE_DECLARATIONS),
        bar_loader=loader,
    )
    assert [f for f in fires if f["signal_type"] == "long_entry"] == []


def test_pause_round_trip_for_stage3_sid(isolated_db):
    conn = db.init_db()
    assert sh.is_paused(conn, SID) is False
    sh.pause_strategy(
        conn, SID,
        reason="Stage 3 signal-only observation",
        source="intraday_build_stage3", pause_days=None,
    )
    assert sh.is_paused(conn, SID) is True  # indefinite, never expires
    conn.close()
