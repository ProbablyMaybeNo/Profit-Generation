"""
db.py — SQLite-backed persistence layer for the trading system.

Single source of truth for: strategy registry, signals (any granularity),
daily snapshots, daily-report metadata, news, outcomes, paper trades, patterns.

Stdlib-only. WAL mode, foreign keys ON, conservative indexes. Idempotent
init_db() — safe to call from any entry point.

The cache file (data/cache.db) is unrelated and stays separate.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DB_FILE = Path(__file__).resolve().parent / "trading.db"

SCHEMA_VERSION = "5"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection with WAL mode + foreign keys + row factory."""
    path = Path(db_path) if db_path is not None else DB_FILE
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


_DDL = [
    """
    CREATE TABLE IF NOT EXISTS strategies (
        strategy_id              TEXT PRIMARY KEY,
        title                    TEXT,
        author                   TEXT,
        methodology_family       TEXT,
        current_verdict          TEXT,
        verdict_summary          TEXT,
        entry_rules              TEXT,
        exit_rules               TEXT,
        risk_management          TEXT,
        instruments_json         TEXT,
        timeframes_json          TEXT,
        core_concepts_json       TEXT,
        failure_modes_json       TEXT,
        improvement_hypotheses_json TEXT,
        code_paths_json          TEXT,
        data_artifacts_json      TEXT,
        active_on_json           TEXT,
        compute_fn               TEXT,
        first_logged_iso         TEXT,
        last_updated_iso         TEXT,
        source_url               TEXT,
        tags_json                TEXT,
        raw_record_json          TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,
        bar_ts          TEXT NOT NULL,
        bar_interval    TEXT NOT NULL DEFAULT '1d',
        strategy_id     TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        signal_type     TEXT NOT NULL,
        close           REAL,
        extra_json      TEXT,
        UNIQUE(strategy_id, symbol, bar_ts, bar_interval, signal_type),
        FOREIGN KEY(strategy_id) REFERENCES strategies(strategy_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_symbol_bar ON signals(symbol, bar_ts)",
    "CREATE INDEX IF NOT EXISTS idx_signals_strategy_bar ON signals(strategy_id, bar_ts)",
    "CREATE INDEX IF NOT EXISTS idx_signals_bar_ts ON signals(bar_ts)",
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_date    TEXT NOT NULL,
        symbol           TEXT NOT NULL,
        asset_class      TEXT,
        bar_date         TEXT,
        close            REAL,
        ret_1d_pct       REAL,
        ret_5d_pct       REAL,
        ret_20d_pct      REAL,
        rvol_vs_20d      REAL,
        dist_sma20_pct   REAL,
        error            TEXT,
        PRIMARY KEY(snapshot_date, symbol)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_date ON snapshots(symbol, snapshot_date)",
    """
    CREATE TABLE IF NOT EXISTS daily_reports (
        report_date           TEXT PRIMARY KEY,
        market_regime         TEXT,
        importance            INTEGER,
        has_notable_pattern   INTEGER NOT NULL DEFAULT 0,
        fires_count           INTEGER NOT NULL DEFAULT 0,
        watchlist_count       INTEGER NOT NULL DEFAULT 0,
        notable_movers_count  INTEGER NOT NULL DEFAULT 0,
        tags_json             TEXT,
        symbols_watched_json  TEXT,
        notion_page_id        TEXT,
        markdown              TEXT,
        generated_at          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        polygon_id      TEXT,
        fetched_at      TEXT NOT NULL,
        published_utc   TEXT NOT NULL,
        symbol          TEXT,
        title           TEXT NOT NULL,
        url             TEXT,
        author          TEXT,
        publisher       TEXT,
        description     TEXT,
        tickers_json    TEXT,
        keywords_json   TEXT,
        sentiment       TEXT,
        UNIQUE(polygon_id, symbol)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_news_symbol_pub ON news(symbol, published_utc)",
    "CREATE INDEX IF NOT EXISTS idx_news_pub ON news(published_utc)",
    """
    CREATE TABLE IF NOT EXISTS outcomes (
        signal_id     INTEGER PRIMARY KEY,
        entry_ts      TEXT,
        entry_price   REAL,
        exit_ts       TEXT,
        exit_price    REAL,
        exit_reason   TEXT,
        return_pct    REAL,
        mfe_pct       REAL,
        mae_pct       REAL,
        bars_held     INTEGER,
        status        TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        FOREIGN KEY(signal_id) REFERENCES signals(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outcomes_status ON outcomes(status)",
    """
    CREATE TABLE IF NOT EXISTS paper_trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        alpaca_order_id TEXT UNIQUE,
        signal_id       INTEGER,
        strategy_id     TEXT,
        symbol          TEXT,
        side            TEXT,
        qty             REAL,
        order_type      TEXT,
        limit_price     REAL,
        stop_price      REAL,
        submitted_at    TEXT,
        filled_at       TEXT,
        fill_price      REAL,
        status          TEXT,
        notes           TEXT,
        pyramid_tier    INTEGER,
        FOREIGN KEY(signal_id) REFERENCES signals(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol)",
    """
    CREATE TABLE IF NOT EXISTS patterns (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        description     TEXT,
        importance      INTEGER,
        status          TEXT,
        observed_count  INTEGER NOT NULL DEFAULT 1,
        first_observed  TEXT,
        last_observed   TEXT,
        notion_page_id  TEXT,
        tags_json       TEXT,
        UNIQUE(name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS earnings (
        symbol         TEXT NOT NULL,
        earnings_date  TEXT NOT NULL,
        source         TEXT,
        fetched_at     TEXT NOT NULL,
        PRIMARY KEY(symbol, earnings_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_earnings_symbol_date ON earnings(symbol, earnings_date)",
    """
    CREATE TABLE IF NOT EXISTS macro (
        series_id   TEXT NOT NULL,
        bar_date    TEXT NOT NULL,
        value       REAL,
        fetched_at  TEXT NOT NULL,
        PRIMARY KEY(series_id, bar_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_macro_series_date ON macro(series_id, bar_date)",
    """
    CREATE TABLE IF NOT EXISTS meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity_snapshots (
        recorded_at      TEXT NOT NULL,
        portfolio_value  REAL NOT NULL,
        cash             REAL,
        equity           REAL,
        buying_power     REAL,
        source           TEXT,
        PRIMARY KEY(recorded_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_equity_snapshots_at ON equity_snapshots(recorded_at)",
    """
    CREATE TABLE IF NOT EXISTS paused_strategies (
        strategy_id   TEXT PRIMARY KEY,
        reason        TEXT,
        paused_at     TEXT NOT NULL,
        expires_at    TEXT,
        source        TEXT,
        live_mean_pct REAL,
        backtest_mean_pct REAL,
        sample_size   INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paused_strategies_expires "
    "ON paused_strategies(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS api_spend (
        provider     TEXT NOT NULL,
        spend_date   TEXT NOT NULL,
        spend_usd    REAL NOT NULL DEFAULT 0.0,
        calls        INTEGER NOT NULL DEFAULT 0,
        updated_at   TEXT NOT NULL,
        PRIMARY KEY(provider, spend_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_spend_date ON api_spend(spend_date)",
    """
    CREATE TABLE IF NOT EXISTS trailing_stops (
        strategy_id   TEXT NOT NULL,
        symbol        TEXT NOT NULL,
        side          TEXT NOT NULL DEFAULT 'long',
        method        TEXT NOT NULL,
        stop_price    REAL NOT NULL,
        extreme_price REAL NOT NULL,
        updated_at    TEXT NOT NULL,
        PRIMARY KEY(strategy_id, symbol)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trailing_stops_symbol "
    "ON trailing_stops(symbol)",
    """
    CREATE TABLE IF NOT EXISTS liquidity_snapshots (
        symbol                  TEXT PRIMARY KEY,
        as_of_date              TEXT NOT NULL,
        avg_dollar_volume_20d   REAL NOT NULL,
        last_close              REAL,
        updated_at              TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_liquidity_snapshots_date "
    "ON liquidity_snapshots(as_of_date)",
    # 6.4.2 — shadow A/B record. Captures what an exit would have looked
    # like if SAR overlay had been firing on the strategy, WITHOUT changing
    # the real paper_trades or live exit decision. One row per shadow exit
    # event (a SAR flip on an open position). Compared against the real
    # exit reason / price in paper_trades by (strategy_id, symbol,
    # entry_order_id).
    """
    CREATE TABLE IF NOT EXISTS paper_trades_sar_overlay (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at       TEXT NOT NULL,
        strategy_id       TEXT NOT NULL,
        symbol            TEXT NOT NULL,
        side              TEXT NOT NULL DEFAULT 'long',
        entry_order_id    TEXT,
        entry_price       REAL,
        qty               REAL,
        shadow_exit_price REAL NOT NULL,
        shadow_sar        REAL,
        shadow_reason     TEXT NOT NULL,
        real_exit_price   REAL,
        real_exit_reason  TEXT,
        shadow_pnl        REAL,
        real_pnl          REAL,
        notes             TEXT,
        UNIQUE(strategy_id, symbol, entry_order_id, recorded_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_sar_overlay_strategy "
    "ON paper_trades_sar_overlay(strategy_id)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_sar_overlay_symbol "
    "ON paper_trades_sar_overlay(symbol)",
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotent column-add migrations for existing DBs. CREATE TABLE
    IF NOT EXISTS is a no-op when the table already exists, so any new
    column needs explicit ALTER TABLE here."""
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(paper_trades)"
    ).fetchall()}
    if "pyramid_tier" not in cols:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN pyramid_tier INTEGER")
    # 6.1.1 — record which initial-stop method protected this entry
    # (atr_initial, fixed_percent, or NULL when no stop was attached).
    if "entry_stops" not in cols:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN entry_stops TEXT")


def init_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Create all tables/indexes if absent. Returns an open connection."""
    conn = connect(db_path)
    with conn:
        for stmt in _DDL:
            conn.execute(stmt)
        _ensure_columns(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
    return conn


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def upsert_strategy(conn: sqlite3.Connection, record: Dict[str, Any]) -> None:
    """
    Insert or update a row in strategies. Accepts both the records.jsonl flat
    dict shape (with 'extra' nested) and the already-flattened shape.
    """
    extra = record.get("extra", {}) if isinstance(record.get("extra"), dict) else {}
    flat = {**extra, **{k: v for k, v in record.items() if k != "extra"}}

    sid = flat.get("strategy_id")
    if not sid:
        raise ValueError("upsert_strategy: record has no strategy_id")

    cols = {
        "strategy_id":              sid,
        "title":                    flat.get("title"),
        "author":                   flat.get("author"),
        "methodology_family":       flat.get("methodology_family"),
        "current_verdict":          flat.get("current_verdict"),
        "verdict_summary":          flat.get("verdict_summary"),
        "entry_rules":              flat.get("entry_rules"),
        "exit_rules":               flat.get("exit_rules"),
        "risk_management":          flat.get("risk_management"),
        "instruments_json":         _dumps(flat.get("instruments")),
        "timeframes_json":          _dumps(flat.get("timeframes")),
        "core_concepts_json":       _dumps(flat.get("core_concepts")),
        "failure_modes_json":       _dumps(flat.get("failure_modes")),
        "improvement_hypotheses_json": _dumps(flat.get("improvement_hypotheses")),
        "code_paths_json":          _dumps(flat.get("code_paths")),
        "data_artifacts_json":      _dumps(flat.get("data_artifacts")),
        "active_on_json":           _dumps(flat.get("active_on")),
        "compute_fn":               flat.get("compute_fn") or flat.get("compute"),
        "first_logged_iso":         flat.get("first_logged_iso"),
        "last_updated_iso":         flat.get("last_updated_iso") or _utc_now_iso(),
        "source_url":               record.get("url") or flat.get("source_url"),
        "tags_json":                _dumps(record.get("tags") or flat.get("tags")),
        "raw_record_json":          _dumps(record),
    }
    placeholders = ", ".join(["?"] * len(cols))
    columns = ", ".join(cols.keys())
    update_clause = ", ".join(f"{k}=excluded.{k}" for k in cols if k != "strategy_id")
    sql = (
        f"INSERT INTO strategies ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(strategy_id) DO UPDATE SET {update_clause}"
    )
    with conn:
        conn.execute(sql, tuple(cols.values()))


def ensure_strategies_seeded(
    conn: sqlite3.Connection,
    tracked_strategies,
    *,
    title_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Make sure every TRACKED_STRATEGIES entry has a row in the strategies
    table. Prevents the FK constraint failure that has bitten us each time
    a new strategy was added without a manual upsert.

    Returns: list of strategy_ids that were newly inserted (existing rows
    are not touched). Safe to call at the start of any pipeline run.
    """
    if not tracked_strategies:
        return []
    existing = {r[0] for r in conn.execute("SELECT strategy_id FROM strategies")}
    today = _utc_now_iso()[:10]
    title_map = title_map or {}
    newly_inserted: List[str] = []
    for entry in tracked_strategies:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id")
        if not sid or sid in existing:
            continue
        cls = entry.get("strategy_class", "uncategorized")
        bar_interval = entry.get("bar_interval", "1d")
        family = f"Auto-seeded · {cls} · {bar_interval}"
        title = title_map.get(sid, sid)
        record = {
            "extra": {
                "strategy_id": sid,
                "title": title,
                "methodology_family": family,
                "current_verdict": "UNTESTED",
                "verdict_summary":
                    f"Auto-seeded {today} by ensure_strategies_seeded; "
                    f"awaiting first closed outcome.",
                "active_on": entry.get("active_on", []),
                "timeframes": {"execution": bar_interval},
                "instruments": entry.get("active_on", []),
                "compute": entry.get("compute", ""),
                "code_paths": {"primitives": entry.get("module", "") + ".py"
                              if entry.get("module") else ""},
                "tags": [cls, bar_interval, "auto-seeded"],
                "first_logged_iso": today,
                "last_updated_iso": today,
            },
        }
        try:
            upsert_strategy(conn, record)
            newly_inserted.append(sid)
        except Exception:
            # Don't kill the pipeline on a bad row — log and skip.
            continue
    return newly_inserted


def set_strategy_active_on(
    conn: sqlite3.Connection, strategy_id: str, symbols: List[str], compute_fn: Optional[str] = None
) -> None:
    """Update only the active_on universe + optional compute_fn for a strategy."""
    fields = ["active_on_json=?", "last_updated_iso=?"]
    params: List[Any] = [_dumps(symbols), _utc_now_iso()]
    if compute_fn is not None:
        fields.append("compute_fn=?")
        params.append(compute_fn)
    params.append(strategy_id)
    with conn:
        conn.execute(
            f"UPDATE strategies SET {', '.join(fields)} WHERE strategy_id=?",
            tuple(params),
        )


def record_signal(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    symbol: str,
    bar_ts: str,
    signal_type: str,
    close: Optional[float] = None,
    bar_interval: str = "1d",
    extra: Optional[Dict[str, Any]] = None,
    ts: Optional[str] = None,
) -> Optional[int]:
    """
    Insert a signal row. Idempotent on
    (strategy_id, symbol, bar_ts, bar_interval, signal_type).
    Returns the row id, or None if the row already existed.
    """
    with conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO signals
                (ts, bar_ts, bar_interval, strategy_id, symbol, signal_type, close, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts or _utc_now_iso(),
                bar_ts,
                bar_interval,
                strategy_id,
                symbol,
                signal_type,
                close,
                _dumps(extra) if extra else None,
            ),
        )
        if cur.rowcount == 0:
            return None
        return cur.lastrowid


def record_snapshot_row(
    conn: sqlite3.Connection, snapshot_date: str, row: Dict[str, Any]
) -> None:
    """Upsert one snapshot row (one symbol on one as_of date)."""
    with conn:
        conn.execute(
            """
            INSERT INTO snapshots
                (snapshot_date, symbol, asset_class, bar_date, close,
                 ret_1d_pct, ret_5d_pct, ret_20d_pct,
                 rvol_vs_20d, dist_sma20_pct, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date, symbol) DO UPDATE SET
                asset_class=excluded.asset_class,
                bar_date=excluded.bar_date,
                close=excluded.close,
                ret_1d_pct=excluded.ret_1d_pct,
                ret_5d_pct=excluded.ret_5d_pct,
                ret_20d_pct=excluded.ret_20d_pct,
                rvol_vs_20d=excluded.rvol_vs_20d,
                dist_sma20_pct=excluded.dist_sma20_pct,
                error=excluded.error
            """,
            (
                snapshot_date,
                row.get("symbol"),
                row.get("asset_class"),
                row.get("bar_date"),
                row.get("close"),
                row.get("ret_1d_pct"),
                row.get("ret_5d_pct"),
                row.get("ret_20d_pct"),
                row.get("rvol_vs_20d"),
                row.get("dist_sma20_pct"),
                row.get("error"),
            ),
        )


def record_daily_report(
    conn: sqlite3.Connection,
    *,
    report_date: str,
    market_regime: str,
    importance: int,
    fires_count: int,
    watchlist_count: int,
    notable_movers_count: int,
    tags: List[str],
    symbols_watched: List[str],
    has_notable_pattern: bool = False,
    notion_page_id: Optional[str] = None,
    markdown: Optional[str] = None,
    force: bool = False,
) -> str:
    """
    Upsert the daily-report metadata row. Returns 'inserted', 'updated',
    or 'skipped_downgrade'.

    Defensive guard: if a row already exists for `report_date` AND the
    incoming `fires_count` or `watchlist_count` is LOWER than the existing
    row's, the write is skipped to protect against transient data-source
    failures (yfinance returning empty frames, partial fetches, etc.).
    Pass `force=True` to bypass.
    """
    existing = conn.execute(
        "SELECT fires_count, watchlist_count FROM daily_reports WHERE report_date=?",
        (report_date,),
    ).fetchone()
    if existing is not None and not force:
        if (int(fires_count) < existing["fires_count"]
                or int(watchlist_count) < existing["watchlist_count"]):
            return "skipped_downgrade"

    with conn:
        conn.execute(
            """
            INSERT INTO daily_reports
                (report_date, market_regime, importance, has_notable_pattern,
                 fires_count, watchlist_count, notable_movers_count,
                 tags_json, symbols_watched_json, notion_page_id, markdown,
                 generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_date) DO UPDATE SET
                market_regime=excluded.market_regime,
                importance=excluded.importance,
                has_notable_pattern=excluded.has_notable_pattern,
                fires_count=excluded.fires_count,
                watchlist_count=excluded.watchlist_count,
                notable_movers_count=excluded.notable_movers_count,
                tags_json=excluded.tags_json,
                symbols_watched_json=excluded.symbols_watched_json,
                notion_page_id=COALESCE(excluded.notion_page_id, daily_reports.notion_page_id),
                markdown=COALESCE(excluded.markdown, daily_reports.markdown),
                generated_at=excluded.generated_at
            """,
            (
                report_date,
                market_regime,
                int(importance),
                1 if has_notable_pattern else 0,
                int(fires_count),
                int(watchlist_count),
                int(notable_movers_count),
                _dumps(tags),
                _dumps(symbols_watched),
                notion_page_id,
                markdown,
                _utc_now_iso(),
            ),
        )
    return "inserted" if existing is None else "updated"


def insert_news(conn: sqlite3.Connection, item: Dict[str, Any]) -> Optional[int]:
    """
    Insert one news item. Idempotent on (polygon_id, symbol).
    Returns inserted id, or None if it was a duplicate.
    """
    with conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO news
                (polygon_id, fetched_at, published_utc, symbol, title, url,
                 author, publisher, description, tickers_json, keywords_json, sentiment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.get("polygon_id") or item.get("id"),
                item.get("fetched_at") or _utc_now_iso(),
                item.get("published_utc"),
                item.get("symbol"),
                item.get("title"),
                item.get("url") or item.get("article_url"),
                item.get("author"),
                item.get("publisher"),
                item.get("description"),
                _dumps(item.get("tickers")),
                _dumps(item.get("keywords")),
                _dumps(item.get("insights")) if item.get("insights") is not None else item.get("sentiment"),
            ),
        )
        if cur.rowcount == 0:
            return None
        return cur.lastrowid


def open_outcome(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    entry_ts: str,
    entry_price: float,
) -> None:
    """Open a tracking outcome for a signal. Idempotent — second call updates."""
    with conn:
        conn.execute(
            """
            INSERT INTO outcomes
                (signal_id, entry_ts, entry_price, status, updated_at)
            VALUES (?, ?, ?, 'open', ?)
            ON CONFLICT(signal_id) DO UPDATE SET
                entry_ts=excluded.entry_ts,
                entry_price=excluded.entry_price,
                updated_at=excluded.updated_at
            """,
            (signal_id, entry_ts, entry_price, _utc_now_iso()),
        )


def close_outcome(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    exit_ts: str,
    exit_price: float,
    exit_reason: str,
    bars_held: Optional[int] = None,
    mfe_pct: Optional[float] = None,
    mae_pct: Optional[float] = None,
) -> None:
    """Mark an outcome closed and compute return_pct from entry/exit."""
    with conn:
        row = conn.execute(
            "SELECT entry_price FROM outcomes WHERE signal_id=?", (signal_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"close_outcome: no open outcome for signal_id={signal_id}")
        entry_price = row["entry_price"]
        return_pct = None
        if entry_price not in (None, 0):
            return_pct = (exit_price - entry_price) / entry_price * 100.0
        conn.execute(
            """
            UPDATE outcomes
               SET exit_ts=?, exit_price=?, exit_reason=?, bars_held=?,
                   mfe_pct=COALESCE(?, mfe_pct),
                   mae_pct=COALESCE(?, mae_pct),
                   return_pct=?, status='closed', updated_at=?
             WHERE signal_id=?
            """,
            (exit_ts, exit_price, exit_reason, bars_held, mfe_pct, mae_pct,
             return_pct, _utc_now_iso(), signal_id),
        )


def _normalize_order_status(value: Any) -> Optional[str]:
    """Coerce an alpaca-py OrderStatus enum (or str repr like
    'OrderStatus.ACCEPTED') down to the bare lowercase token ('accepted')
    that every paper_trades read path expects.

    Why: auto_trader stores `str(order.status)` which yields the enum's
    repr, not its .value. That broke reconcile and every open-position
    query (they filter for lowercase strings).
    """
    if value is None:
        return None
    v = getattr(value, "value", value)
    s = str(v).strip()
    if "." in s:
        head, _, tail = s.rpartition(".")
        if head.endswith("Status"):
            s = tail
    return s.lower() or None


def record_paper_trade(conn: sqlite3.Connection, trade: Dict[str, Any]) -> Optional[int]:
    """Upsert a paper trade keyed by alpaca_order_id."""
    trade = {**trade, "status": _normalize_order_status(trade.get("status"))}
    with conn:
        cur = conn.execute(
            """
            INSERT INTO paper_trades
                (alpaca_order_id, signal_id, strategy_id, symbol, side, qty,
                 order_type, limit_price, stop_price, submitted_at,
                 filled_at, fill_price, status, notes, entry_stops)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alpaca_order_id) DO UPDATE SET
                signal_id=COALESCE(excluded.signal_id, paper_trades.signal_id),
                strategy_id=COALESCE(excluded.strategy_id, paper_trades.strategy_id),
                symbol=excluded.symbol,
                side=excluded.side,
                qty=excluded.qty,
                order_type=excluded.order_type,
                limit_price=excluded.limit_price,
                stop_price=excluded.stop_price,
                submitted_at=COALESCE(paper_trades.submitted_at, excluded.submitted_at),
                filled_at=COALESCE(excluded.filled_at, paper_trades.filled_at),
                fill_price=COALESCE(excluded.fill_price, paper_trades.fill_price),
                status=excluded.status,
                notes=COALESCE(excluded.notes, paper_trades.notes),
                entry_stops=COALESCE(excluded.entry_stops, paper_trades.entry_stops)
            """,
            (
                trade.get("alpaca_order_id"),
                trade.get("signal_id"),
                trade.get("strategy_id"),
                trade.get("symbol"),
                trade.get("side"),
                trade.get("qty"),
                trade.get("order_type"),
                trade.get("limit_price"),
                trade.get("stop_price"),
                trade.get("submitted_at"),
                trade.get("filled_at"),
                trade.get("fill_price"),
                trade.get("status"),
                trade.get("notes"),
                trade.get("entry_stops"),
            ),
        )
        return cur.lastrowid if cur.rowcount else None


def upsert_earnings_date(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    earnings_date: str,
    source: Optional[str] = None,
) -> Optional[int]:
    """Insert an earnings date. Idempotent on (symbol, earnings_date).

    Returns 1 if the row was newly inserted, 0 if it already existed.
    """
    if not symbol or not earnings_date:
        return 0
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO earnings (symbol, earnings_date, source, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (symbol, earnings_date, source, _utc_now_iso()),
        )
        if cur.rowcount == 0:
            return 0
    return 1


def next_earnings_date_on_or_after(
    conn: sqlite3.Connection, symbol: str, asof_iso: str,
) -> Optional[str]:
    """Return the earliest earnings_date for `symbol` on or after `asof_iso`,
    or None when no future event is recorded."""
    row = conn.execute(
        "SELECT earnings_date FROM earnings "
        " WHERE symbol=? AND earnings_date >= ? "
        " ORDER BY earnings_date ASC LIMIT 1",
        (symbol, asof_iso),
    ).fetchone()
    return row["earnings_date"] if row else None


def upsert_macro_value(
    conn: sqlite3.Connection,
    *,
    series_id: str,
    bar_date: str,
    value: Optional[float],
) -> Optional[int]:
    """Insert or update one macro datapoint. Idempotent on (series_id, bar_date).

    Returns 1 if the row changed (insert or value update), 0 if it was a no-op.
    NaN / None values silently skipped — the macro table is "last known good".
    """
    if value is None:
        return 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v != v:  # NaN
        return 0
    existing = conn.execute(
        "SELECT value FROM macro WHERE series_id=? AND bar_date=?",
        (series_id, bar_date),
    ).fetchone()
    if existing is not None and existing["value"] == v:
        return 0
    with conn:
        conn.execute(
            "INSERT INTO macro (series_id, bar_date, value, fetched_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(series_id, bar_date) DO UPDATE SET "
            "    value=excluded.value, fetched_at=excluded.fetched_at",
            (series_id, bar_date, v, _utc_now_iso()),
        )
    return 1


def latest_macro_value(
    conn: sqlite3.Connection, series_id: str
) -> Optional[sqlite3.Row]:
    """Return the most recent (bar_date, value, fetched_at) row for a series, or None."""
    return conn.execute(
        "SELECT series_id, bar_date, value, fetched_at FROM macro "
        " WHERE series_id=? AND value IS NOT NULL "
        " ORDER BY bar_date DESC LIMIT 1",
        (series_id,),
    ).fetchone()


def upsert_pattern(conn: sqlite3.Connection, pattern: Dict[str, Any]) -> int:
    """Upsert a pattern keyed by name. Increments observed_count on conflict."""
    now = _utc_now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO patterns
                (name, description, importance, status, observed_count,
                 first_observed, last_observed, notion_page_id, tags_json)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description=COALESCE(excluded.description, patterns.description),
                importance=COALESCE(excluded.importance, patterns.importance),
                status=COALESCE(excluded.status, patterns.status),
                observed_count=patterns.observed_count + 1,
                last_observed=excluded.last_observed,
                notion_page_id=COALESCE(excluded.notion_page_id, patterns.notion_page_id),
                tags_json=COALESCE(excluded.tags_json, patterns.tags_json)
            """,
            (
                pattern["name"],
                pattern.get("description"),
                pattern.get("importance"),
                pattern.get("status", "active"),
                pattern.get("first_observed", now),
                pattern.get("last_observed", now),
                pattern.get("notion_page_id"),
                _dumps(pattern.get("tags")),
            ),
        )
        row = conn.execute("SELECT id FROM patterns WHERE name=?", (pattern["name"],)).fetchone()
        return int(row["id"])


def query_recent_signals(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    symbol: Optional[str] = None,
    strategy_id: Optional[str] = None,
    signal_type: Optional[str] = None,
) -> List[sqlite3.Row]:
    where: List[str] = []
    params: List[Any] = []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if strategy_id:
        where.append("strategy_id = ?")
        params.append(strategy_id)
    if signal_type:
        where.append("signal_type = ?")
        params.append(signal_type)
    sql = "SELECT * FROM signals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY bar_ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return list(conn.execute(sql, tuple(params)).fetchall())


def query_recent_news(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    symbol: Optional[str] = None,
) -> List[sqlite3.Row]:
    sql = "SELECT * FROM news"
    params: List[Any] = []
    if symbol:
        sql += " WHERE symbol = ?"
        params.append(symbol)
    sql += " ORDER BY published_utc DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return list(conn.execute(sql, tuple(params)).fetchall())


def get_strategy(conn: sqlite3.Connection, strategy_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strategies WHERE strategy_id = ?", (strategy_id,)
    ).fetchone()


def list_strategies(
    conn: sqlite3.Connection, verdicts: Optional[Iterable[str]] = None
) -> List[sqlite3.Row]:
    if verdicts is None:
        return list(conn.execute("SELECT * FROM strategies ORDER BY strategy_id").fetchall())
    placeholders = ", ".join(["?"] * len(list(verdicts)))
    return list(conn.execute(
        f"SELECT * FROM strategies WHERE current_verdict IN ({placeholders}) ORDER BY strategy_id",
        tuple(verdicts),
    ).fetchall())


def record_equity_snapshot(
    conn: sqlite3.Connection,
    *,
    portfolio_value: float,
    cash: Optional[float] = None,
    equity: Optional[float] = None,
    buying_power: Optional[float] = None,
    source: str = "auto_trader",
    recorded_at: Optional[str] = None,
) -> None:
    """Append (or replace by timestamp) one equity snapshot row."""
    ts = recorded_at or _utc_now_iso()
    with conn:
        conn.execute(
            "INSERT INTO equity_snapshots(recorded_at, portfolio_value, cash, "
            "  equity, buying_power, source) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(recorded_at) DO UPDATE SET "
            "  portfolio_value=excluded.portfolio_value, "
            "  cash=excluded.cash, equity=excluded.equity, "
            "  buying_power=excluded.buying_power, source=excluded.source",
            (ts, float(portfolio_value),
             cash if cash is None else float(cash),
             equity if equity is None else float(equity),
             buying_power if buying_power is None else float(buying_power),
             source),
        )


def trailing_peak_portfolio_value(
    conn: sqlite3.Connection, *, window_days: int = 30,
    asof: Optional[str] = None,
) -> Optional[float]:
    """Max portfolio_value across equity_snapshots rows in the last
    `window_days`. Returns None when no rows exist in window."""
    asof = asof or _utc_now_iso()
    cutoff = (datetime.fromisoformat(asof.replace("Z", "+00:00"))
               - timedelta(days=window_days)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT MAX(portfolio_value) AS peak FROM equity_snapshots "
        " WHERE recorded_at >= ? AND recorded_at <= ?",
        (cutoff, asof),
    ).fetchone()
    if row is None or row["peak"] is None:
        return None
    return float(row["peak"])


def upsert_liquidity_snapshot(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    as_of_date: str,
    avg_dollar_volume_20d: float,
    last_close: Optional[float] = None,
) -> None:
    """Insert / replace a per-symbol liquidity snapshot (5.5.2.1)."""
    with conn:
        conn.execute(
            "INSERT INTO liquidity_snapshots(symbol, as_of_date, "
            "  avg_dollar_volume_20d, last_close, updated_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  as_of_date=excluded.as_of_date, "
            "  avg_dollar_volume_20d=excluded.avg_dollar_volume_20d, "
            "  last_close=excluded.last_close, "
            "  updated_at=excluded.updated_at",
            (
                symbol.upper(),
                as_of_date,
                float(avg_dollar_volume_20d),
                None if last_close is None else float(last_close),
                _utc_now_iso(),
            ),
        )


def get_liquidity_snapshots(
    conn: sqlite3.Connection,
    symbols: Optional[Iterable[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return {symbol: {as_of_date, avg_dollar_volume_20d, last_close, updated_at}}.

    If `symbols` is None, returns the full table.
    """
    if symbols is None:
        rows = conn.execute(
            "SELECT symbol, as_of_date, avg_dollar_volume_20d, last_close, "
            "       updated_at FROM liquidity_snapshots"
        ).fetchall()
    else:
        upper = [s.upper() for s in symbols]
        if not upper:
            return {}
        placeholders = ",".join(["?"] * len(upper))
        rows = conn.execute(
            f"SELECT symbol, as_of_date, avg_dollar_volume_20d, last_close, "
            f"       updated_at FROM liquidity_snapshots "
            f" WHERE symbol IN ({placeholders})",
            upper,
        ).fetchall()
    return {
        row["symbol"]: {
            "as_of_date": row["as_of_date"],
            "avg_dollar_volume_20d": row["avg_dollar_volume_20d"],
            "last_close": row["last_close"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    }


if __name__ == "__main__":
    conn = init_db()
    n_strats = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    n_sigs = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    n_snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    n_reports = conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0]
    n_news = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    print(f"DB initialized at {DB_FILE}")
    print(f"  schema_version = {SCHEMA_VERSION}")
    print(f"  strategies={n_strats}  signals={n_sigs}  snapshots={n_snaps} "
          f"daily_reports={n_reports}  news={n_news}")
    conn.close()
