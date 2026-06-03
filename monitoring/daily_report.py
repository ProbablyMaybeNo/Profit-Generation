"""
daily_report.py — Generate today's daily trading report.

Pulls the snapshot of tracked symbols + checks tracked strategies for fires +
formats a structured markdown report ready to post to Notion (or stash locally).

Usage:
  python -m monitoring.daily_report                  # today
  python -m monitoring.daily_report 2026-04-24       # specific date
  python -m monitoring.daily_report 2026-04-24 -o /path/to/out.md
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring.config import (
    TRACKED_STOCKS, TRACKED_SECTORS, TRACKED_CRYPTO,
    NOTION_DAILY_REPORTS_DB_ID,
)
from monitoring.movers import snapshot, classify_market_regime
from monitoring.strategy_fires import check_fires
from monitoring import news_fetcher
from monitoring import outcome_tracker
from data import db


@dataclass
class DailyReport:
    report_date: date
    market_regime: str
    snapshot_rows: List[Dict] = field(default_factory=list)
    fires: List[Dict] = field(default_factory=list)
    exit_signals: List[Dict] = field(default_factory=list)
    notable_movers: List[Dict] = field(default_factory=list)
    importance: int = 1
    has_notable_pattern: bool = False
    tags: List[str] = field(default_factory=list)
    symbols_watched: List[str] = field(default_factory=list)
    news_by_symbol: Dict[str, List[Dict]] = field(default_factory=dict)


def _is_notable_mover(row: Dict) -> bool:
    """Mark as notable if 1-day move > 2% absolute or rvol > 1.5x."""
    r1d = row.get("ret_1d_pct")
    rvol = row.get("rvol_vs_20d")
    if r1d is not None and abs(r1d) >= 2.0:
        return True
    if rvol is not None and rvol >= 1.5:
        return True
    return False


def _news_metrics(report: DailyReport) -> Dict[str, int]:
    """Count news context that should influence importance / tags.

    `negative_on_fires` / `positive_on_fires` count distinct fired SYMBOLS
    with at least one matching-sentiment item — not per-item.
    """
    fired_syms = {f["symbol"] for f in report.fires}
    neg_syms: set = set()
    pos_syms: set = set()
    total_news = 0
    for sym, items in (report.news_by_symbol or {}).items():
        for item in items or []:
            total_news += 1
            if sym not in fired_syms:
                continue
            insights = item.get("insights")
            if not isinstance(insights, list):
                continue
            for ins in insights:
                if not isinstance(ins, dict) or ins.get("ticker") != sym:
                    continue
                sentiment = (ins.get("sentiment") or "").lower()
                if sentiment == "negative":
                    neg_syms.add(sym)
                elif sentiment == "positive":
                    pos_syms.add(sym)
    return {
        "negative_on_fires": len(neg_syms),
        "positive_on_fires": len(pos_syms),
        "total_news": total_news,
    }


def _compute_importance(report: DailyReport, news: Optional[Dict] = None) -> int:
    """1-5 score based on signal density + news context (mean-rev fires INTO bad news = riskier)."""
    score = 1
    if len(report.fires) >= 1:
        score = max(score, 2)
    if len(report.fires) >= 3:
        score = max(score, 3)
    if len(report.fires) >= 6:
        score = max(score, 4)
    if any(abs(m.get("ret_1d_pct", 0)) >= 5.0 for m in report.notable_movers):
        score = max(score, 4)
    if news and news.get("negative_on_fires", 0) >= 1:
        score = min(5, score + 1)
    if report.has_notable_pattern:
        score = 5
    return score


def _derive_tags(report: DailyReport, news: Optional[Dict] = None) -> List[str]:
    tags: List[str] = []
    big_up = sum(1 for m in report.snapshot_rows if (m.get("ret_1d_pct") or 0) >= 2.0)
    big_down = sum(1 for m in report.snapshot_rows if (m.get("ret_1d_pct") or 0) <= -2.0)
    if big_up >= 3:
        tags.append("gap-up")
    if big_down >= 3:
        tags.append("gap-down")
    high_vol = sum(1 for m in report.snapshot_rows if (m.get("rvol_vs_20d") or 0) >= 1.5)
    low_vol = sum(1 for m in report.snapshot_rows if (m.get("rvol_vs_20d") or 0) <= 0.5)
    if high_vol >= 3:
        tags.append("high-volume")
    elif low_vol >= 5:
        tags.append("low-volume")
    if news:
        if news.get("negative_on_fires", 0) >= 1:
            tags.append("against-news")
        if news.get("total_news", 0) >= 15:
            tags.append("news-heavy")
    return tags


def finalize_report(report: DailyReport) -> Dict[str, int]:
    """Compute tags + importance from current report state including news. Returns the metrics."""
    news = _news_metrics(report)
    report.tags = _derive_tags(report, news)
    report.importance = _compute_importance(report, news)
    return news


def build_report(as_of: date) -> DailyReport:
    snap = snapshot(as_of)
    regime = classify_market_regime(snap)
    all_fire_rows = check_fires(as_of)
    fires = [f for f in all_fire_rows if f.get("fired")]
    exit_only = [f for f in all_fire_rows
                 if f.get("long_exit_signal") and not f.get("fired")]
    notable = [r for r in snap if _is_notable_mover(r)]

    report = DailyReport(
        report_date=as_of,
        market_regime=regime,
        snapshot_rows=snap,
        fires=fires,
        exit_signals=exit_only,
        notable_movers=notable,
        symbols_watched=[r["symbol"] for r in snap if "error" not in r],
    )
    finalize_report(report)
    return report


def _prioritized_symbols(report: DailyReport) -> List[str]:
    """Order: fired symbols first, then notable movers, then the rest of the watchlist."""
    seen: List[str] = []
    def _add(sym: str):
        if sym and sym not in seen:
            seen.append(sym)
    for f in report.fires:
        _add(f.get("symbol"))
    for m in sorted(report.notable_movers,
                    key=lambda x: abs(x.get("ret_1d_pct", 0)), reverse=True):
        _add(m.get("symbol"))
    for s in report.symbols_watched:
        _add(s)
    return seen


def gather_news(report: DailyReport, *, limit: int = 5, max_age_hours: int = 168) -> None:
    """Fetch + persist news for the report's universe; stash on the report."""
    try:
        symbols = _prioritized_symbols(report)
        if not symbols:
            return
        report.news_by_symbol = news_fetcher.fetch_and_persist_for_universe(
            symbols, limit=limit, max_age_hours=max_age_hours,
        )
    except Exception as e:
        from config.utils import log
        log(f"gather_news failed (non-fatal): {e}", "WARNING")


def _summarize_symbol_context(report: DailyReport, sym: str) -> str:
    parts: List[str] = []
    fires_for = [f["strategy_id"] for f in report.fires if f["symbol"] == sym]
    if fires_for:
        parts.append("fire: " + ", ".join(fires_for))
    mover = next((m for m in report.notable_movers if m["symbol"] == sym), None)
    if mover is not None:
        parts.append(f"mover {mover.get('ret_1d_pct', 0):+.2f}%")
    return " · ".join(parts)


def render_markdown(report: DailyReport) -> str:
    lines: List[str] = []
    lines.append(f"# Trading Daily Report — {report.report_date.isoformat()}")
    lines.append("")
    lines.append(f"**Market regime:** {report.market_regime}  •  "
                 f"**Importance:** {report.importance}/5  •  "
                 f"**Strategy fires:** {len(report.fires)}  •  "
                 f"**Notable movers:** {len(report.notable_movers)}")
    if report.tags:
        lines.append(f"**Tags:** {', '.join(report.tags)}")
    lines.append("")

    lines.append("## Strategy fires today")
    if not report.fires:
        lines.append("_No tracked strategy fired a long_entry signal today._")
    else:
        lines.append("| Strategy | Symbol | Close | Bar Date |")
        lines.append("|---|---|---|---|")
        for f in report.fires:
            lines.append(f"| `{f['strategy_id']}` | {f['symbol']} | "
                         f"${f.get('close', 0):.2f} | {f.get('bar_date', '?')} |")
    lines.append("")

    lines.append("## Notable movers")
    if not report.notable_movers:
        lines.append("_No symbol moved >=2% or had RVol >=1.5x today._")
    else:
        lines.append("| Symbol | Class | 1d % | 5d % | 20d % | RVol | Δ vs SMA20 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in sorted(report.notable_movers, key=lambda x: abs(x.get("ret_1d_pct", 0)), reverse=True):
            lines.append(
                f"| {r['symbol']} | {r['asset_class']} | "
                f"{r.get('ret_1d_pct', 0):+.2f}% | "
                f"{r.get('ret_5d_pct', 0):+.2f}% | "
                f"{r.get('ret_20d_pct', 0):+.2f}% | "
                f"{r.get('rvol_vs_20d') or 0:.2f}x | "
                f"{r.get('dist_sma20_pct') or 0:+.2f}% |"
            )
    lines.append("")

    lines.append("## Recent news")
    news_symbols = [s for s in _prioritized_symbols(report)
                    if report.news_by_symbol.get(s)]
    if not news_symbols:
        lines.append("_No headlines fetched (Polygon news disabled, "
                     "rate-limited, or no recent items for tracked tickers)._")
    else:
        for sym in news_symbols[:8]:
            ctx = _summarize_symbol_context(report, sym)
            header = f"**{sym}**"
            if ctx:
                header += f" — {ctx}"
            lines.append(header)
            for item in report.news_by_symbol[sym][:3]:
                pub = (item.get("published_utc") or "")[:16].replace("T", " ")
                pubr = item.get("publisher") or "?"
                title = item.get("title") or "(no title)"
                url = item.get("url")
                if url:
                    lines.append(f"- [{pub}] {pubr} — [{title}]({url})")
                else:
                    lines.append(f"- [{pub}] {pubr} — {title}")
            lines.append("")
    lines.append("")

    lines.append("## Full snapshot")
    lines.append("| Symbol | Class | Close | 1d % | 5d % | 20d % | RVol | Δ vs SMA20 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in report.snapshot_rows:
        if "error" in r:
            lines.append(f"| {r['symbol']} | (error) | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {r['symbol']} | {r['asset_class']} | "
            f"${r['close']:.2f} | "
            f"{r.get('ret_1d_pct', 0):+.2f}% | "
            f"{r.get('ret_5d_pct', 0):+.2f}% | "
            f"{r.get('ret_20d_pct', 0):+.2f}% | "
            f"{r.get('rvol_vs_20d') or 0:.2f}x | "
            f"{r.get('dist_sma20_pct') or 0:+.2f}% |"
        )
    lines.append("")

    # Position reconciliation: spliced in if the latest reconcile run
    # left a snapshot file. Silent no-op when the file is absent.
    try:
        from monitoring import reconcile_positions
        rec = reconcile_positions.load_snapshot()
        if rec is not None:
            lines.append(reconcile_positions.format_section(rec))
            lines.append("")
    except Exception:
        pass

    lines.append("## Notes for future Claude")
    lines.append("_Patterns to watch, hypotheses, anything that should surface during tomorrow's startup ritual._")
    lines.append("")
    lines.append("- _(none today)_")
    lines.append("")

    lines.append("---")
    lines.append(f"_Generated by `monitoring/daily_report.py` at "
                 f"{datetime.now().isoformat(timespec='seconds')}_")

    return "\n".join(lines)


def persist_report(report: DailyReport, markdown: Optional[str] = None) -> Dict[str, int]:
    """Write the report + snapshots + fire/exit signals; reconcile outcomes."""
    conn = db.init_db()
    try:
        for row in report.snapshot_rows:
            db.record_snapshot_row(conn, report.report_date.isoformat(), row)
        for f in report.fires:
            bar_ts = f.get("bar_date") or report.report_date.isoformat()
            db.record_signal(
                conn,
                strategy_id=f["strategy_id"],
                symbol=f["symbol"],
                bar_ts=bar_ts,
                signal_type="long_entry",
                close=f.get("close"),
                bar_interval="1d",
                extra={k: v for k, v in f.items()
                       if k not in {"strategy_id", "symbol", "bar_date", "close", "fired"}},
            )
            if f.get("long_exit_signal"):
                db.record_signal(
                    conn,
                    strategy_id=f["strategy_id"],
                    symbol=f["symbol"],
                    bar_ts=bar_ts,
                    signal_type="long_exit",
                    close=f.get("close"),
                    bar_interval="1d",
                )
        for e in report.exit_signals:
            bar_ts = e.get("bar_date") or report.report_date.isoformat()
            db.record_signal(
                conn,
                strategy_id=e["strategy_id"],
                symbol=e["symbol"],
                bar_ts=bar_ts,
                signal_type="long_exit",
                close=e.get("close"),
                bar_interval="1d",
            )
        status = db.record_daily_report(
            conn,
            report_date=report.report_date.isoformat(),
            market_regime=report.market_regime,
            importance=report.importance,
            fires_count=len(report.fires),
            watchlist_count=len(report.symbols_watched),
            notable_movers_count=len(report.notable_movers),
            tags=report.tags,
            symbols_watched=report.symbols_watched,
            has_notable_pattern=report.has_notable_pattern,
            markdown=markdown,
        )
        if status == "skipped_downgrade":
            from config.utils import log
            log(f"daily_reports row for {report.report_date} NOT overwritten "
                f"— this run had fewer fires/watchlist than the existing row "
                f"(probable yfinance hiccup). Re-run with force to override.",
                "WARNING")
        # F3 (audit 2026-06-03): the live reconcile previously passed no
        # bars_fetcher, so close_for_exit could never compute MFE/MAE — every
        # closed outcome landed with mfe_pct/mae_pct NULL. Feed it the same
        # daily bars fetcher auto_trader uses so the 1d signal-exit majority
        # records excursion. (Stop/trailing reasons are handled in F5; the
        # intraday open/close pass is F2.)
        from monitoring.auto_trader import _build_default_bars_fetcher
        counts = outcome_tracker.reconcile_signals(
            conn, bars_fetcher=_build_default_bars_fetcher()
        )
        # F2 (audit 2026-06-03): the 1d pass above never opens intraday
        # outcomes, so M1's EOD-flatten capture had nothing to close — 0
        # outcome rows for any intraday signal. Run a second pass over the
        # intraday intervals with open_only=True: it OPENS an outcome for
        # each intraday entry but does NOT close on an intraday scanner
        # long_exit signal. The EOD flatten (close_intraday_positions) then
        # closes those open outcomes with exit_reason='eod_close' + MFE/MAE.
        # Kept non-overlapping with the 1d pass (distinct bar_intervals) so
        # no 1d outcome is double-opened. Runs here on the EOD schedule so
        # the open happens before close_intraday_positions fires at 16:00 ET.
        intraday_counts = outcome_tracker.reconcile_signals(
            conn,
            bar_intervals=["1m", "5m", "15m", "1d-intraday"],
            open_only=True,
        )
        counts["opened"] += intraday_counts["opened"]
        counts["noop"] += intraday_counts["noop"]
        # F2-SAFETY (audit 2026-06-03): F2 lets ONLY the EOD flatten close an
        # intraday outcome. If a prior session's flatten was missed (crash,
        # restart, schedule gap) the outcome is orphaned OPEN forever. Sweep
        # PRIOR-session orphans here (entry strictly before this report's
        # session) and close them honestly at the last available bar with
        # exit_reason='stale_intraday_flatten_missed'. Same-session intraday
        # outcomes are NOT touched — the flatten still owns the normal close,
        # so this never races the report-date session's flatten. Best-effort:
        # a sweep failure must not abort report persistence.
        try:
            from monitoring import close_intraday_positions
            # Boundary is THIS report's session date: the EOD flatten owns the
            # current (report_date) session, so only outcomes entered strictly
            # before report_date are prior-session orphans to be swept.
            sweep = close_intraday_positions.sweep_stale_intraday_outcomes(
                conn, session_date=report.report_date)
            if sweep.get("swept"):
                from config.utils import log
                log(f"persist_report: stale-intraday safety net closed "
                    f"{sweep['swept']} orphaned outcome(s)", "INFO")
        except Exception as e:
            from config.utils import log
            log(f"persist_report: stale-intraday sweep skipped "
                f"({type(e).__name__}: {e})", "WARNING")
        return counts
    finally:
        conn.close()


def post_to_notion(report: DailyReport, markdown: str) -> Optional[str]:
    """
    Idempotent: skip if a notion_page_id already exists for this report_date.
    Returns the page_id (existing or newly-created), or None on failure.
    """
    from config.utils import log
    conn = db.init_db()
    try:
        existing = conn.execute(
            "SELECT notion_page_id FROM daily_reports WHERE report_date = ?",
            (report.report_date.isoformat(),),
        ).fetchone()
        if existing and existing["notion_page_id"]:
            log(f"Notion: page already exists for {report.report_date} "
                f"({existing['notion_page_id']}); skipping post", "INFO")
            return existing["notion_page_id"]
        try:
            from monitoring import notion_writer
            resp = notion_writer.post_daily_report(
                report, markdown, NOTION_DAILY_REPORTS_DB_ID,
            )
        except Exception as e:
            log(f"Notion post failed (non-fatal): {e}", "WARNING")
            return None
        page_id = resp.get("id")
        if page_id:
            conn.execute(
                "UPDATE daily_reports SET notion_page_id = ? WHERE report_date = ?",
                (page_id, report.report_date.isoformat()),
            )
            conn.commit()
            log(f"Notion: posted page {page_id}", "SUCCESS")
        return page_id
    finally:
        conn.close()


def to_notion_properties(report: DailyReport) -> Dict:
    """Build the Notion page properties dict for the Trading Daily Reports DB."""
    return {
        "Report": f"Daily Report — {report.report_date.isoformat()}",
        "date:Date:start": report.report_date.isoformat(),
        "date:Date:is_datetime": 0,
        "Market Regime": report.market_regime,
        "Importance": report.importance,
        "Has Notable Pattern": "__YES__" if report.has_notable_pattern else "__NO__",
        "Watchlist Count": len(report.symbols_watched),
        "Strategy Fires": len(report.fires),
        "Symbols Watched": json.dumps(report.symbols_watched),
        "Tags": json.dumps(report.tags),
        "Status": "Generated",
        "Source": "auto-scan",
    }


def maybe_run_trend_scanner() -> Optional[int]:
    """Run the wide-universe trend scanner iff
    `auto_trade.trend_scanner_enabled` is True in settings.

    Returns the fire count (0 if disabled / failed). Returns None on
    settings-read failure — that's the signal to the caller that we
    skipped without even reaching the enable check. Failures are
    always non-fatal: the daily report must still publish on a
    scanner crash.
    """
    try:
        from monitoring import auto_trader
        _at_settings = auto_trader._config()
    except Exception as outer:  # noqa: BLE001
        from config.utils import log
        log(f"trend_scanner enable-check failed (non-fatal): {outer}",
            "WARNING")
        return None
    if not _at_settings.get("trend_scanner_enabled", False):
        return 0
    try:
        from monitoring import trend_scanner
        scan_fires = trend_scanner.scan_trend_universe()
        from config.utils import log
        log(f"trend_scanner: {len(scan_fires)} fires recorded for "
            f"wide universe", "INFO")
        return len(scan_fires)
    except Exception as scan_exc:  # noqa: BLE001
        from config.utils import log
        log(f"trend_scanner failed (non-fatal): {scan_exc}", "WARNING")
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", default=date.today().isoformat())
    parser.add_argument("-o", "--out", help="Write markdown to file path")
    parser.add_argument("--json", action="store_true", help="Output as JSON instead of markdown")
    parser.add_argument("--no-news", action="store_true", help="Skip Polygon news fetch")
    parser.add_argument("--news-limit", type=int, default=5)
    parser.add_argument("--no-notion", action="store_true", help="Skip Notion auto-post")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram summary")
    parser.add_argument("--no-trade", action="store_true", help="Skip auto-trader (paper orders)")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.date)
    # Auto-seed any TRACKED_STRATEGIES entries that aren't yet in the
    # strategies table. Prevents the FK constraint failure that has bitten
    # us on each new strategy addition (trend 2026-05-18, intraday 2026-05-19).
    try:
        from monitoring.config import TRACKED_STRATEGIES as _TS
        _conn = db.init_db()
        try:
            new_ids = db.ensure_strategies_seeded(_conn, _TS)
            if new_ids:
                log(f"auto-seeded {len(new_ids)} new strategy rows: {new_ids}",
                    level="INFO")
        finally:
            _conn.close()
    except Exception as exc:  # noqa: BLE001 — never fail the report on this
        log(f"ensure_strategies_seeded skipped: {exc}", level="WARNING")
    report = build_report(as_of)
    if not args.no_news:
        gather_news(report, limit=args.news_limit)
        finalize_report(report)  # rescore now that news is loaded
    markdown = render_markdown(report)
    persist_report(report, markdown=markdown)

    # 5.5.5.1 — Wide-universe trend scanner. Conditional on the master
    # flag `auto_trade.trend_scanner_enabled` (default false). Runs BEFORE
    # auto_trader so the freshly-recorded scanner signals get picked up in
    # the same pipeline pass. Failures are isolated — a scanner crash
    # must not poison the daily report.
    if not args.no_trade:
        maybe_run_trend_scanner()

    if not args.no_trade:
        try:
            from monitoring import auto_trader
            from data import db as _db
            _conn = _db.init_db()
            try:
                trade_result = auto_trader.process_signals(_conn, asof=as_of)
            finally:
                _conn.close()
            from config.utils import log
            log(f"auto_trader: status={trade_result['status']} "
                f"actions={len(trade_result.get('actions', []))}", "INFO")
        except Exception as e:
            from config.utils import log
            log(f"auto_trader failed (non-fatal): {e}", "WARNING")

    page_id = None
    if not args.no_notion:
        page_id = post_to_notion(report, markdown)
    if not args.no_telegram:
        try:
            from monitoring import telegram_alerter
            telegram_alerter.send_daily_summary(report, notion_page_id=page_id)
        except Exception as e:
            from config.utils import log
            log(f"Telegram daily summary failed (non-fatal): {e}", "WARNING")

    if args.json:
        payload = {
            "date": report.report_date.isoformat(),
            "market_regime": report.market_regime,
            "importance": report.importance,
            "tags": report.tags,
            "fires": report.fires,
            "notable_movers": report.notable_movers,
            "snapshot": report.snapshot_rows,
            "news_by_symbol": report.news_by_symbol,
            "notion_properties": to_notion_properties(report),
        }
        out = json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        out = markdown

    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        _emit(f"Wrote: {args.out}")
    else:
        _emit(out)


def _emit(text: str) -> None:
    # colorama wraps sys.stdout on Windows and forces cp1252, choking on
    # chars like Δ. Write UTF-8 bytes straight to the underlying buffer.
    data = (text + "\n").encode("utf-8", errors="replace")
    buf = getattr(sys.stdout, "buffer", None) or sys.__stdout__.buffer
    buf.write(data)
    buf.flush()


if __name__ == "__main__":
    main()
