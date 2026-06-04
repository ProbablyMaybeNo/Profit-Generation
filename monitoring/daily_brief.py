"""
daily_brief.py — Detailed daily market + system report delivered to Telegram.

Usage:
  python -m monitoring.daily_brief            # today
  python -m monitoring.daily_brief 2026-06-03 # specific date

Sections (plain text, chunked at 4096 chars on section boundaries):
  1. Header          — date, regime, P&L, cash, deployed %
  2. System activity — signals fired, orders submitted/filled
  3. Trades          — entries/exits, swing vs intraday
  4. Intraday        — by strategy
  5. Risk mechanics  — ATR stops, trailing stops, pyramids, stop exits
  6. Outcomes        — closed today, open count, MFE/MAE coverage
  7. Notable         — news, macro, biggest movers

Sends via telegram_alerter.send_message (plain text, no parse_mode so
stray dashes/underscores in data never break the API). Long reports are
chunked with "(1/N)" section labels.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from data import db  # noqa: E402
from monitoring import _report_data as rd  # noqa: E402
from monitoring import telegram_alerter  # noqa: E402

TELEGRAM_MAX = 4000  # safe buffer under 4096


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_usd(v) -> str:
    if v is None:
        return "N/A"
    return f"${v:,.2f}"


def _fmt_pct(v) -> str:
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _fmt_row(trade: dict) -> str:
    price = _fmt_usd(trade.get("fill_price"))
    qty = trade.get("qty", "?")
    sym = trade.get("symbol", "?")
    strat = trade.get("strategy_id", "?")
    interval = trade.get("bar_interval") or "1d"
    return f"  {sym} x{qty} @ {price} [{strat} / {interval}]"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _section_header(h: dict) -> str:
    lines = [
        f"=== DAILY TRADING BRIEF — {h['date']} ===",
        f"Regime: {h['market_regime']}",
    ]
    if h["portfolio_value"] is not None:
        lines.append(f"Portfolio: {_fmt_usd(h['portfolio_value'])}")
    if h["day_pnl_usd"] is not None:
        lines.append(
            f"Day P&L:   {_fmt_usd(h['day_pnl_usd'])} ({_fmt_pct(h['day_pnl_pct'])})"
        )
    else:
        lines.append("Day P&L:   N/A (no prior-day snapshot)")
    if h["cash"] is not None:
        lines.append(f"Cash:      {_fmt_usd(h['cash'])}")
    if h["buying_power"] is not None:
        lines.append(f"Buying Pw: {_fmt_usd(h['buying_power'])}")
    if h["deployed_pct"] is not None:
        lines.append(f"Deployed:  {h['deployed_pct']:.1f}% of portfolio")
    return "\n".join(lines)


def _section_activity(a: dict) -> str:
    lines = ["--- SYSTEM ACTIVITY ---"]
    lines.append(
        f"Orders: {a['total_submitted']} submitted, "
        f"{a['total_filled']} filled | "
        f"Buys: {a['total_buys']}  Sells: {a['total_sells']}"
    )
    if a["signals_by_strat"]:
        lines.append("Signals fired today (strategy / interval / count):")
        for r in a["signals_by_strat"]:
            lines.append(f"  {r['strategy_id']} / {r['bar_interval']}: {r['cnt']}")
    else:
        lines.append("No signals recorded today.")
    return "\n".join(lines)


def _section_trades(t: dict) -> str:
    lines = ["--- TRADES ---"]

    def _block(label, trades):
        if not trades:
            return
        lines.append(label)
        for tr in trades:
            lines.append(_fmt_row(tr))

    _block("SWING Entries (buys):", t["entries_swing"])
    _block("SWING Exits (sells):", t["exits_swing"])
    _block("INTRADAY Entries (buys):", t["entries_intraday"])
    _block("INTRADAY Exits (sells):", t["exits_intraday"])

    total = (len(t["entries_swing"]) + len(t["exits_swing"]) +
             len(t["entries_intraday"]) + len(t["exits_intraday"]))
    if total == 0:
        lines.append("No filled trades today.")
    return "\n".join(lines)


def _section_intraday(i: dict) -> str:
    by_strat = i["intraday_by_strategy"]
    if not by_strat:
        return "--- INTRADAY BY STRATEGY ---\nNo intraday filled trades today."
    lines = ["--- INTRADAY BY STRATEGY ---"]
    for strat, trades in by_strat.items():
        syms = {}
        for tr in trades:
            sym = tr["symbol"]
            syms.setdefault(sym, {"buy": None, "sell": None})
            if tr["side"] == "buy":
                syms[sym]["buy"] = tr.get("fill_price")
            else:
                syms[sym]["sell"] = tr.get("fill_price")
        lines.append(f"  {strat}:")
        for sym, prices in syms.items():
            e = _fmt_usd(prices["buy"])
            x = _fmt_usd(prices["sell"]) if prices["sell"] else "open"
            lines.append(f"    {sym}: entry {e} / exit {x}")
    return "\n".join(lines)


def _section_risk(r: dict) -> str:
    lines = ["--- RISK MECHANICS ---"]
    lines.append(f"ATR initial stops attached today: {r['atr_stops_count']}")

    if r["trailing_stops"]:
        lines.append(f"Trailing stops armed: {len(r['trailing_stops'])}")
        for ts in r["trailing_stops"][:8]:
            gap = None
            if ts.get("extreme_price") and ts.get("stop_price"):
                gap = abs(ts["extreme_price"] - ts["stop_price"])
            gap_str = f" gap={gap:.2f}" if gap is not None else ""
            lines.append(
                f"  {ts['symbol']} [{ts['strategy_id']}] "
                f"method={ts['method']} stop={_fmt_usd(ts['stop_price'])}"
                f" extreme={_fmt_usd(ts['extreme_price'])}{gap_str}"
            )
        if len(r["trailing_stops"]) > 8:
            lines.append(f"  ... +{len(r['trailing_stops']) - 8} more")
    else:
        lines.append("Trailing stops: none armed")

    if r["pyramids"]:
        lines.append(f"Pyramid adds today: {len(r['pyramids'])}")
        for p in r["pyramids"]:
            lines.append(f"  {p['symbol']} tier={p['pyramid_tier']} @ {_fmt_usd(p['fill_price'])}")

    if r["pyramid_skips"]:
        lines.append(f"Pyramid skips today: {sum(x['cnt'] for x in r['pyramid_skips'])}")

    if r["stop_exits"]:
        lines.append(f"Stop/trailing EXITS today: {len(r['stop_exits'])}")
        for se in r["stop_exits"]:
            lines.append(
                f"  {se['symbol']} [{se['strategy_id']}] "
                f"reason={se['exit_reason']} ret={_fmt_pct(se.get('return_pct'))}"
            )
    return "\n".join(lines)


def _section_outcomes(o: dict) -> str:
    lines = ["--- OUTCOMES ---"]

    if o["closed_today"]:
        lines.append("Closed today by exit reason:")
        for row in o["closed_today"]:
            win_rate = (row["wins"] / row["cnt"] * 100) if row["cnt"] > 0 else 0
            lines.append(
                f"  {row['exit_reason'] or 'unknown'}: {row['cnt']} trades "
                f"avg ret={_fmt_pct(row['avg_ret'])} "
                f"win%={win_rate:.0f}%"
            )
    else:
        lines.append("No closed outcomes today.")

    lines.append(f"Open positions (outcomes): {o['open_positions']}")

    cov = o["mfe_coverage"]
    if cov["total"] > 0:
        mfe_pct = cov["has_mfe"] / cov["total"] * 100
        mae_pct = cov["has_mae"] / cov["total"] * 100
        lines.append(
            f"MFE/MAE coverage: {cov['has_mfe']}/{cov['total']} "
            f"({mfe_pct:.0f}%) MFE, {cov['has_mae']}/{cov['total']} "
            f"({mae_pct:.0f}%) MAE"
        )
    return "\n".join(lines)


def _section_notable(n: dict) -> str:
    lines = ["--- NOTABLE ---"]

    if n["macro"]:
        lines.append("Macro:")
        for m in n["macro"]:
            lines.append(f"  {m['series_id']}: {m['value']} (as of {m['bar_date']})")

    if n["movers"]:
        lines.append("Biggest movers (snapshot):")
        for mv in n["movers"][:6]:
            lines.append(
                f"  {mv['symbol']}: {_fmt_pct(mv.get('ret_1d_pct'))} "
                f"close={_fmt_usd(mv.get('close'))} "
                f"rvol={mv.get('rvol_vs_20d') or 0:.1f}x"
            )

    if n["news"]:
        lines.append("News headlines today:")
        seen_titles: set = set()
        shown = 0
        for item in n["news"]:
            title = (item.get("title") or "")[:100]
            if title in seen_titles:
                continue
            seen_titles.add(title)
            pub = (item.get("published_utc") or "")[:16].replace("T", " ")
            lines.append(f"  [{pub}] {item.get('symbol','?')}: {title}")
            shown += 1
            if shown >= 8:
                break
    else:
        lines.append("News: none fetched for today.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Assembly + chunking + send
# ---------------------------------------------------------------------------

def build_report_text(conn, as_of) -> str:
    h = rd.get_header(conn, as_of)
    a = rd.get_activity(conn, as_of)
    t = rd.get_trades(conn, as_of)
    i = rd.get_intraday_by_strategy(conn, as_of)
    r = rd.get_risk(conn, as_of)
    o = rd.get_outcomes(conn, as_of)
    n = rd.get_notable(conn, as_of)

    sections = [
        _section_header(h),
        _section_activity(a),
        _section_trades(t),
        _section_intraday(i),
        _section_risk(r),
        _section_outcomes(o),
        _section_notable(n),
    ]
    return "\n\n".join(sections)


def is_empty_day(conn, as_of) -> bool:
    a = rd.get_activity(conn, as_of)
    h = rd.get_header(conn, as_of)
    return (a["total_submitted"] == 0 and
            h["portfolio_value"] is None and
            len(a["signals_by_strat"]) == 0)


def chunk_report(text: str, max_chars: int = TELEGRAM_MAX) -> List[str]:
    """Split a long report on blank-line boundaries, labelling chunks (1/N)."""
    if len(text) <= max_chars:
        return [text]

    # Split on double-newline (section boundaries)
    paragraphs = text.split("\n\n")
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for the \n\n separator
        if current_len + para_len > max_chars and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len

    if current:
        chunks.append("\n\n".join(current))

    # Add (n/N) labels
    total = len(chunks)
    if total > 1:
        chunks = [f"({i+1}/{total})\n{c}" for i, c in enumerate(chunks)]
    return chunks


def send_report(text: str, prefix: str = "") -> bool:
    chunks = chunk_report(prefix + text if prefix else text)
    success = True
    for chunk in chunks:
        ok = telegram_alerter.send_message(chunk, parse_mode=None)
        if not ok:
            log(f"daily_brief: telegram send failed for chunk ({len(chunk)} chars)",
                "WARNING")
            success = False
    return success


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Send the detailed daily trading brief to Telegram."
    )
    parser.add_argument("date", nargs="?", default=date.today().isoformat())
    parser.add_argument("--prefix", default="",
                        help="Optional prefix string prepended to the first chunk.")
    parser.add_argument("--no-send", action="store_true",
                        help="Print the report to stdout only; do not send.")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.date)
    conn = db.init_db()
    try:
        if is_empty_day(conn, as_of):
            msg = (f"=== DAILY BRIEF — {as_of} ===\n"
                   "No trading activity and no equity snapshot recorded today "
                   "(weekend / holiday / system offline).")
            log("daily_brief: no activity for today, sending empty-day note", "INFO")
            if not args.no_send:
                ok = telegram_alerter.send_message(
                    (args.prefix + msg) if args.prefix else msg,
                    parse_mode=None,
                )
                if ok:
                    log("daily_brief: empty-day note sent", "SUCCESS")
                else:
                    log("daily_brief: empty-day note send failed", "WARNING")
            else:
                print(msg)
            return

        text = build_report_text(conn, as_of)
        if args.no_send:
            # Write UTF-8 safely on Windows
            buf = getattr(sys.stdout, "buffer", None)
            if buf:
                buf.write((text + "\n").encode("utf-8", errors="replace"))
                buf.flush()
            else:
                print(text)
            return

        ok = send_report(text, prefix=args.prefix)
        if ok:
            log(f"daily_brief: report sent for {as_of}", "SUCCESS")
        else:
            log(f"daily_brief: one or more chunks failed for {as_of}", "WARNING")
    except Exception as exc:
        log(f"daily_brief: unhandled error: {exc}", "ERROR")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
