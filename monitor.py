"""
monitor.py — Trading system heartbeat monitor.
Invoked by Windows Task Scheduler every 15 minutes.
Must complete in under 10 seconds. No blocking calls, no retries.
"""

import sys
import os
import glob
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from config.utils import market_is_open, get_account_summary, log

LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
HEARTBEAT_LOG = LOG_DIR / "heartbeat.log"
LOG_DIR.mkdir(exist_ok=True)


def run():
    if not market_is_open():
        log("Market closed. Nothing to do.", "INFO", str(HEARTBEAT_LOG))
        return

    try:
        summary = get_account_summary()
    except Exception as e:
        log(f"Could not fetch account summary: {e}", "ERROR", str(HEARTBEAT_LOG))
        return

    portfolio = summary["portfolio_value"]
    cash = summary["cash"]
    buying_power = summary["buying_power"]

    log(f"Portfolio: ${portfolio:,.2f} | Cash: ${cash:,.2f} | Buying power: ${buying_power:,.2f}", "INFO")

    # Check for strategy state files
    state_files = list(DATA_DIR.glob("*_state.json"))
    active_count = len(state_files)
    for sf in state_files:
        mtime = datetime.fromtimestamp(sf.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        log(f"  Strategy state: {sf.name} (last modified: {mtime})", "INFO")

    # Check for silent strategies (log files not updated in 30 mins during market hours)
    cutoff = datetime.now() - timedelta(minutes=30)
    for lf in LOG_DIR.glob("*.log"):
        if lf.name == "heartbeat.log":
            continue
        mtime = datetime.fromtimestamp(lf.stat().st_mtime)
        if mtime < cutoff:
            log(f"WARNING: {lf.name} has not been updated in over 30 minutes — strategy may be silent", "WARNING")

    # Write heartbeat line
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    heartbeat = (
        f"[{ts}] HEARTBEAT | Market: OPEN | "
        f"Portfolio: ${portfolio:,.2f} | Cash: ${cash:,.2f} | "
        f"Active strategies: {active_count}"
    )
    with open(HEARTBEAT_LOG, "a") as f:
        f.write(heartbeat + "\n")

    log(heartbeat, "SUCCESS")


if __name__ == "__main__":
    run()
