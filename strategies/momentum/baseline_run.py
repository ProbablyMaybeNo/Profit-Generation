"""
baseline_run.py — Strategy 1: drift baseline on Five-Pillar gappers.

Runs the universe scanner over a date range, fetches 1m bars for each
qualifier, and computes the average / median return at multiple horizons
from the 9:30 open.

This answers the gating question: do Ross-style gapper stocks have any
positive expected drift on average? If no, all execution-level strategies
in this family are dead-on-arrival.

Usage:
  python -m strategies.momentum.baseline_run [start] [end]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd

from strategies.momentum.scanner import scan_range, ScannerConfig
from strategies.momentum.drift import measure_universe


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-06-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2024-06-30"

    print(f"=== Strategy 1: Five-Pillar drift baseline ===")
    print(f"Window: {start} -> {end}\n")

    print("[1/2] Scanning universe ...")
    qualifiers = scan_range(start, end, ScannerConfig())
    if qualifiers.empty:
        print("FAIL: no qualifying gappers in window")
        return
    print(f"  {len(qualifiers)} qualifiers across {len(qualifiers['date'].unique())} days")

    print("\n[2/2] Measuring drift on minute bars ...")
    drift = measure_universe(qualifiers)
    valid = drift[~drift["halted_or_missing"]].copy()
    print(f"  {len(valid)} of {len(drift)} have intraday data ({len(drift)-len(valid)} missing)\n")

    if valid.empty:
        print("FAIL: no usable intraday data")
        return

    horizons = ["ret_30min_pct", "ret_60min_pct", "ret_2h_pct", "ret_close_pct"]
    print("=== Drift statistics — equal-weighted across qualifiers ===\n")
    rows = []
    for col in horizons:
        s = valid[col].dropna()
        if s.empty:
            continue
        rows.append({
            "horizon": col.replace("ret_", "").replace("_pct", ""),
            "n": len(s),
            "mean_pct": s.mean(),
            "median_pct": s.median(),
            "hit_rate_pct": (s > 0).mean() * 100,
            "p25": s.quantile(0.25),
            "p75": s.quantile(0.75),
            "std_pct": s.std(),
        })
    summary = pd.DataFrame(rows)
    print(summary.to_string(index=False))

    print("\n=== Path characteristics ===")
    mfe = valid["max_favorable_pct"].dropna()
    mae = valid["max_adverse_pct"].dropna()
    print(f"  Max favorable excursion (intraday peak from open):")
    print(f"    mean +{mfe.mean():.2f}%   median +{mfe.median():.2f}%   p75 +{mfe.quantile(0.75):.2f}%")
    print(f"  Max adverse excursion (intraday trough from open):")
    print(f"    mean {mae.mean():.2f}%   median {mae.median():.2f}%   p25 {mae.quantile(0.25):.2f}%")

    print("\n=== Worst & best individual setups (close return) ===")
    rc = valid[["date", "ticker", "open_930", "ret_close_pct",
                "max_favorable_pct", "max_adverse_pct"]].dropna(subset=["ret_close_pct"])
    print("Top 5 winners:")
    print(rc.nlargest(5, "ret_close_pct").to_string(index=False))
    print("\nTop 5 losers:")
    print(rc.nsmallest(5, "ret_close_pct").to_string(index=False))

    print("\n=== Verdict ===")
    close_mean = valid["ret_close_pct"].dropna().mean()
    close_hit = (valid["ret_close_pct"].dropna() > 0).mean() * 100
    print(f"  Mean open->close return: {close_mean:+.2f}%")
    print(f"  Hit rate (closed green from open): {close_hit:.1f}%")
    if close_mean > 1.0 and close_hit > 50:
        print(f"  GATE: PASS — universe shows positive drift, worth deeper testing")
    elif close_mean > 0:
        print(f"  GATE: MARGINAL — small drift, edge may exist but tight")
    else:
        print(f"  GATE: FAIL — universe drifts down on average; mean reversion not momentum")

    out_path = ROOT / "data" / "momentum_drift_results.csv"
    valid.to_csv(out_path, index=False)
    print(f"\n  Detailed results saved to {out_path}")


if __name__ == "__main__":
    main()
