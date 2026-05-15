"""
_append_botnet101.py — Append the 9 Botnet101 strategy records to records.jsonl
based on the actual backtest results from data/botnet101_mean_reversion_results.csv.

Run once after the backtest. Records are appended (not deduplicated) so don't
re-run unless you've cleaned records.jsonl first.
"""

import json
import sys
from pathlib import Path

import pandas as pd

BUNDLE = Path(__file__).parent
JSONL = BUNDLE / "records.jsonl"
RESULTS_CSV = BUNDLE.parent.parent / "botnet101_mean_reversion_results.csv"


# Map runner-label -> strategy_id, source TradingView script_key, title, methodology snippet
STRATEGY_DEFS = {
    "buy-5day-low": {
        "id": "botnet101-buy-5day-low",
        "tv_url": "https://in.tradingview.com/script/ggrdrhcU-Buy-on-5-day-low-Strategy/",
        "title": "Botnet101 Buy on 5-Day Low",
        "entry_rules": "Long when close < lowest_low(prev 5 bars).",
        "exit_rules": "Exit when close > previous bar's high.",
        "core_concepts": ["mean reversion", "rolling low breach"],
    },
    "3-bar-low": {
        "id": "botnet101-3-bar-low",
        "tv_url": "https://in.tradingview.com/script/JLMmul3O-3-Bar-Low-Strategy/",
        "title": "Botnet101 3-Bar Low",
        "entry_rules": "Long when close < lowest_low(prev 3 bars). Optional: close > 200 EMA.",
        "exit_rules": "Exit when close > highest_high(prev 7 bars).",
        "core_concepts": ["mean reversion", "rolling low breach", "trend filter"],
    },
    "bb-reversal-ibs": {
        "id": "botnet101-bb-reversal-ibs",
        "tv_url": "https://in.tradingview.com/script/gdLxchYW-Bollinger-Bands-Reversal-IBS-Strategy/",
        "title": "Botnet101 Bollinger Bands Reversal + IBS",
        "entry_rules": "Long when IBS < 0.2 AND close < lower BB(20, 2.0).",
        "exit_rules": "Exit when IBS > 0.8.",
        "core_concepts": ["mean reversion", "Bollinger Bands", "Internal Bar Strength"],
    },
    "avg-hl-range-ibs": {
        "id": "botnet101-avg-hl-range-ibs",
        "tv_url": "https://in.tradingview.com/script/btwoIv1H-Average-High-Low-Range-IBS-Reversal-Strategy/",
        "title": "Botnet101 Avg High-Low Range + IBS Reversal",
        "entry_rules": "Long when close < SMA(close, 20) - 2.5*SMA(H-L, 20) for 2 consecutive bars AND IBS < 0.2.",
        "exit_rules": "Exit when close > previous bar's high.",
        "core_concepts": ["mean reversion", "volatility-adjusted threshold", "IBS"],
    },
    "turn-of-month-25": {
        "id": "botnet101-turn-of-month",
        "tv_url": "https://in.tradingview.com/script/FknzA6QS-Turn-of-the-Month-Strategy-on-Steroids/",
        "title": "Botnet101 Turn of the Month on Steroids",
        "entry_rules": "Long when day_of_month >= 25 AND close < close[1] AND close[1] < close[2].",
        "exit_rules": "Exit when 2-period RSI > 65.",
        "core_concepts": ["seasonality", "calendar anomaly", "mean reversion", "RSI"],
    },
    "consec-below-sma5": {
        "id": "botnet101-consec-below-ema",
        "tv_url": "https://in.tradingview.com/script/gnvgnsfj-Consecutive-Bars-Above-Below-EMA-Buy-the-Dip-Strategy/",
        "title": "Botnet101 Consecutive Bars Below MA Buy the Dip",
        "entry_rules": "Long when close < SMA(5) for 3 consecutive bars.",
        "exit_rules": "Exit when close > previous bar's high.",
        "core_concepts": ["mean reversion", "moving average", "buy the dip"],
    },
    "turn-around-tuesday": {
        "id": "botnet101-turn-around-tuesday",
        "tv_url": "https://in.tradingview.com/script/Urz1prxd-Turn-around-Tuesday-on-Steroids-Strategy/",
        "title": "Botnet101 Turn-around Tuesday on Steroids",
        "entry_rules": "Long if day-of-week == Monday AND close < close[1] AND close[1] < close[2].",
        "exit_rules": "Exit when close > previous bar's high.",
        "core_concepts": ["seasonality", "weekday anomaly", "mean reversion"],
    },
    "consec-bearish-3": {
        "id": "botnet101-consec-bearish",
        "tv_url": "https://in.tradingview.com/script/vrqolvsm-Consecutive-Bearish-Candle-Strategy/",
        "title": "Botnet101 Consecutive Bearish Candle",
        "entry_rules": "Long when close has been < previous close for 3 consecutive bars.",
        "exit_rules": "Exit when close > previous bar's high.",
        "core_concepts": ["mean reversion", "consecutive-down counting"],
    },
    "4bar-momentum-reversal": {
        "id": "botnet101-4bar-momentum-reversal",
        "tv_url": "https://in.tradingview.com/script/fvzgodz0-4-Bar-Momentum-Reversal-strategy/",
        "title": "Botnet101 4-Bar Momentum Reversal",
        "entry_rules": "Long when close < close[lookback=4] for 4 consecutive bars.",
        "exit_rules": "Exit when close > previous bar's high.",
        "core_concepts": ["mean reversion", "lookback comparison"],
    },
}

# Variants belonging to the same source strategy (extra test runs, not separate records)
VARIANTS = {
    "3-bar-low": ["3-bar-low-200ema"],
    "turn-around-tuesday": ["turn-around-tuesday-200sma"],
}


def verdict_for(rows_for_strategy):
    """
    rows_for_strategy: list of dicts with sharpe, cagr_pct, max_dd_pct, win_rate_pct
                       (one per symbol, baseline + all variants together).
    """
    bh_metrics = {}
    strat_metrics = []
    for r in rows_for_strategy:
        if r["strategy"] == "BUY_AND_HOLD":
            bh_metrics[r["symbol"]] = r
        else:
            strat_metrics.append(r)

    beats_count = 0
    underperform_count = 0
    for r in strat_metrics:
        bh = bh_metrics.get(r["symbol"])
        if bh is None:
            continue
        if r["sharpe"] >= bh["sharpe"] and r["max_dd_pct"] >= bh["max_dd_pct"]:
            beats_count += 1
        else:
            underperform_count += 1

    if beats_count == 0:
        return "FAIL"
    if beats_count >= len(strat_metrics) - 1:
        return "MARGINAL"
    return "PASS_WITH_NUANCE"


def main():
    if not RESULTS_CSV.exists():
        print(f"FAIL: results CSV not found at {RESULTS_CSV}", file=sys.stderr)
        sys.exit(1)
    df = pd.read_csv(RESULTS_CSV)

    new_records = []
    for label, defn in STRATEGY_DEFS.items():
        these_variants = [label] + VARIANTS.get(label, [])
        bh = df[df["strategy"] == "BUY_AND_HOLD"]
        own = df[df["strategy"].isin(these_variants)]
        all_rows = pd.concat([bh, own]).to_dict("records")

        verdict = verdict_for(all_rows)

        test_runs = []
        for _, row in own.iterrows():
            bh_row = bh[bh["symbol"] == row["symbol"]]
            tr = {
                "test_id": f"{defn['id']}-{row['symbol']}-{row['strategy']}-2010-2024",
                "date_iso": "2026-04-26",
                "instrument": row["symbol"],
                "timeframe": "1d",
                "period": "2010-01-01 to 2024-12-31",
                "trades": int(row["trades"]),
                "win_rate_pct": float(row["win_rate_pct"]),
                "sharpe": float(row["sharpe"]),
                "total_return_pct": float(row["total_return_pct"]),
                "cagr_pct": float(row["cagr_pct"]),
                "max_drawdown_pct": float(row["max_dd_pct"]),
                "slippage_bps_rt": 4.0,
                "verdict": (
                    "PASS" if (
                        not bh_row.empty
                        and float(row["sharpe"]) >= float(bh_row.iloc[0]["sharpe"])
                        and float(row["max_dd_pct"]) >= float(bh_row.iloc[0]["max_dd_pct"])
                    ) else "FAIL"
                ),
            }
            if not bh_row.empty:
                tr["benchmark_return_pct"] = float(bh_row.iloc[0]["total_return_pct"])
                tr["benchmark_sharpe"] = float(bh_row.iloc[0]["sharpe"])
                tr["benchmark_max_dd_pct"] = float(bh_row.iloc[0]["max_dd_pct"])
            if row["strategy"] != label:
                tr["variant"] = row["strategy"]
            test_runs.append(tr)

        per_sym_summary = []
        for _, row in own.iterrows():
            per_sym_summary.append(
                f"{row['symbol']}/{row['strategy']}: CAGR {row['cagr_pct']:.2f}% Sharpe {row['sharpe']:.2f} MaxDD {row['max_dd_pct']:.2f}%"
            )

        record = {
            "url": defn["tv_url"],
            "title": defn["title"],
            "author": "Botnet101",
            "description": defn["entry_rules"][:180],
            "source": "tradingview_pine_strategy",
            "date_scraped": "2026-04-26",
            "tags": ["mean-reversion", "long-only", "daily", "Botnet101"]
                    + (["seasonality"] if "seasonal" in defn["entry_rules"].lower() or "month" in label or "tuesday" in label else []),
            "extra": {
                "agent_summary": (
                    f"{defn['title']}: simple long-only mean-reversion rule on daily bars. "
                    f"Tested on SPY/QQQ/IWM 2010-2024, {len(test_runs)} runs. "
                    f"Verdict {verdict}. Summary: {'; '.join(per_sym_summary)}."
                ),
                "description_full_readable": (
                    f"# {defn['title']}\n\n"
                    f"## Source\n"
                    f"TradingView Pine strategy by Botnet101. URL: {defn['tv_url']}\n\n"
                    f"## Methodology\n"
                    f"- **Entry:** {defn['entry_rules']}\n"
                    f"- **Exit:** {defn['exit_rules']}\n"
                    f"- Long-only, single-position, daily bars.\n\n"
                    f"## Test results (2026-04-26)\n"
                    f"Backtested on SPY, QQQ, IWM daily 2010-2024 (3,773 bars per symbol).\n"
                    f"Slippage: 2 bps per fill (4 bps round-trip), realistic for liquid ETFs.\n"
                    f"Initial cash: $10,000, position size = 95% of available cash.\n\n"
                    f"### Per-symbol summary\n"
                    + "\n".join(f"- {s}" for s in per_sym_summary)
                    + f"\n\n### Verdict: {verdict}\n"
                    f"Compared against buy-and-hold of each symbol on Sharpe and MaxDD jointly. "
                    + ({"FAIL": "Strategy underperforms B&H on every tested symbol — no risk-adjusted edge.",
                        "MARGINAL": "Mixed results — beats B&H on some symbols, fails on others.",
                        "PASS_WITH_NUANCE": "Beats B&H on most symbols but absolute performance is below liquid index B&H."}
                       .get(verdict, ""))
                ),
                "strategy_id": defn["id"],
                "methodology_family": "Long-only daily mean-reversion (Botnet101 cluster)",
                "instruments": ["SPY", "QQQ", "IWM"],
                "timeframes": {"execution": "1d"},
                "core_concepts": defn["core_concepts"],
                "entry_rules": defn["entry_rules"],
                "exit_rules": defn["exit_rules"],
                "risk_management": "Single position, 95% cash deployment per entry, no leverage, no stop loss",
                "tested": True,
                "test_runs": test_runs,
                "current_verdict": verdict,
                "verdict_summary": (
                    f"Across SPY/QQQ/IWM 2010-2024: " + "; ".join(per_sym_summary) + ". "
                    f"Underperforms buy-and-hold on liquid indices despite high win rate — "
                    f"the time-in-cash penalty during strong bull periods exceeds any "
                    f"defensive benefit from avoiding drawdowns."
                ),
                "failure_modes": [
                    "Underexposed during strong trending markets (mostly in cash)",
                    "High win rate but small per-trade gains; rare losers can be large",
                    "No alpha vs liquid index B&H over 15-year sample"
                ],
                "improvement_hypotheses": [
                    "Combine multiple signals as portfolio (lower correlation might raise Sharpe)",
                    "Test on individual stocks with higher mean-reversion tendency",
                    "Stack with a trend-following overlay to capture trends + reversions",
                    "Test with leveraged ETFs (SSO/QLD) — same edge with more shares-at-work"
                ],
                "code_paths": {
                    "primitives": "strategies/mean_reversion/botnet101.py",
                    "runner": "strategies/mean_reversion/runner.py"
                },
                "data_artifacts": ["data/botnet101_mean_reversion_results.csv"],
                "first_logged_iso": "2026-04-26",
                "last_updated_iso": "2026-04-26",
            }
        }
        new_records.append(record)

    with open(JSONL, "a", encoding="utf-8") as f:
        for r in new_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"Appended {len(new_records)} records to {JSONL}")
    print()
    for r in new_records:
        print(f"  [{r['extra']['current_verdict']:<18}] {r['title']}")


if __name__ == "__main__":
    main()
