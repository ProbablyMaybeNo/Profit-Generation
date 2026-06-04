"""
live_stream.py — 7.5.1 Alpaca IEX WebSocket listener + minute-bar storage.

Long-running listener that subscribes to Alpaca's free IEX WebSocket bars
channel for the TRACKED_STOCKS + TRACKED_SECTORS universe (10 symbols)
and upserts every bar into `intraday_bars`. Writes a heartbeat row to
`stream_heartbeat` every 5 seconds. Reconnects on socket drop with
exponential backoff (1s, 2s, 4s, 8s, max 60s).

Architecture: AUGMENT, NOT REPLACE. The existing 15m polling loop in
`monitoring/intraday_monitor.py` and the daily strategies in
`monitoring/strategy_fires.py` are untouched. This module is pure data
ingestion — no new condition in auto_trader.py, no flag wiring, no
behavior change on existing trades. Workstream B (7.5.5+) will be the
first consumer of these rows.

Network seam: `_make_stream()` builds the underlying `StockDataStream`
from `alpaca-py`. Tests inject a fake stream via the factory parameter
to LiveStream so the unit suite never opens a real socket.

Auth + subscribe wire format: alpaca-py handles those internally, but
the message shapes are exposed here as `build_auth_message()` and
`build_subscribe_message()` so the tests can pin them. Bar parsing
(`parse_bar`) and the upsert (`upsert_bar`) live independently of the
SDK so each is testable on its own.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.utils import load_credentials, log  # noqa: E402
from monitoring.config import (  # noqa: E402
    TRACKED_STOCKS, TRACKED_SECTORS, INTRADAY_1M_UNIVERSE,
)


COMPONENT = "live_stream"
DEFAULT_FEED = "iex"
DEFAULT_SOURCE = "iex"
HEARTBEAT_INTERVAL_SEC = 5.0
BACKOFF_SCHEDULE = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
BACKOFF_CAP_SEC = 60.0


def _dedupe_upper(*groups) -> List[str]:
    """Union of symbol groups, upper-cased, first-seen order preserved."""
    seen: Dict[str, None] = {}
    for group in groups:
        for sym in group:
            if sym is None:
                continue
            key = str(sym).upper()
            if key not in seen:
                seen[key] = None
    return list(seen.keys())


# A4 (audit 2026-06-03): the bars table fed MFE/MAE and the F2-SAFETY stale
# sweep, but only 10 of the 20-symbol intraday universe ever got bars (the
# other 10 — AAPL, MSFT, NVDA, etc. — had ZERO rows, so their orphans never
# auto-closed and excursion stayed NULL). The persisted universe must equal
# the configured intraday strategy universe, so the listener subscribes to
# the full set. IEX bar bandwidth for 20 names is trivial.
DEFAULT_UNIVERSE: List[str] = _dedupe_upper(
    TRACKED_STOCKS, TRACKED_SECTORS, INTRADAY_1M_UNIVERSE,
)


# ---------------------------------------------------------------------------
# Wire-format helpers (Alpaca v2 stream protocol)
# ---------------------------------------------------------------------------

def build_auth_message(api_key: str, secret_key: str) -> Dict[str, str]:
    """The Alpaca v2 stream auth handshake payload.

    Mirrors what alpaca-py sends internally — exposed here so tests can
    pin the shape without having to dig into the SDK.
    """
    return {"action": "auth", "key": api_key, "secret": secret_key}


def build_subscribe_message(
    symbols: Sequence[str],
    *,
    bars: bool = True,
    trades: bool = True,
) -> Dict[str, Any]:
    """The Alpaca v2 stream channel subscribe payload."""
    msg: Dict[str, Any] = {"action": "subscribe"}
    sym_list = [s.upper() for s in symbols]
    if bars:
        msg["bars"] = list(sym_list)
    if trades:
        msg["trades"] = list(sym_list)
    return msg


def compute_backoff(attempt: int) -> float:
    """Exponential backoff with a 60-second ceiling. attempt is 1-indexed
    (1 → 1s, 2 → 2s, 3 → 4s, 4 → 8s, ... capped at 60s)."""
    if attempt <= 0:
        return 0.0
    delay = 2 ** (attempt - 1)
    return float(min(delay, BACKOFF_CAP_SEC))


# ---------------------------------------------------------------------------
# Bar parsing + upsert
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _to_iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            return s
    return None


def parse_bar(msg: Any) -> Optional[Dict[str, Any]]:
    """Normalize a raw Alpaca bar message (or alpaca-py Bar model) into
    the row shape we persist. Returns None when the message isn't a bar
    or is missing required fields.

    Accepts both the dict shape (``{T:"b", S:"SPY", o, h, l, c, v, t}``)
    and the alpaca-py ``Bar`` model (with ``symbol``, ``open``, ``high``,
    ``low``, ``close``, ``volume``, ``timestamp``).
    """
    if msg is None:
        return None

    if isinstance(msg, dict):
        if msg.get("T") not in (None, "b"):
            return None
        symbol = msg.get("S") or msg.get("symbol")
        ts = msg.get("t") or msg.get("timestamp")
        o = msg.get("o", msg.get("open"))
        h = msg.get("h", msg.get("high"))
        low = msg.get("l", msg.get("low"))
        c = msg.get("c", msg.get("close"))
        v = msg.get("v", msg.get("volume"))
    else:
        symbol = getattr(msg, "symbol", None)
        ts = getattr(msg, "timestamp", None)
        o = getattr(msg, "open", None)
        h = getattr(msg, "high", None)
        low = getattr(msg, "low", None)
        c = getattr(msg, "close", None)
        v = getattr(msg, "volume", None)

    if not symbol or ts is None:
        return None

    return {
        "symbol": str(symbol).upper(),
        "ts_utc": _to_iso(ts),
        "open": _safe_float(o),
        "high": _safe_float(h),
        "low": _safe_float(low),
        "close": _safe_float(c),
        "volume": _safe_float(v),
    }


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def upsert_bar(
    conn: sqlite3.Connection,
    bar: Dict[str, Any],
    *,
    source: str = DEFAULT_SOURCE,
    recorded_at: Optional[str] = None,
) -> Optional[int]:
    """Insert one bar row. Idempotent on (symbol, ts_utc, source) — a
    duplicate bar (Alpaca occasionally re-sends) is a no-op.

    Returns the row id on insert, None on duplicate.
    """
    if not bar or not bar.get("symbol") or not bar.get("ts_utc"):
        return None
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO intraday_bars "
            "  (symbol, ts_utc, open, high, low, close, volume, source, "
            "   recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bar["symbol"],
                bar["ts_utc"],
                bar.get("open"),
                bar.get("high"),
                bar.get("low"),
                bar.get("close"),
                bar.get("volume"),
                source,
                recorded_at or _utc_now_iso(),
            ),
        )
        return cur.lastrowid if cur.rowcount else None


def upsert_bars(
    conn: sqlite3.Connection,
    bars: Iterable[Dict[str, Any]],
    *,
    source: str = DEFAULT_SOURCE,
) -> int:
    """Bulk upsert. Returns the count of newly-inserted rows."""
    n = 0
    for b in bars:
        if upsert_bar(conn, b, source=source) is not None:
            n += 1
    return n


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def update_heartbeat(
    conn: sqlite3.Connection,
    *,
    component: str = COMPONENT,
    state: str = "connected",
    last_error: Optional[str] = None,
    now_iso: Optional[str] = None,
    today: Optional[str] = None,
    reconnect_delta: int = 0,
) -> None:
    """Upsert the single heartbeat row for `component`.

    Semantics:
      - `state` set verbatim.
      - `last_error` set verbatim (callers pass None to clear it on a
        successful tick, or pass an error string on failure).
      - `reconnect_delta` is the additional count to add to today's
        reconnect total. Tests pass `reconnect_delta=1` on each
        simulated drop.
      - When the UTC date rolls over, `reconnects_today` resets to
        `reconnect_delta` (typically 0 on a routine tick) and
        `rollover_date` is updated.
    """
    now_iso = now_iso or _utc_now_iso()
    today = today or _utc_today()
    existing = conn.execute(
        "SELECT reconnects_today, rollover_date FROM stream_heartbeat "
        " WHERE component=?",
        (component,),
    ).fetchone()

    if existing is None:
        with conn:
            conn.execute(
                "INSERT INTO stream_heartbeat "
                "  (component, last_ts, reconnects_today, last_error, "
                "   rollover_date, state) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (component, now_iso, int(reconnect_delta), last_error,
                 today, state),
            )
        return

    prev_rollover = existing["rollover_date"]
    if prev_rollover != today:
        new_total = int(reconnect_delta)
    else:
        new_total = int(existing["reconnects_today"]) + int(reconnect_delta)

    with conn:
        conn.execute(
            "UPDATE stream_heartbeat "
            "   SET last_ts=?, reconnects_today=?, last_error=?, "
            "       rollover_date=?, state=? "
            " WHERE component=?",
            (now_iso, new_total, last_error, today, state, component),
        )


def get_heartbeat(
    conn: sqlite3.Connection, *, component: str = COMPONENT,
) -> Optional[Dict[str, Any]]:
    """Return the heartbeat row as a dict, or None when unset."""
    row = conn.execute(
        "SELECT component, last_ts, reconnects_today, last_error, "
        "       rollover_date, state "
        "  FROM stream_heartbeat WHERE component=?",
        (component,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Stream factory + LiveStream wrapper
# ---------------------------------------------------------------------------

def _make_stream(
    api_key: str, secret_key: str, *, feed: str = DEFAULT_FEED,
):
    """Build the underlying alpaca-py StockDataStream. Lazy import so
    test paths that inject a fake never touch alpaca-py."""
    from alpaca.data.live import StockDataStream  # type: ignore
    from alpaca.data.enums import DataFeed  # type: ignore

    feed_enum = DataFeed.IEX if feed.lower() == "iex" else DataFeed.SIP
    return StockDataStream(
        api_key=api_key,
        secret_key=secret_key,
        feed=feed_enum,
    )


class LiveStream:
    """Wraps an Alpaca StockDataStream, handling bar persistence,
    heartbeat updates, and reconnect-with-backoff. Designed so the
    unit suite can drive it via a fake stream factory — the real
    alpaca-py code path is exercised by an actual production run
    (no live tests in the unit suite).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        symbols: Optional[Sequence[str]] = None,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        feed: str = DEFAULT_FEED,
        source: str = DEFAULT_SOURCE,
        stream_factory: Optional[Callable[..., Any]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC,
    ) -> None:
        self.conn = conn
        self.symbols: List[str] = [s.upper() for s in (symbols or DEFAULT_UNIVERSE)]
        self.api_key = api_key
        self.secret_key = secret_key
        self.feed = feed
        self.source = source
        self._stream_factory = stream_factory or _make_stream
        self._sleep = sleep_fn
        self._heartbeat_interval_sec = float(heartbeat_interval_sec)
        self._stop_event = threading.Event()
        self._reconnects_observed = 0
        # Recorded for inspection by tests:
        self.last_subscribe_message: Optional[Dict[str, Any]] = None
        self.last_auth_message: Optional[Dict[str, str]] = None

    # ---- handler wrappers ------------------------------------------------

    async def on_bar(self, msg: Any) -> None:
        """Bar handler — alpaca-py awaits this on every bar."""
        bar = parse_bar(msg)
        if bar is None:
            return
        upsert_bar(self.conn, bar, source=self.source)

    async def on_trade(self, msg: Any) -> None:
        """Trade handler — we subscribe but don't persist trades in 7.5.1.
        Workstream B (7.5.5+) may consume the firehose for VWAP /
        rvol computation. For now, swallowing keeps the channel open."""
        return None

    # ---- credentials -----------------------------------------------------

    def _resolve_credentials(self) -> bool:
        if self.api_key and self.secret_key:
            return True
        try:
            section = load_credentials("alpaca")
        except Exception as exc:
            log(f"live_stream: credentials load failed: {type(exc).__name__}",
                level="ERROR")
            return False
        if not isinstance(section, dict):
            return False
        self.api_key = self.api_key or section.get("api_key")
        self.secret_key = self.secret_key or section.get("secret_key")
        return bool(self.api_key and self.secret_key)

    # ---- subscribe -------------------------------------------------------

    def _build_handshake_messages(self) -> None:
        """Capture the auth + subscribe messages for the heartbeat /
        tests. Doesn't actually send anything — alpaca-py does the
        sending internally. We pin the shape so any drift between our
        contract docs and the SDK is observable from the unit suite."""
        self.last_auth_message = build_auth_message(
            self.api_key or "", self.secret_key or "")
        self.last_subscribe_message = build_subscribe_message(
            self.symbols, bars=True, trades=True,
        )

    def _attach_handlers(self, stream: Any) -> None:
        """Register on_bar / on_trade callbacks on the underlying stream."""
        if hasattr(stream, "subscribe_bars"):
            stream.subscribe_bars(self.on_bar, *self.symbols)
        if hasattr(stream, "subscribe_trades"):
            stream.subscribe_trades(self.on_trade, *self.symbols)

    # ---- heartbeat thread ------------------------------------------------

    def heartbeat_tick(
        self,
        *,
        state: str = "connected",
        last_error: Optional[str] = None,
        reconnect_delta: int = 0,
    ) -> None:
        update_heartbeat(
            self.conn,
            component=COMPONENT,
            state=state,
            last_error=last_error,
            reconnect_delta=reconnect_delta,
        )

    # ---- reconnect loop --------------------------------------------------

    def stop(self) -> None:
        self._stop_event.set()

    def run_with_reconnect(
        self,
        *,
        max_attempts: Optional[int] = None,
        stream_runner: Optional[Callable[[Any], None]] = None,
    ) -> List[float]:
        """Run the stream, reconnecting on any exception with the
        documented backoff schedule. Returns the list of sleep durations
        observed (in test) so unit tests can assert the schedule.

        Each iteration:
          1. Build a fresh stream via the factory (so a torn socket
             gets a clean object).
          2. Capture handshake messages for the dashboard / tests.
          3. Attach bar + trade handlers.
          4. Invoke `stream_runner(stream)` (defaults to
             ``stream.run()``); if it raises, sleep ``compute_backoff(n)``
             and try again.
          5. Heartbeat once per iteration entry; mark `reconnect_delta=1`
             on every retry.

        `max_attempts` caps the loop for test invocations. Production
        callers leave it None and rely on `stop()` to exit.
        """
        observed_sleeps: List[float] = []
        if not self._resolve_credentials():
            self.heartbeat_tick(state="disconnected", last_error="no_credentials")
            return observed_sleeps

        runner = stream_runner or (lambda s: s.run())
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            reconnect_delta = 0 if attempt == 1 else 1
            try:
                stream = self._stream_factory(
                    self.api_key, self.secret_key, feed=self.feed,
                )
            except Exception as exc:
                self.heartbeat_tick(
                    state="error",
                    last_error=f"factory_error:{type(exc).__name__}",
                    reconnect_delta=reconnect_delta,
                )
                if max_attempts is not None and attempt >= max_attempts:
                    break
                delay = compute_backoff(attempt)
                observed_sleeps.append(delay)
                self._sleep(delay)
                continue

            self._build_handshake_messages()
            try:
                self._attach_handlers(stream)
            except Exception as exc:
                self.heartbeat_tick(
                    state="error",
                    last_error=f"attach_error:{type(exc).__name__}",
                    reconnect_delta=reconnect_delta,
                )
                if max_attempts is not None and attempt >= max_attempts:
                    break
                delay = compute_backoff(attempt)
                observed_sleeps.append(delay)
                self._sleep(delay)
                continue

            self.heartbeat_tick(
                state="connected", last_error=None,
                reconnect_delta=reconnect_delta,
            )
            if reconnect_delta:
                self._reconnects_observed += 1

            try:
                runner(stream)
            except KeyboardInterrupt:
                self.heartbeat_tick(state="stopped", last_error=None)
                break
            except Exception as exc:
                self.heartbeat_tick(
                    state="reconnecting",
                    last_error=f"{type(exc).__name__}:{exc}",
                    reconnect_delta=0,
                )
                if max_attempts is not None and attempt >= max_attempts:
                    break
                delay = compute_backoff(attempt + 1)
                observed_sleeps.append(delay)
                self._sleep(delay)
                continue
            else:
                self.heartbeat_tick(state="stopped", last_error=None)
                break

            if max_attempts is not None and attempt >= max_attempts:
                break

        return observed_sleeps


# ---------------------------------------------------------------------------
# Entry point — used by schedulers/run_live_stream.bat
# ---------------------------------------------------------------------------

def main() -> int:
    from data import db as _db

    conn = _db.init_db()
    listener = LiveStream(conn)
    log(f"live_stream: starting on {len(listener.symbols)} symbols "
        f"({','.join(listener.symbols)})", level="INFO")
    try:
        listener.run_with_reconnect()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
