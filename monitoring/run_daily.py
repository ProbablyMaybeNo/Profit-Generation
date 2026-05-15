"""
run_daily.py — Generate today's report AND post to Notion. Single entry point.

Designed to be called by Windows Task Scheduler at ~09:00 ET on weekdays.
On weekends/holidays it gracefully exits (yfinance returns no fresh data).

Logs to logs/monitoring.log alongside the heartbeat log.

Usage:
  py -3.13 run_daily.py                # via the bat wrapper (preferred)
  python -m monitoring.run_daily       # within trading conda env
  python -m monitoring.run_daily 2026-04-26   # specific date for backfill
"""

import json
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log
from monitoring.daily_report import build_report, render_markdown
from monitoring.notion_writer import post_daily_report, smoke_test

DAILY_REPORTS_DB_ID = "38b8012b-9278-4d30-8806-e0f4ce92624e"

LOG_FILE = ROOT / "logs" / "monitoring.log"
REPORTS_DIR = ROOT / "logs" / "daily_reports"


def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 1:
        as_of = date.fromisoformat(sys.argv[1])
    else:
        as_of = date.today()

    log(f"daily_run start, as_of={as_of}", "INFO", str(LOG_FILE))

    try:
        if not smoke_test():
            log("Notion API smoke test failed; aborting", "ERROR", str(LOG_FILE))
            sys.exit(2)
    except Exception as e:
        log(f"Notion smoke test error: {e}", "ERROR", str(LOG_FILE))
        sys.exit(2)

    try:
        report = build_report(as_of)
    except Exception as e:
        log(f"build_report failed: {e}\n{traceback.format_exc()}", "ERROR", str(LOG_FILE))
        sys.exit(3)

    md = render_markdown(report)
    md_path = REPORTS_DIR / f"{as_of.isoformat()}.md"
    md_path.write_text(md, encoding="utf-8")
    log(f"Wrote markdown: {md_path}", "INFO", str(LOG_FILE))

    json_path = REPORTS_DIR / f"{as_of.isoformat()}.json"
    json_payload = {
        "date": report.report_date.isoformat(),
        "market_regime": report.market_regime,
        "importance": report.importance,
        "tags": report.tags,
        "fires_count": len(report.fires),
        "fires": report.fires,
        "notable_movers": report.notable_movers,
        "snapshot": report.snapshot_rows,
    }
    json_path.write_text(json.dumps(json_payload, indent=2), encoding="utf-8")

    try:
        result = post_daily_report(report, md, DAILY_REPORTS_DB_ID)
        url = result.get("url", "(no url)")
        log(f"Posted to Notion: {url}", "SUCCESS", str(LOG_FILE))
        print(f"OK posted: {url}")
    except Exception as e:
        log(f"Notion post failed: {e}", "ERROR", str(LOG_FILE))
        sys.exit(4)


if __name__ == "__main__":
    main()
