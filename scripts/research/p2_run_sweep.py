"""
p2_run_sweep.py — Run the full P2 backtest sweep and write result CSVs.

Validates the 4 coded strategies + the gap-fill prototype across the proven
EOD ETF core and candidate ETFs. Writes:
  data/p2_strategy_validation_results.csv   (per strategy x symbol + ALL agg)
  data/p2_gap_fill_results.csv              (gap-fill per symbol + agg)
  data/p2_symbol_expansion_results.csv      (best MR strategy on candidate ETFs)

OFFLINE research only. Reuses backtest engine + cached yfinance history.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402
from scripts.research.p2_backtest_lib import (  # noqa: E402
    aggregate, backtest_one, metrics_to_df,
)
from scripts.research.p2_polygon_daily import PROVEN_CORE, CANDIDATE_ETFS  # noqa: E402

from strategies.generated.rsi2_oversold import compute_rsi2_oversold  # noqa: E402
from strategies.generated.rsi14_oversold import compute_rsi14_oversold  # noqa: E402
from strategies.generated.inside_day_breakout import compute_inside_day_breakout  # noqa: E402
from strategies.generated.bollinger_bandit import compute_bollinger_bandit  # noqa: E402
from strategies.generated.gap_fill_reversion import compute_gap_fill_reversion  # noqa: E402

HIST = ROOT / "data" / "p2_history_yf.pkl"

CODED = [
    ("rsi2-oversold", compute_rsi2_oversold),
    ("rsi14-oversold", compute_rsi14_oversold),
    ("inside-day-breakout", compute_inside_day_breakout),
    ("bollinger-bandit", compute_bollinger_bandit),
]


def load_hist():
    with open(HIST, "rb") as fh:
        return pickle.load(fh)


def run_strategy_validation(data):
    rows = []
    for name, fn in CODED:
        for sym in PROVEN_CORE:
            if sym not in data:
                continue
            try:
                rows.append(backtest_one(name, fn, sym, data[sym]))
            except Exception as e:  # noqa: BLE001
                log(f"{name} x {sym} FAILED: {e}", "ERROR")
        rows.append(aggregate([], name, {s: data[s] for s in PROVEN_CORE if s in data}, fn))
    df = metrics_to_df(rows)
    out = ROOT / "data" / "p2_strategy_validation_results.csv"
    df.to_csv(out, index=False)
    log(f"wrote {out} ({len(df)} rows)", "INFO")
    return df


def run_gap_fill(data):
    rows = []
    for sym in PROVEN_CORE:
        if sym not in data:
            continue
        rows.append(backtest_one("gap-fill-reversion", compute_gap_fill_reversion, sym, data[sym]))
    rows.append(aggregate([], "gap-fill-reversion",
                          {s: data[s] for s in PROVEN_CORE if s in data},
                          compute_gap_fill_reversion))
    df = metrics_to_df(rows)
    out = ROOT / "data" / "p2_gap_fill_results.csv"
    df.to_csv(out, index=False)
    log(f"wrote {out} ({len(df)} rows)", "INFO")
    return df


def run_symbol_expansion(data):
    """Run the strongest classic MR strategy (rsi2 + gap-fill) on candidate ETFs
    NOT already in the proven core, to rank expansion candidates."""
    candidates = [s for s in CANDIDATE_ETFS if s not in PROVEN_CORE and s in data]
    rows = []
    for sym in candidates:
        rows.append(backtest_one("rsi2-oversold", compute_rsi2_oversold, sym, data[sym]))
        rows.append(backtest_one("gap-fill-reversion", compute_gap_fill_reversion, sym, data[sym]))
    df = metrics_to_df(rows)
    out = ROOT / "data" / "p2_symbol_expansion_results.csv"
    df.to_csv(out, index=False)
    log(f"wrote {out} ({len(df)} rows)", "INFO")
    return df


def main():
    data = load_hist()
    log(f"loaded {len(data)} symbols of history", "INFO")
    pd.set_option("display.width", 200, "display.max_columns", 30)

    print("\n" + "=" * 90 + "\nSTRATEGY VALIDATION (coded strategies x proven core)\n" + "=" * 90)
    sv = run_strategy_validation(data)
    print(sv[sv.symbol == "ALL"][["strategy", "n_trades", "win_rate_pct",
          "mean_ret_pct", "profit_factor", "payoff_ratio", "sharpe_ish"]].to_string(index=False))

    print("\n" + "=" * 90 + "\nGAP-FILL PROTOTYPE (per symbol + ALL)\n" + "=" * 90)
    gf = run_gap_fill(data)
    print(gf[["symbol", "n_trades", "win_rate_pct", "mean_ret_pct", "profit_factor",
          "payoff_ratio", "sharpe_ish", "cagr_pct", "bh_cagr_pct"]].to_string(index=False))

    print("\n" + "=" * 90 + "\nSYMBOL EXPANSION (rsi2 + gap-fill on candidate ETFs)\n" + "=" * 90)
    se = run_symbol_expansion(data)
    print(se[["strategy", "symbol", "n_trades", "win_rate_pct", "mean_ret_pct",
          "profit_factor", "payoff_ratio", "sharpe_ish"]].to_string(index=False))


if __name__ == "__main__":
    main()
