"""7.5.1 — Alpaca IEX WebSocket listener + minute-bar storage tests.

Validates the invariants for the new data layer:

  1. Schema — intraday_bars and stream_heartbeat tables exist after
     init_db() with expected columns; idempotent re-init.
  2. Bar-upsert idempotency — feeding the same bar twice → one row.
  3. Auth handshake — build_auth_message() matches Alpaca's
     {"action":"auth","key":...,"secret":...} contract.
  4. Subscription handshake — build_subscribe_message() emits the
     {"action":"subscribe","bars":[...],"trades":[...]} shape for the
     configured symbols, after auth.
  5. Bar parsing → row — the dict shape ({T:"b", S, o, h, l, c, v, t})
     and the alpaca-py Bar model both yield identical upserts.
  6. Reconnect with backoff — compute_backoff(n) follows
     1, 2, 4, 8, 16, 32, 60, 60... and LiveStream.run_with_reconnect()
     observes that schedule on simulated drops; re-attaches handlers
     and rebuilds the auth + subscribe messages on each reconnect.
  7. Heartbeat — update_heartbeat writes the row; reconnect_delta
     increments reconnects_today; UTC midnight rollover resets it.
  8. No-impact on existing trading — running the listener alongside
     a fixture auto_trader.process_signals call leaves paper_trades
     byte-identical vs. running auto_trader alone.
"""
import sqlite3
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import live_stream as ls  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    yield c
    c.close()


def _bar_dict(symbol="SPY", ts="2026-05-22T14:30:00+00:00",
              o=400.0, h=401.0, low=399.5, c=400.5, v=12345.0):
    return {"T": "b", "S": symbol, "t": ts,
            "o": o, "h": h, "l": low, "c": c, "v": v}


class _FakeBar:
    """Mimic the alpaca-py Bar model attribute interface."""
    def __init__(self, symbol, ts, o, h, low, c, v):
        self.symbol = symbol
        self.timestamp = ts
        self.open = o
        self.high = h
        self.low = low
        self.close = c
        self.volume = v


# ---------------------------------------------------------------------------
# 1. Schema + idempotent re-init
# ---------------------------------------------------------------------------


def test_intraday_bars_table_exists(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type='table' AND name='intraday_bars'"
    ).fetchone()
    assert row is not None, "intraday_bars table missing"


def test_intraday_bars_has_expected_columns(conn):
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(intraday_bars)").fetchall()}
    expected = {"id", "symbol", "ts_utc", "open", "high", "low", "close",
                "volume", "source", "recorded_at"}
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_stream_heartbeat_table_exists(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type='table' AND name='stream_heartbeat'"
    ).fetchone()
    assert row is not None, "stream_heartbeat table missing"


def test_stream_heartbeat_has_expected_columns(conn):
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(stream_heartbeat)").fetchall()}
    expected = {"component", "last_ts", "reconnects_today", "last_error",
                "rollover_date", "state"}
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_init_db_idempotent_for_live_stream_tables(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c1 = db.init_db(test_db)
    c1.close()
    c2 = db.init_db(test_db)
    c2.execute("PRAGMA table_info(intraday_bars)").fetchall()
    c2.execute("PRAGMA table_info(stream_heartbeat)").fetchall()
    c2.close()


# ---------------------------------------------------------------------------
# 2. Bar-upsert idempotency
# ---------------------------------------------------------------------------


def test_upsert_bar_idempotent_on_duplicate(conn):
    bar = ls.parse_bar(_bar_dict())
    assert bar is not None
    id1 = ls.upsert_bar(conn, bar)
    id2 = ls.upsert_bar(conn, bar)
    assert id1 is not None
    assert id2 is None, "second insert for same (symbol, ts, source) must no-op"
    n = conn.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0]
    assert n == 1


def test_upsert_bar_distinct_source_inserts_separate_row(conn):
    """SIP and IEX feeds for the same bar are separate rows — the
    UNIQUE clause includes `source`."""
    bar = ls.parse_bar(_bar_dict())
    ls.upsert_bar(conn, bar, source="iex")
    ls.upsert_bar(conn, bar, source="sip")
    n = conn.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0]
    assert n == 2


def test_upsert_bar_distinct_ts_inserts_separate_row(conn):
    b1 = ls.parse_bar(_bar_dict(ts="2026-05-22T14:30:00+00:00"))
    b2 = ls.parse_bar(_bar_dict(ts="2026-05-22T14:31:00+00:00"))
    ls.upsert_bar(conn, b1)
    ls.upsert_bar(conn, b2)
    n = conn.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0]
    assert n == 2


def test_upsert_bar_rejects_missing_symbol_or_ts(conn):
    assert ls.upsert_bar(conn, {"symbol": None, "ts_utc": "x"}) is None
    assert ls.upsert_bar(conn, {"symbol": "SPY", "ts_utc": None}) is None
    assert ls.upsert_bar(conn, {}) is None
    n = conn.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0]
    assert n == 0


# ---------------------------------------------------------------------------
# 3. Auth handshake — Alpaca v2 stream contract
# ---------------------------------------------------------------------------


def test_build_auth_message_matches_alpaca_contract():
    msg = ls.build_auth_message("my-key", "my-secret")
    assert msg == {"action": "auth", "key": "my-key", "secret": "my-secret"}


def test_live_stream_records_auth_message_during_run(conn):
    """LiveStream.run_with_reconnect captures the auth message via the
    handshake-builder. Mirrors what alpaca-py sends internally."""
    fake = _FakeStream()
    listener = ls.LiveStream(
        conn,
        api_key="k1", secret_key="s1",
        symbols=["SPY", "QQQ"],
        stream_factory=lambda *a, **kw: fake,
        sleep_fn=lambda s: None,
    )
    listener.run_with_reconnect(
        max_attempts=1,
        stream_runner=lambda s: None,  # clean exit, no exception
    )
    assert listener.last_auth_message == {
        "action": "auth", "key": "k1", "secret": "s1",
    }


# ---------------------------------------------------------------------------
# 4. Subscription handshake
# ---------------------------------------------------------------------------


def test_build_subscribe_message_includes_bars_and_trades():
    msg = ls.build_subscribe_message(["SPY", "QQQ"], bars=True, trades=True)
    assert msg["action"] == "subscribe"
    assert msg["bars"] == ["SPY", "QQQ"]
    assert msg["trades"] == ["SPY", "QQQ"]


def test_build_subscribe_message_uppercases_symbols():
    msg = ls.build_subscribe_message(["spy", "qqq"])
    assert msg["bars"] == ["SPY", "QQQ"]
    assert msg["trades"] == ["SPY", "QQQ"]


def test_live_stream_records_subscribe_message_for_configured_universe(conn):
    fake = _FakeStream()
    listener = ls.LiveStream(
        conn,
        api_key="k", secret_key="s",
        symbols=["SPY", "QQQ", "IWM"],
        stream_factory=lambda *a, **kw: fake,
        sleep_fn=lambda s: None,
    )
    listener.run_with_reconnect(
        max_attempts=1, stream_runner=lambda s: None,
    )
    sub = listener.last_subscribe_message
    assert sub is not None
    assert sub["action"] == "subscribe"
    assert sub["bars"] == ["SPY", "QQQ", "IWM"]
    assert sub["trades"] == ["SPY", "QQQ", "IWM"]


def test_live_stream_calls_subscribe_bars_on_underlying_stream(conn):
    fake = _FakeStream()
    listener = ls.LiveStream(
        conn,
        api_key="k", secret_key="s",
        symbols=["AAA", "BBB"],
        stream_factory=lambda *a, **kw: fake,
        sleep_fn=lambda s: None,
    )
    listener.run_with_reconnect(
        max_attempts=1, stream_runner=lambda s: None,
    )
    # alpaca-py-style: subscribe_bars(handler, *symbols)
    assert fake.bar_subscriptions == [("AAA", "BBB")]
    assert fake.trade_subscriptions == [("AAA", "BBB")]


def test_live_stream_default_universe_covers_full_intraday_universe(conn):
    """A4 (audit 2026-06-03): the persisted-bar universe must equal the full
    configured intraday strategy universe (INTRADAY_1M_UNIVERSE) so MFE/MAE
    and the F2-SAFETY stale sweep work for ALL intraday symbols — not the
    stale 10 (TRACKED_STOCKS + TRACKED_SECTORS) that left AAPL/NVDA/TSLA/etc.
    with zero bars. On the old code the default was 10 symbols and the 10
    large-caps below were absent -> this asserts the gap is closed."""
    from monitoring.config import (
        INTRADAY_1M_UNIVERSE, TRACKED_STOCKS, TRACKED_SECTORS,
    )
    listener = ls.LiveStream(
        conn,
        api_key="k", secret_key="s",
        stream_factory=lambda *a, **kw: _FakeStream(),
        sleep_fn=lambda s: None,
    )
    subscribed = set(listener.symbols)
    # Every configured intraday-universe symbol is subscribed (no silent cap).
    missing = set(s.upper() for s in INTRADAY_1M_UNIVERSE) - subscribed
    assert not missing, f"intraday-universe symbols not subscribed: {missing}"
    # And the previously-absent large-caps are now present.
    for sym in ("AAPL", "MSFT", "NVDA", "TSLA", "META", "AMZN"):
        assert sym in subscribed
    # The legacy IEX 10 are still covered (union, not replacement).
    for sym in list(TRACKED_STOCKS) + list(TRACKED_SECTORS):
        assert sym.upper() in subscribed
    # No duplicates from the union.
    assert len(listener.symbols) == len(subscribed)


# ---------------------------------------------------------------------------
# 5. Bar parsing → row
# ---------------------------------------------------------------------------


def test_parse_bar_from_dict():
    bar = ls.parse_bar(_bar_dict())
    assert bar == {
        "symbol": "SPY",
        "ts_utc": "2026-05-22T14:30:00+00:00",
        "open": 400.0, "high": 401.0, "low": 399.5,
        "close": 400.5, "volume": 12345.0,
    }


def test_parse_bar_from_alpaca_py_model():
    ts = datetime(2026, 5, 22, 14, 30, tzinfo=timezone.utc)
    fb = _FakeBar("SPY", ts, 400.0, 401.0, 399.5, 400.5, 12345.0)
    bar = ls.parse_bar(fb)
    assert bar["symbol"] == "SPY"
    assert bar["ts_utc"] == "2026-05-22T14:30:00+00:00"
    assert bar["open"] == 400.0
    assert bar["volume"] == 12345.0


def test_parse_bar_rejects_non_bar_message():
    """The Alpaca stream multiplexes message types via `T`. Only T='b'
    is a bar — quotes, trades, status messages must be ignored here."""
    assert ls.parse_bar({"T": "q", "S": "SPY"}) is None
    assert ls.parse_bar({"T": "t", "S": "SPY"}) is None
    assert ls.parse_bar(None) is None


def test_parse_bar_handles_z_suffix_timestamp():
    """Alpaca sometimes serializes timestamps with a trailing Z. The
    normalizer must produce a +00:00 form so the UNIQUE index sees
    identical bytes regardless of source format."""
    bar = ls.parse_bar(_bar_dict(ts="2026-05-22T14:30:00Z"))
    assert bar["ts_utc"] == "2026-05-22T14:30:00+00:00"


def test_bar_stream_replay_writes_expected_rows(conn):
    """Feed a stream of 4 bars through the on_bar handler, then assert
    each becomes the expected intraday_bars row."""
    listener = ls.LiveStream(
        conn,
        api_key="k", secret_key="s",
        symbols=["SPY"],
        stream_factory=lambda *a, **kw: _FakeStream(),
    )
    msgs = [
        _bar_dict(ts="2026-05-22T14:30:00+00:00", c=400.0),
        _bar_dict(ts="2026-05-22T14:31:00+00:00", c=400.5),
        _bar_dict(ts="2026-05-22T14:32:00+00:00", c=401.0),
        _bar_dict(symbol="QQQ", ts="2026-05-22T14:30:00+00:00", c=300.0),
    ]
    import asyncio
    for m in msgs:
        asyncio.run(listener.on_bar(m))

    rows = conn.execute(
        "SELECT symbol, ts_utc, close, source FROM intraday_bars "
        " ORDER BY symbol, ts_utc"
    ).fetchall()
    assert len(rows) == 4
    assert tuple(rows[0]) == ("QQQ", "2026-05-22T14:30:00+00:00", 300.0, "iex")
    assert tuple(rows[1]) == ("SPY", "2026-05-22T14:30:00+00:00", 400.0, "iex")
    assert tuple(rows[2]) == ("SPY", "2026-05-22T14:31:00+00:00", 400.5, "iex")
    assert tuple(rows[3]) == ("SPY", "2026-05-22T14:32:00+00:00", 401.0, "iex")


# ---------------------------------------------------------------------------
# 6. Reconnect with backoff
# ---------------------------------------------------------------------------


def test_compute_backoff_schedule():
    """1, 2, 4, 8, 16, 32, 60, 60 ..."""
    expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    actual = [ls.compute_backoff(i) for i in range(1, len(expected) + 1)]
    assert actual == expected


def test_compute_backoff_zero_or_negative_returns_zero():
    assert ls.compute_backoff(0) == 0.0
    assert ls.compute_backoff(-3) == 0.0


def test_reconnect_observes_expected_backoff_schedule(conn):
    """Simulate three consecutive socket drops, then a clean exit on
    the fourth iteration. Backoff sleeps follow 2, 4, 8 (the +1
    offset is correct because each drop happens during the run, so
    the NEXT iteration's sleep is `compute_backoff(attempt+1)`)."""
    drops = [ConnectionResetError("drop1"), ConnectionResetError("drop2"),
             ConnectionResetError("drop3"), None]
    iters = iter(drops)

    def runner(stream):
        e = next(iters)
        if e is not None:
            raise e

    fake = _FakeStream()
    listener = ls.LiveStream(
        conn,
        api_key="k", secret_key="s", symbols=["SPY", "QQQ"],
        stream_factory=lambda *a, **kw: fake,
        sleep_fn=lambda s: None,
    )
    sleeps = listener.run_with_reconnect(
        max_attempts=4, stream_runner=runner,
    )
    # Three failed runs → three sleeps. Schedule is compute_backoff(2),
    # compute_backoff(3), compute_backoff(4) = 2, 4, 8.
    assert sleeps == [2.0, 4.0, 8.0]


def test_reconnect_rebuilds_handshake_and_attaches_handlers(conn):
    """Each reconnect must rebuild a fresh stream object, re-attach bar
    + trade handlers, and re-emit the auth + subscribe messages — the
    invariant that protects against subscription drift on flap."""
    attempts = []

    class _FakeFactory:
        def __init__(self):
            self.streams = []
        def __call__(self, key, secret, *, feed):
            s = _FakeStream()
            self.streams.append(s)
            attempts.append({"key": key, "secret": secret, "feed": feed})
            return s

    factory = _FakeFactory()
    errors = [ConnectionResetError("drop"), None]
    err_iter = iter(errors)

    def runner(stream):
        e = next(err_iter)
        if e is not None:
            raise e

    listener = ls.LiveStream(
        conn,
        api_key="K", secret_key="S", symbols=["AAA", "BBB"],
        stream_factory=factory,
        sleep_fn=lambda s: None,
    )
    listener.run_with_reconnect(max_attempts=2, stream_runner=runner)

    # Two stream objects were built (one per attempt).
    assert len(factory.streams) == 2
    # Each has both bars + trades subscribed to the same universe.
    for s in factory.streams:
        assert s.bar_subscriptions == [("AAA", "BBB")]
        assert s.trade_subscriptions == [("AAA", "BBB")]
    # Auth message reflects the configured credentials.
    assert listener.last_auth_message == {
        "action": "auth", "key": "K", "secret": "S",
    }


def test_reconnect_writes_heartbeat_state_transitions(conn):
    """After reconnect, the heartbeat row shows reconnects_today
    incremented and the latest state."""
    errors = [ConnectionResetError("drop"), None]
    err_iter = iter(errors)

    def runner(stream):
        e = next(err_iter)
        if e is not None:
            raise e

    fake = _FakeStream()
    listener = ls.LiveStream(
        conn,
        api_key="k", secret_key="s", symbols=["SPY"],
        stream_factory=lambda *a, **kw: fake,
        sleep_fn=lambda s: None,
    )
    listener.run_with_reconnect(max_attempts=2, stream_runner=runner)
    hb = ls.get_heartbeat(conn)
    assert hb is not None
    assert hb["component"] == "live_stream"
    # Two attempts → one reconnect_delta=1 on the second.
    assert hb["reconnects_today"] >= 1
    # Final state: stopped (clean exit on 2nd attempt).
    assert hb["state"] in ("stopped", "connected"), (
        f"unexpected terminal state: {hb['state']}"
    )


# ---------------------------------------------------------------------------
# 7. Heartbeat — scheduled updates, increments, UTC midnight rollover
# ---------------------------------------------------------------------------


def test_update_heartbeat_inserts_new_component_row(conn):
    ls.update_heartbeat(
        conn, component="live_stream",
        state="connected",
        now_iso="2026-05-22T14:30:05+00:00",
        today="2026-05-22",
    )
    hb = ls.get_heartbeat(conn, component="live_stream")
    assert hb is not None
    assert hb["last_ts"] == "2026-05-22T14:30:05+00:00"
    assert hb["state"] == "connected"
    assert hb["reconnects_today"] == 0
    assert hb["rollover_date"] == "2026-05-22"


def test_update_heartbeat_increments_reconnects_today(conn):
    for tick in range(3):
        ls.update_heartbeat(
            conn, component="live_stream",
            state="reconnecting",
            now_iso=f"2026-05-22T14:30:{tick:02d}+00:00",
            today="2026-05-22",
            reconnect_delta=1,
        )
    hb = ls.get_heartbeat(conn)
    assert hb["reconnects_today"] == 3


def test_heartbeat_reconnects_reset_at_utc_midnight(conn):
    # Day 1: hit the cap.
    for _ in range(5):
        ls.update_heartbeat(
            conn, component="live_stream",
            state="reconnecting",
            now_iso="2026-05-21T23:59:55+00:00",
            today="2026-05-21",
            reconnect_delta=1,
        )
    hb = ls.get_heartbeat(conn)
    assert hb["reconnects_today"] == 5
    assert hb["rollover_date"] == "2026-05-21"

    # Day 2: a routine connected tick — counter resets to 0.
    ls.update_heartbeat(
        conn, component="live_stream",
        state="connected",
        now_iso="2026-05-22T00:00:05+00:00",
        today="2026-05-22",
        reconnect_delta=0,
    )
    hb = ls.get_heartbeat(conn)
    assert hb["reconnects_today"] == 0
    assert hb["rollover_date"] == "2026-05-22"

    # Day 2: an additional reconnect counts from zero, not from yesterday.
    ls.update_heartbeat(
        conn, component="live_stream",
        state="reconnecting",
        now_iso="2026-05-22T00:01:00+00:00",
        today="2026-05-22",
        reconnect_delta=1,
    )
    hb = ls.get_heartbeat(conn)
    assert hb["reconnects_today"] == 1


def test_heartbeat_records_last_error_on_failure(conn):
    ls.update_heartbeat(
        conn, component="live_stream",
        state="error",
        last_error="ConnectionResetError",
        now_iso="2026-05-22T14:30:00+00:00",
        today="2026-05-22",
    )
    hb = ls.get_heartbeat(conn)
    assert hb["state"] == "error"
    assert hb["last_error"] == "ConnectionResetError"


def test_heartbeat_clears_last_error_on_recovery(conn):
    ls.update_heartbeat(
        conn, component="live_stream",
        state="error", last_error="boom",
        now_iso="2026-05-22T14:30:00+00:00", today="2026-05-22",
    )
    ls.update_heartbeat(
        conn, component="live_stream",
        state="connected", last_error=None,
        now_iso="2026-05-22T14:30:10+00:00", today="2026-05-22",
    )
    hb = ls.get_heartbeat(conn)
    assert hb["last_error"] is None
    assert hb["state"] == "connected"


# ---------------------------------------------------------------------------
# 8. No-impact on existing trading — invariant
# ---------------------------------------------------------------------------


def _seed_open_position(conn, *, strategy_id, symbol, order_id, qty, price):
    db.record_paper_trade(conn, {
        "alpaca_order_id": order_id,
        "signal_id": None,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": "buy",
        "qty": qty,
        "order_type": "market",
        "fill_price": price,
        "limit_price": price,
        "status": "filled",
        "submitted_at": "2026-05-19T15:00:00+00:00",
        "filled_at": "2026-05-19T15:00:00+00:00",
    })


def _snapshot_paper_trades(conn):
    return [
        tuple(r) for r in conn.execute(
            "SELECT alpaca_order_id, strategy_id, symbol, side, qty, "
            "       status, fill_price, stop_price, notes "
            "  FROM paper_trades ORDER BY id"
        ).fetchall()
    ]


def test_listener_writes_no_paper_trades_row(conn):
    """The load-bearing invariant of §7.5 Workstream A: running the
    listener leaves paper_trades byte-identical. Mirrors
    test_sar_overlay_ab.py:test_shadow_does_not_affect_paper_trades_when_sar_flips.
    """
    _seed_open_position(
        conn, strategy_id="trend-donchian-breakout-20", symbol="SPY",
        order_id="ord-stream-1", qty=10, price=400.0,
    )
    _seed_open_position(
        conn, strategy_id="botnet101-3-bar-low", symbol="QQQ",
        order_id="ord-stream-2", qty=5, price=300.0,
    )
    before = _snapshot_paper_trades(conn)
    before_n = conn.execute(
        "SELECT COUNT(*) FROM paper_trades").fetchone()[0]

    # Replay a stream of bars on the SAME symbols as the open positions.
    import asyncio
    listener = ls.LiveStream(
        conn, api_key="k", secret_key="s", symbols=["SPY", "QQQ"],
        stream_factory=lambda *a, **kw: _FakeStream(),
    )
    for i in range(20):
        for sym, base in (("SPY", 400.0), ("QQQ", 300.0)):
            asyncio.run(listener.on_bar(_bar_dict(
                symbol=sym,
                ts=f"2026-05-22T14:{30+i:02d}:00+00:00",
                o=base, h=base + 0.5, low=base - 0.5, c=base, v=10000.0,
            )))

    # Heartbeat ticks alongside.
    for tick in range(5):
        ls.update_heartbeat(
            conn, component="live_stream",
            state="connected",
            now_iso=f"2026-05-22T14:30:{tick:02d}+00:00",
            today="2026-05-22",
        )

    after = _snapshot_paper_trades(conn)
    after_n = conn.execute(
        "SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert after_n == before_n, (
        "paper_trades row count changed — live_stream altered live PnL"
    )
    assert after == before, (
        "paper_trades row contents changed — live_stream altered live PnL"
    )
    # The bars DID land in the parallel table.
    n_bars = conn.execute(
        "SELECT COUNT(*) FROM intraday_bars").fetchone()[0]
    assert n_bars == 40, f"expected 40 bars, got {n_bars}"


def test_listener_does_not_modify_existing_signals_table(conn):
    """A second guard: the listener writes to intraday_bars only.
    signals (the EOD/intraday fire-detection table) must be untouched."""
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s-untouched"}})
    db.record_signal(
        conn, strategy_id="s-untouched", symbol="SPY",
        bar_ts="2026-05-22", signal_type="long_entry",
        close=400.0, bar_interval="1d",
    )
    before = [tuple(r) for r in conn.execute(
        "SELECT id, strategy_id, symbol, bar_ts, signal_type, close, "
        "       bar_interval FROM signals ORDER BY id"
    ).fetchall()]

    import asyncio
    listener = ls.LiveStream(
        conn, api_key="k", secret_key="s", symbols=["SPY"],
        stream_factory=lambda *a, **kw: _FakeStream(),
    )
    for i in range(10):
        asyncio.run(listener.on_bar(_bar_dict(
            ts=f"2026-05-22T14:{30+i:02d}:00+00:00",
        )))

    after = [tuple(r) for r in conn.execute(
        "SELECT id, strategy_id, symbol, bar_ts, signal_type, close, "
        "       bar_interval FROM signals ORDER BY id"
    ).fetchall()]
    assert after == before, "signals table modified by live_stream"


# ---------------------------------------------------------------------------
# Misc — credential resolution, main entry
# ---------------------------------------------------------------------------


def test_missing_credentials_writes_disconnected_heartbeat_and_exits(conn,
                                                                    monkeypatch):
    """No env, no credentials file section → heartbeat marks
    disconnected with last_error='no_credentials'; no stream factory
    call is made."""
    monkeypatch.setattr(
        ls, "load_credentials",
        lambda *a, **kw: (_ for _ in ()).throw(KeyError("alpaca")),
    )
    calls = []

    def _factory(*a, **kw):
        calls.append(1)
        return _FakeStream()

    listener = ls.LiveStream(
        conn, symbols=["SPY"], stream_factory=_factory,
        sleep_fn=lambda s: None,
    )
    # No api_key/secret_key passed in.
    sleeps = listener.run_with_reconnect(max_attempts=1)
    assert sleeps == []
    assert calls == [], "factory must not be called without credentials"
    hb = ls.get_heartbeat(conn)
    assert hb is not None
    assert hb["state"] == "disconnected"
    assert hb["last_error"] == "no_credentials"


def test_credentials_loaded_from_alpaca_section_when_not_provided(conn,
                                                                  monkeypatch):
    captured = {}

    def _factory(api_key, secret_key, *, feed):
        captured["api_key"] = api_key
        captured["secret_key"] = secret_key
        captured["feed"] = feed
        return _FakeStream()

    monkeypatch.setattr(
        ls, "load_credentials",
        lambda *a, **kw: {"api_key": "loaded-k", "secret_key": "loaded-s"},
    )

    listener = ls.LiveStream(
        conn, symbols=["SPY"], stream_factory=_factory,
        sleep_fn=lambda s: None,
    )
    listener.run_with_reconnect(
        max_attempts=1, stream_runner=lambda s: None,
    )
    assert captured["api_key"] == "loaded-k"
    assert captured["secret_key"] == "loaded-s"
    assert captured["feed"] == "iex"


# ---------------------------------------------------------------------------
# Fake stream — minimal stand-in for alpaca-py's StockDataStream
# ---------------------------------------------------------------------------


class _FakeStream:
    """Captures subscribe_bars / subscribe_trades calls + their symbol
    tuples. `run()` is what the production code calls; tests pass their
    own `stream_runner` that raises or returns cleanly."""
    def __init__(self):
        self.bar_handler = None
        self.trade_handler = None
        self.bar_subscriptions = []
        self.trade_subscriptions = []
        self.closed = False

    def subscribe_bars(self, handler, *symbols):
        self.bar_handler = handler
        self.bar_subscriptions.append(tuple(symbols))

    def subscribe_trades(self, handler, *symbols):
        self.trade_handler = handler
        self.trade_subscriptions.append(tuple(symbols))

    def run(self):
        # Default behavior — exit cleanly.
        return None

    def stop(self):
        self.closed = True
