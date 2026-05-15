"""
strategy2_run.py — Run the micro-pullback strategy on the Strategy 1 universe
and report aggregate stats. Includes slippage sensitivity sweep.

Usage:
  python -m strategies.momentum.strategy2_run [start] [end]
"""

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from strategies.momentum.scanner import scan_range, ScannerConfig
from strategies.momentum.execution import (
    StrategyConfig, run_universe,
)


def trades_to_df(trades) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([t.__dict__ for t in trades])


def summarize(trades_df: pd.DataFrame, label: str, initial_equity: float = 10_000.0):
    if trades_df.empty:
        print(f"\n--- {label} ---  ZERO TRADES")
        return None

    n = len(trades_df)
    wins = (trades_df["pnl_dollars"] > 0).sum()
    losses = (trades_df["pnl_dollars"] < 0).sum()
    flats = (trades_df["pnl_dollars"] == 0).sum()
    win_rate = wins / n * 100
    total_pnl = trades_df["pnl_dollars"].sum()
    final_eq = initial_equity + total_pnl
    avg_r = trades_df["pnl_r"].mean()
    median_r = trades_df["pnl_r"].median()
    avg_win_r = trades_df.loc[trades_df["pnl_r"] > 0, "pnl_r"].mean() if wins else 0
    avg_loss_r = trades_df.loc[trades_df["pnl_r"] < 0, "pnl_r"].mean() if losses else 0
    gross_w = trades_df.loc[trades_df["pnl_dollars"] > 0, "pnl_dollars"].sum()
    gross_l = -trades_df.loc[trades_df["pnl_dollars"] < 0, "pnl_dollars"].sum()
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    expectancy_r = avg_r

    daily = trades_df.groupby("date")["pnl_dollars"].sum()
    sharpe_daily = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0
    eq = initial_equity + daily.cumsum()
    eq_running = pd.concat([pd.Series([initial_equity]), eq])
    peaks = eq_running.cummax()
    drawdown = (eq_running - peaks) / peaks * 100
    max_dd = drawdown.min()

    reasons = Counter(trades_df["exit_reason"])

    print(f"\n--- {label} ---")
    print(f"  Trades: {n}  ({wins}W / {losses}L / {flats} flat)")
    print(f"  Win rate: {win_rate:.1f}%")
    print(f"  Avg R: {avg_r:+.3f}  Median R: {median_r:+.3f}")
    print(f"  Avg winner: +{avg_win_r:.2f}R   Avg loser: {avg_loss_r:.2f}R")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Total $ P&L: ${total_pnl:+,.2f}  (start ${initial_equity:,.0f} -> ${final_eq:,.0f})")
    print(f"  Daily Sharpe (annualized): {sharpe_daily:.2f}")
    print(f"  Max drawdown: {max_dd:.2f}%")
    print(f"  Exit reasons: {dict(reasons)}")
    if "pullback_ordinal" in trades_df.columns:
        po = trades_df["pullback_ordinal"].value_counts().sort_index().to_dict()
        print(f"  Trades by pullback ordinal: {po}")

    return {
        "label": label, "n": n, "win_rate": win_rate, "avg_r": avg_r,
        "pf": pf, "pnl": total_pnl, "sharpe": sharpe_daily, "max_dd": max_dd,
    }


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-06-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2024-06-30"

    print(f"=== Strategy 2: Micro-pullback execution backtest ===")
    print(f"Window: {start} -> {end}\n")

    print("[1/2] Loading universe ...")
    qualifiers = scan_range(start, end, ScannerConfig())
    print(f"  {len(qualifiers)} qualifiers across {len(qualifiers['date'].unique())} days")

    print("\n[2/2] Running strategy across slippage scenarios ...")
    summaries = []

    for slip_bps_one_way, label in [
        (5,   "5 bps  one-way (10 bps RT) — fantasy"),
        (25,  "25 bps one-way (50 bps RT) — favorable retail"),
        (50,  "50 bps one-way (100 bps RT) — realistic small-cap"),
        (100, "100 bps one-way (200 bps RT) — pessimistic"),
    ]:
        cfg = StrategyConfig(slippage_bps_one_way=slip_bps_one_way)
        trades = run_universe(qualifiers, cfg)
        df = trades_to_df(trades)
        s = summarize(df, label, cfg.initial_equity)
        if s is not None:
            summaries.append(s)

    print("\n=== Slippage sensitivity ===")
    if summaries:
        sdf = pd.DataFrame(summaries)
        print(sdf.to_string(index=False))

    print("\n=== Pullback ordinal breakdown (using realistic 100bps RT) ===")
    cfg = StrategyConfig(slippage_bps_one_way=50)
    trades = run_universe(qualifiers, cfg)
    df = trades_to_df(trades)
    if not df.empty:
        for ord_n, group in df.groupby("pullback_ordinal"):
            wr = (group["pnl_dollars"] > 0).mean() * 100
            avgR = group["pnl_r"].mean()
            print(f"  Pullback #{ord_n}: n={len(group)}  win_rate={wr:.1f}%  avg_R={avgR:+.2f}")

        out_path = ROOT / "data" / "strategy2_trades.csv"
        df.to_csv(out_path, index=False)
        print(f"\n  Trade detail saved to {out_path}")

    print("\n=== Verdict ===")
    if not summaries:
        print("  NO TRADES — strategy never triggered. Check pattern detection.")
        return
    realistic = [s for s in summaries if "realistic" in s["label"]]
    if realistic:
        r = realistic[0]
        passed = r["sharpe"] > 1.0 and r["pf"] > 1.5 and r["win_rate"] > 35
        print(f"  At realistic 100bps RT slippage:")
        print(f"    Sharpe {r['sharpe']:.2f}  PF {r['pf']:.2f}  WinRate {r['win_rate']:.1f}%  PnL ${r['pnl']:+,.0f}")
        if passed:
            print(f"    GATE: PASS — strategy survives realistic costs. Worth deeper testing.")
        elif r["pnl"] > 0:
            print(f"    GATE: MARGINAL — profitable but below quality bar (Sharpe>1, PF>1.5, WR>35%)")
        else:
            print(f"    GATE: FAIL — does not survive realistic small-cap execution costs.")


if __name__ == "__main__":
    main()
