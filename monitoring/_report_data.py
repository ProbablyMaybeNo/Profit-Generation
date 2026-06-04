"""
_report_data.py — Shared SQL query helpers for daily_brief and daily_analysis.

Both reports query the same tables; this module centralises the SQL so
there is no duplication. All functions accept a sqlite3.Connection and a
target date (str ISO YYYY-MM-DD or datetime.date) and return plain dicts /
lists of dicts that are safe to serialise.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]


def _d(as_of) -> str:
    if isinstance(as_of, date):
        return as_of.isoformat()
    return str(as_of)[:10]


# ---------------------------------------------------------------------------
# Section 1 — Header: portfolio / regime
# ---------------------------------------------------------------------------

def get_header(conn: sqlite3.Connection, as_of) -> Dict:
    day = _d(as_of)

    regime_row = conn.execute(
        "SELECT market_regime FROM daily_reports WHERE report_date = ?",
        (day,),
    ).fetchone()
    regime = regime_row["market_regime"] if regime_row else "unknown"

    # Today's latest equity snapshot
    today_snap = conn.execute(
        """SELECT portfolio_value, cash, buying_power
           FROM equity_snapshots
           WHERE date(recorded_at) = ?
           ORDER BY recorded_at DESC LIMIT 1""",
        (day,),
    ).fetchone()

    # Prior trading-day snapshot (last record BEFORE today)
    prior_snap = conn.execute(
        """SELECT portfolio_value
           FROM equity_snapshots
           WHERE date(recorded_at) < ?
           ORDER BY recorded_at DESC LIMIT 1""",
        (day,),
    ).fetchone()

    portfolio_value = float(today_snap["portfolio_value"]) if today_snap else None
    cash = float(today_snap["cash"]) if today_snap else None
    buying_power = float(today_snap["buying_power"]) if today_snap else None

    prior_value = float(prior_snap["portfolio_value"]) if prior_snap else None

    if portfolio_value is not None and prior_value is not None and prior_value > 0:
        day_pnl_usd = portfolio_value - prior_value
        day_pnl_pct = day_pnl_usd / prior_value * 100.0
    else:
        day_pnl_usd = None
        day_pnl_pct = None

    # % capital deployed = equity in positions / portfolio_value
    deployed_pct = None
    if portfolio_value is not None and cash is not None and portfolio_value > 0:
        deployed_pct = max(0.0, (portfolio_value - cash) / portfolio_value * 100.0)

    return {
        "date": day,
        "market_regime": regime,
        "portfolio_value": portfolio_value,
        "cash": cash,
        "buying_power": buying_power,
        "prior_value": prior_value,
        "day_pnl_usd": day_pnl_usd,
        "day_pnl_pct": day_pnl_pct,
        "deployed_pct": deployed_pct,
    }


# ---------------------------------------------------------------------------
# Section 2 — System activity
# ---------------------------------------------------------------------------

def get_activity(conn: sqlite3.Connection, as_of) -> Dict:
    day = _d(as_of)

    # Signals fired today by strategy + bar_interval
    sig_rows = conn.execute(
        """SELECT strategy_id, bar_interval, COUNT(*) as cnt
           FROM signals
           WHERE date(bar_ts) = ?
           GROUP BY strategy_id, bar_interval
           ORDER BY cnt DESC""",
        (day,),
    ).fetchall()
    signals_by_strat = [dict(r) for r in sig_rows]

    # Orders submitted today
    orders_rows = conn.execute(
        """SELECT side, status, COUNT(*) as cnt
           FROM paper_trades
           WHERE date(submitted_at) = ?
           GROUP BY side, status
           ORDER BY side, status""",
        (day,),
    ).fetchall()
    orders_summary = [dict(r) for r in orders_rows]

    total_submitted = sum(r["cnt"] for r in orders_summary)
    total_filled = sum(r["cnt"] for r in orders_summary if r["status"] == "filled")
    total_buys = sum(r["cnt"] for r in orders_summary if r["side"] == "buy")
    total_sells = sum(r["cnt"] for r in orders_summary if r["side"] == "sell")

    return {
        "signals_by_strat": signals_by_strat,
        "orders_summary": orders_summary,
        "total_submitted": total_submitted,
        "total_filled": total_filled,
        "total_buys": total_buys,
        "total_sells": total_sells,
    }


# ---------------------------------------------------------------------------
# Section 3 — Trades (entries / exits), swing vs intraday
# ---------------------------------------------------------------------------

INTRADAY_INTERVALS = {"1m", "5m", "15m", "1d-intraday"}


def get_trades(conn: sqlite3.Connection, as_of) -> Dict:
    day = _d(as_of)

    rows = conn.execute(
        """SELECT pt.symbol, pt.side, pt.qty, pt.fill_price, pt.status,
                  pt.strategy_id, pt.submitted_at, pt.filled_at,
                  s.bar_interval
           FROM paper_trades pt
           LEFT JOIN signals s ON s.id = pt.signal_id
           WHERE date(pt.submitted_at) = ?
             AND pt.status = 'filled'
           ORDER BY pt.filled_at""",
        (day,),
    ).fetchall()

    entries_swing, entries_intra, exits_swing, exits_intra = [], [], [], []
    for r in rows:
        d = dict(r)
        interval = d.get("bar_interval") or "1d"
        is_intra = interval in INTRADAY_INTERVALS
        if d["side"] == "buy":
            (entries_intra if is_intra else entries_swing).append(d)
        else:
            (exits_intra if is_intra else exits_swing).append(d)

    return {
        "entries_swing": entries_swing,
        "entries_intraday": entries_intra,
        "exits_swing": exits_swing,
        "exits_intraday": exits_intra,
    }


# ---------------------------------------------------------------------------
# Section 4 — Intraday day-trades by strategy
# ---------------------------------------------------------------------------

def get_intraday_by_strategy(conn: sqlite3.Connection, as_of) -> Dict:
    day = _d(as_of)

    rows = conn.execute(
        """SELECT pt.strategy_id, pt.symbol, pt.side, pt.fill_price,
                  pt.qty, pt.filled_at, s.bar_interval
           FROM paper_trades pt
           LEFT JOIN signals s ON s.id = pt.signal_id
           WHERE date(pt.submitted_at) = ?
             AND pt.status = 'filled'
             AND s.bar_interval IN ('1m','5m','15m','1d-intraday')
           ORDER BY pt.strategy_id, pt.symbol, pt.filled_at""",
        (day,),
    ).fetchall()

    by_strat: Dict[str, List] = {}
    for r in rows:
        d = dict(r)
        sid = d["strategy_id"]
        by_strat.setdefault(sid, []).append(d)

    return {"intraday_by_strategy": by_strat}


# ---------------------------------------------------------------------------
# Section 5 — Risk mechanics
# ---------------------------------------------------------------------------

def get_risk(conn: sqlite3.Connection, as_of) -> Dict:
    day = _d(as_of)

    # ATR stops attached (entry_stops not null/empty for today's buys)
    stops_rows = conn.execute(
        """SELECT pt.symbol, pt.strategy_id, pt.entry_stops
           FROM paper_trades pt
           WHERE date(pt.submitted_at) = ?
             AND pt.side = 'buy'
             AND pt.entry_stops IS NOT NULL
             AND pt.entry_stops != ''
             AND pt.entry_stops != 'null'
           ORDER BY pt.submitted_at""",
        (day,),
    ).fetchall()
    atr_stops = [dict(r) for r in stops_rows]

    # Trailing stops currently armed
    trail_rows = conn.execute(
        """SELECT strategy_id, symbol, side, method,
                  stop_price, extreme_price, updated_at
           FROM trailing_stops
           ORDER BY updated_at DESC""",
    ).fetchall()
    trailing = [dict(r) for r in trail_rows]

    # Pyramids today
    pyramid_rows = conn.execute(
        """SELECT pt.symbol, pt.strategy_id, pt.pyramid_tier, pt.fill_price
           FROM paper_trades pt
           WHERE date(pt.submitted_at) = ?
             AND pt.pyramid_tier > 0
             AND pt.status = 'filled'
           ORDER BY pt.pyramid_tier""",
        (day,),
    ).fetchall()
    pyramids = [dict(r) for r in pyramid_rows]

    # Pyramid skips
    pyra_skip_rows = conn.execute(
        """SELECT strategy_id, symbol, gate, COUNT(*) as cnt
           FROM intraday_skips
           WHERE date(recorded_at) = ?
             AND gate LIKE '%pyramid%'
           GROUP BY strategy_id, symbol, gate
           ORDER BY cnt DESC""",
        (day,),
    ).fetchall()
    pyramid_skips = [dict(r) for r in pyra_skip_rows]

    # Stop exits today
    stop_exits = conn.execute(
        """SELECT o.exit_reason, s.strategy_id, s.symbol, o.return_pct
           FROM outcomes o
           JOIN signals s ON s.id = o.signal_id
           WHERE date(o.exit_ts) = ?
             AND o.exit_reason IN ('stop_loss_atr','trailing_stop','stop_loss')
           ORDER BY o.exit_ts""",
        (day,),
    ).fetchall()
    stop_exits = [dict(r) for r in stop_exits]

    return {
        "atr_stops_count": len(atr_stops),
        "atr_stops": atr_stops,
        "trailing_stops": trailing,
        "pyramids": pyramids,
        "pyramid_skips": pyramid_skips,
        "stop_exits": stop_exits,
    }


# ---------------------------------------------------------------------------
# Section 6 — Outcomes
# ---------------------------------------------------------------------------

def get_outcomes(conn: sqlite3.Connection, as_of) -> Dict:
    day = _d(as_of)

    # Closed today by exit_reason, avg return
    closed_rows = conn.execute(
        """SELECT o.exit_reason, COUNT(*) as cnt,
                  AVG(o.return_pct) as avg_ret,
                  SUM(CASE WHEN o.return_pct > 0 THEN 1 ELSE 0 END) as wins
           FROM outcomes o
           WHERE date(o.exit_ts) = ?
             AND o.status = 'closed'
           GROUP BY o.exit_reason
           ORDER BY cnt DESC""",
        (day,),
    ).fetchall()
    closed_today = [dict(r) for r in closed_rows]

    # Open positions count
    open_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM outcomes WHERE status = 'open'"
    ).fetchone()["cnt"]

    # MFE/MAE coverage
    mfe_cov = conn.execute(
        """SELECT
             COUNT(*) as total,
             SUM(CASE WHEN mfe_pct IS NOT NULL THEN 1 ELSE 0 END) as has_mfe,
             SUM(CASE WHEN mae_pct IS NOT NULL THEN 1 ELSE 0 END) as has_mae
           FROM outcomes
           WHERE status = 'closed'"""
    ).fetchone()
    mfe_coverage = dict(mfe_cov)

    return {
        "closed_today": closed_today,
        "open_positions": open_count,
        "mfe_coverage": mfe_coverage,
    }


# ---------------------------------------------------------------------------
# Section 7 — Notable (news, macro, movers)
# ---------------------------------------------------------------------------

def get_notable(conn: sqlite3.Connection, as_of) -> Dict:
    day = _d(as_of)

    # News headlines today
    news_rows = conn.execute(
        """SELECT symbol, title, published_utc, publisher
           FROM news
           WHERE date(published_utc) = ?
           ORDER BY published_utc DESC
           LIMIT 10""",
        (day,),
    ).fetchall()
    news = [dict(r) for r in news_rows]

    # Macro context — latest values
    macro_rows = conn.execute(
        """SELECT series_id, bar_date, value
           FROM macro
           WHERE bar_date <= ?
           GROUP BY series_id
           HAVING bar_date = MAX(bar_date)
           ORDER BY series_id""",
        (day,),
    ).fetchall()
    macro = [dict(r) for r in macro_rows]

    # Biggest movers from snapshots
    mover_rows = conn.execute(
        """SELECT symbol, asset_class, close, ret_1d_pct, ret_5d_pct,
                  rvol_vs_20d, dist_sma20_pct
           FROM snapshots
           WHERE snapshot_date = ?
           ORDER BY ABS(COALESCE(ret_1d_pct, 0)) DESC
           LIMIT 10""",
        (day,),
    ).fetchall()
    movers = [dict(r) for r in mover_rows]

    return {
        "news": news,
        "macro": macro,
        "movers": movers,
    }


# ---------------------------------------------------------------------------
# Analysis extras — logs, skip distribution, strategy performance
# ---------------------------------------------------------------------------

def get_recent_errors(log_dir: Path, max_lines: int = 80) -> List[str]:
    """Pull ERROR/WARNING lines from schtask_run_*.log files (last N lines each)."""
    pattern = re.compile(r"\b(ERROR|WARNING|Exception|Traceback|CRITICAL)\b",
                         re.IGNORECASE)
    results: List[str] = []
    for lf in sorted(log_dir.glob("schtask_run_*.log")):
        try:
            text = lf.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            # Take the last 300 lines of each file
            tail = lines[-300:]
            for line in tail:
                if pattern.search(line):
                    results.append(f"[{lf.name}] {line.strip()}")
        except Exception:
            pass
    return results[-max_lines:]


def get_skip_distribution(conn: sqlite3.Connection, days: int = 5) -> List[Dict]:
    rows = conn.execute(
        """SELECT date(recorded_at) as day, gate, COUNT(*) as cnt
           FROM intraday_skips
           WHERE date(recorded_at) >= date('now', ?)
           GROUP BY day, gate
           ORDER BY day DESC, cnt DESC""",
        (f"-{days} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def get_strategy_performance(conn: sqlite3.Connection, lookback: int = 30) -> List[Dict]:
    rows = conn.execute(
        """SELECT s.strategy_id,
                  COUNT(*) as trades,
                  SUM(CASE WHEN o.return_pct > 0 THEN 1 ELSE 0 END) as wins,
                  ROUND(AVG(o.return_pct), 4) as avg_ret_pct,
                  ROUND(MIN(o.return_pct), 4) as worst,
                  ROUND(MAX(o.return_pct), 4) as best
           FROM outcomes o
           JOIN signals s ON s.id = o.signal_id
           WHERE o.status = 'closed'
             AND date(o.exit_ts) >= date('now', ?)
           GROUP BY s.strategy_id
           ORDER BY avg_ret_pct DESC""",
        (f"-{lookback} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def get_open_vs_broker_note(conn: sqlite3.Connection) -> Dict:
    open_outcomes = conn.execute(
        """SELECT s.strategy_id, s.symbol, o.entry_ts, o.entry_price
           FROM outcomes o
           JOIN signals s ON s.id = o.signal_id
           WHERE o.status = 'open'
           ORDER BY o.entry_ts DESC
           LIMIT 20"""
    ).fetchall()
    open_trades = conn.execute(
        """SELECT strategy_id, symbol, side, fill_price, filled_at
           FROM paper_trades
           WHERE status = 'filled'
             AND side = 'buy'
           ORDER BY filled_at DESC
           LIMIT 20"""
    ).fetchall()
    return {
        "open_outcomes": [dict(r) for r in open_outcomes],
        "filled_buys": [dict(r) for r in open_trades],
    }
