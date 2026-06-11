"""
p2_pull_history.py — Assemble the long-history daily dataset for the P2 sweep.

Primary source: yfinance (free, ~8yr depth, auto_adjust=True) via backtest.data.load_bars.
Secondary/validation source: Polygon free tier (p2_polygon_daily, ~500-bar/2yr cap).

Writes a cached pickle of {symbol: DataFrame} to data/p2_history_yf.pkl so the
backtest harness loads instantly. OFFLINE research only.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backtest.data import load_bars  # noqa: E402
from config.utils import log  # noqa: E402
from scripts.research.p2_polygon_daily import ALL_SYMBOLS  # noqa: E402

START = "2019-01-01"
END = "2026-06-01"
OUT = ROOT / "data" / "p2_history_yf.pkl"


def main():
    log(f"yfinance daily {START}..{END} for {len(ALL_SYMBOLS)} symbols", "INFO")
    data = load_bars(ALL_SYMBOLS, start=START, end=END, interval="1d", source="yf")
    log(f"loaded {len(data)}/{len(ALL_SYMBOLS)} symbols", "INFO")
    for s in sorted(data):
        df = data[s]
        log(f"  {s}: {len(df)} bars {df.index[0].date()}..{df.index[-1].date()}", "INFO")
    missing = [s for s in ALL_SYMBOLS if s not in data]
    if missing:
        log(f"MISSING: {missing}", "WARNING")
    with open(OUT, "wb") as fh:
        pickle.dump(data, fh)
    log(f"wrote {OUT}", "INFO")


if __name__ == "__main__":
    main()
