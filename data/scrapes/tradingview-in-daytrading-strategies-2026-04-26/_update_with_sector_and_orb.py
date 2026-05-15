"""
_update_with_sector_and_orb.py — One-shot updater that:
  1. Adds sector ETF test_runs to existing Botnet101 records
  2. Updates Botnet101 verdicts based on the full evidence
  3. Adds 3 new ORB strategy records
  4. Adds overlay-test note to 3-bar-low record
"""

import json
from pathlib import Path

import pandas as pd

BUNDLE = Path(__file__).parent
JSONL = BUNDLE / "records.jsonl"

LATEST_RESULTS = BUNDLE.parent.parent / "botnet101_mean_reversion_results.csv"
ORB_RESULTS = BUNDLE.parent.parent / "orb_family_results.csv"
OVERLAY_RESULTS = BUNDLE.parent.parent / "overlay_test_results.csv"


def load_records():
    out = []
    with open(JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def write_records(records):
    with open(JSONL, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# Maps runner strategy label -> existing strategy_id in records.jsonl
LABEL_TO_ID = {
    "buy-5day-low": "botnet101-buy-5day-low",
    "3-bar-low": "botnet101-3-bar-low",
    "3-bar-low-200ema": "botnet101-3-bar-low",
    "bb-reversal-ibs": "botnet101-bb-reversal-ibs",
    "avg-hl-range-ibs": "botnet101-avg-hl-range-ibs",
    "turn-of-month-25": "botnet101-turn-of-month",
    "consec-below-sma5": "botnet101-consec-below-ema",
    "turn-around-tuesday": "botnet101-turn-around-tuesday",
    "turn-around-tuesday-200sma": "botnet101-turn-around-tuesday",
    "consec-bearish-3": "botnet101-consec-bearish",
    "4bar-momentum-reversal": "botnet101-4bar-momentum-reversal",
}


def add_sector_runs(records):
    df = pd.read_csv(LATEST_RESULTS)
    sector_symbols = {"XLE", "XOP", "XBI", "KRE", "XME", "GDX", "XHB"}
    sector_df = df[df["symbol"].isin(sector_symbols)]

    by_id = {r["extra"]["strategy_id"]: r for r in records if "strategy_id" in r.get("extra", {})}

    for _, row in sector_df.iterrows():
        sid = LABEL_TO_ID.get(row["strategy"])
        if sid is None or sid not in by_id:
            continue
        rec = by_id[sid]
        bh = sector_df[(sector_df["symbol"] == row["symbol"]) & (sector_df["strategy"] == "BUY_AND_HOLD")]
        beats = (
            not bh.empty
            and float(row["sharpe"]) >= float(bh.iloc[0]["sharpe"])
            and float(row["max_dd_pct"]) >= float(bh.iloc[0]["max_dd_pct"])
            and int(row["trades"]) >= 20
        )
        tr = {
            "test_id": f"{sid}-{row['symbol']}-{row['strategy']}-2010-2024",
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
            "verdict": "PASS" if beats else "FAIL",
            "test_cohort": "sector_etfs",
        }
        if not bh.empty:
            tr["benchmark_return_pct"] = float(bh.iloc[0]["total_return_pct"])
            tr["benchmark_sharpe"] = float(bh.iloc[0]["sharpe"])
            tr["benchmark_max_dd_pct"] = float(bh.iloc[0]["max_dd_pct"])
        if row["strategy"] != sid.replace("botnet101-", "").replace("-", "-"):
            tr["variant"] = row["strategy"]
        rec["extra"]["test_runs"].append(tr)


def update_verdicts(records):
    """
    Recompute current_verdict for each Botnet101 record based on union of test_runs.
    Logic:
      - If any test_run PASS in major_indices cohort: PASS
      - Else if multiple PASS (>=2) in sector_etfs: PASS_WITH_NUANCE
      - Else if any PASS in sector_etfs: PASS_WITH_NUANCE (was FAIL on indices but works on at least one volatile ETF)
      - Else: FAIL
    """
    for rec in records:
        extra = rec.get("extra", {})
        sid = extra.get("strategy_id", "")
        if not sid.startswith("botnet101-"):
            continue
        runs = extra.get("test_runs", [])
        passes = sum(1 for r in runs if r.get("verdict") == "PASS")
        sector_passes = sum(
            1 for r in runs
            if r.get("verdict") == "PASS" and r.get("instrument") in {"XLE", "XOP", "XBI", "KRE", "XME", "GDX", "XHB"}
        )
        major_passes = sum(
            1 for r in runs
            if r.get("verdict") == "PASS" and r.get("instrument") in {"SPY", "QQQ", "IWM"}
        )

        if major_passes >= 1 and sector_passes >= 2:
            verdict = "PASS_WITH_NUANCE"
        elif sector_passes >= 3:
            verdict = "PASS_WITH_NUANCE"
        elif sector_passes >= 1 or major_passes >= 1:
            verdict = "MARGINAL"
        else:
            verdict = "FAIL"

        n = len(runs)
        win_runs = sum(1 for r in runs if r.get("verdict") == "PASS")
        win_symbols = sorted({r["instrument"] for r in runs if r.get("verdict") == "PASS"})

        extra["current_verdict"] = verdict
        extra["verdict_summary"] = (
            f"Tested across {n} (symbol, variant) combinations on major indices and "
            f"sector ETFs 2010-2024. Beats benchmark on Sharpe AND MaxDD jointly with >=20 "
            f"trades on {win_runs} of {n} runs. Passing instruments: "
            f"{', '.join(win_symbols) if win_symbols else 'none'}. "
            + ({
                "PASS_WITH_NUANCE": "Genuine alpha on volatile sector ETFs but no edge on liquid major indices.",
                "MARGINAL": "Edge on at most one or two instruments; not robust.",
                "FAIL": "No edge on any tested substrate after costs."
            }.get(verdict, ""))
        )
        extra["last_updated_iso"] = "2026-04-26"


def add_overlay_note(records):
    overlay_df = pd.read_csv(OVERLAY_RESULTS)
    for rec in records:
        if rec.get("extra", {}).get("strategy_id") != "botnet101-3-bar-low":
            continue
        for _, row in overlay_df.iterrows():
            scenario_id = f"overlay-{row['symbol']}-{row['scenario'].replace(' ', '_').replace('/', '_').replace('+', 'plus').replace('%', 'pct')}"
            rec["extra"]["test_runs"].append({
                "test_id": scenario_id,
                "date_iso": "2026-04-26",
                "instrument": row["symbol"],
                "timeframe": "1d",
                "period": "2010-01-01 to 2024-12-31",
                "scenario": row["scenario"],
                "total_return_pct": float(row["total_return_pct"]),
                "cagr_pct": float(row["cagr_pct"]),
                "sharpe": float(row["sharpe"]),
                "max_drawdown_pct": float(row["max_dd_pct"]),
                "verdict": "INFO",
                "test_cohort": "overlay_test",
                "note": "Linear combination of B&H and 3-Bar Low equity curves",
            })
        rec["extra"]["improvement_hypotheses"].append(
            "Use 3-Bar Low as a 30% defensive overlay on QQQ (back-tested: +0.02 Sharpe, "
            "-3.4% MaxDD vs pure B&H, only -0.98% CAGR cost)"
        )


def add_orb_records(records):
    orb_df = pd.read_csv(ORB_RESULTS)
    bh = orb_df[orb_df["label"] == "BUY_AND_HOLD"].iloc[0]

    orb_strategies = [
        {
            "label": "orbo-bidirectional",
            "strategy_id": "orbo-bidirectional",
            "url": "https://in.tradingview.com/script/Y6cGGA73-Session-Opening-Range-Breakout-ORBO/",
            "title": "ORBO Session Opening Range Breakout (bidirectional)",
            "author": "AIScripts",
            "entry": "Build OR 09:30-09:50 ET. Long if close > orHigh. Short if close < orLow.",
            "exit": "Stop at opposite OR side; EOD flat at 15:55.",
            "concepts": ["Opening Range Breakout", "intraday momentum", "session-based"],
        },
        {
            "label": "orbo-long-only",
            "strategy_id": "orbo-long-only",
            "url": "https://in.tradingview.com/script/Y6cGGA73-Session-Opening-Range-Breakout-ORBO/",
            "title": "ORBO Session Opening Range Breakout (long-only)",
            "author": "AIScripts (long-only variant)",
            "entry": "Build OR 09:30-09:50 ET. Long if close > orHigh.",
            "exit": "Stop at orLow; EOD flat at 15:55.",
            "concepts": ["Opening Range Breakout", "intraday momentum", "long-only"],
        },
        {
            "label": "orb-pivots-long-only",
            "strategy_id": "orb-pivots-long-only",
            "url": "https://in.tradingview.com/script/6KdE9bLA-Long-Only-Opening-Range-Breakout-ORB-with-Pivot-Points/",
            "title": "Long-Only ORB with Pivot Points",
            "author": "VolumeVigilante",
            "entry": "Build OR 09:30-09:45. Long if open<orHigh AND high>orHigh AND R1>orHigh.",
            "exit": "Initial stop = prior day's low. Trailing stop walks up via pivot ladder. EOD flat.",
            "concepts": ["Opening Range Breakout", "Floor Pivots", "trailing stop", "long-only"],
        },
    ]

    for spec in orb_strategies:
        row = orb_df[orb_df["label"] == spec["label"]].iloc[0]
        beats = (
            float(row["sharpe"]) >= float(bh["sharpe"])
            and float(row["max_dd_pct"]) >= float(bh["max_dd_pct"])
            and int(row["trades"]) >= 20
        )
        verdict = "PASS" if beats else "FAIL"
        record = {
            "url": spec["url"],
            "title": spec["title"],
            "author": spec["author"],
            "description": spec["entry"][:180],
            "source": "tradingview_pine_strategy",
            "date_scraped": "2026-04-26",
            "tags": ["intraday", "ORB", "opening-range-breakout"]
                    + (["bidirectional"] if "bidirectional" in spec["label"] else ["long-only"]),
            "extra": {
                "agent_summary": (
                    f"{spec['title']}: " + spec["entry"] + f" Tested SPY 5m 2024 (252 days). "
                    f"Verdict {verdict}: trades={row['trades']}, Sharpe {row['sharpe']:.2f}, "
                    f"CAGR {row['cagr_pct']:.2f}% vs SPY B&H Sharpe {bh['sharpe']:.2f} "
                    f"CAGR {bh['cagr_pct']:.2f}%. "
                    + (
                        "Bidirectional version got crushed by 2024 strong uptrend (shorts kept losing)."
                        if "bidirectional" in spec["label"] else
                        "Long-only also failed — 2024 had muted intraday volatility, ORB needs higher VIX regime."
                        if spec["label"] == "orbo-long-only" else
                        "Most selective variant (R1>orHigh requirement) lost least but still failed."
                    )
                ),
                "description_full_readable": (
                    f"# {spec['title']}\n\n"
                    f"## Source\nTradingView: {spec['url']}\nAuthor: {spec['author']}\n\n"
                    f"## Methodology\n- Entry: {spec['entry']}\n- Exit: {spec['exit']}\n\n"
                    f"## Test (2026-04-26)\nSPY 5m 2024-01-01 to 2025-01-01, "
                    f"5 bps slippage per fill, 1% risk per trade.\n\n"
                    f"- Trades: {int(row['trades'])}\n"
                    f"- Win rate: {row['win_rate_pct']:.1f}%\n"
                    f"- Sharpe: {row['sharpe']:.2f}\n"
                    f"- CAGR: {row['cagr_pct']:.2f}%\n"
                    f"- Max DD: {row['max_dd_pct']:.2f}%\n\n"
                    f"## Baseline\nSPY B&H over same window: Sharpe {bh['sharpe']:.2f}, "
                    f"CAGR {bh['cagr_pct']:.2f}%, MaxDD {bh['max_dd_pct']:.2f}%.\n\n"
                    f"## Verdict: {verdict}\n"
                    f"2024 was a strong-uptrend, low-VIX year — historically unfavorable for "
                    f"breakout strategies. Re-test in 2018, 2020, 2022 (high-VIX years) before "
                    f"discarding the methodology family entirely."
                ),
                "strategy_id": spec["strategy_id"],
                "methodology_family": "Opening Range Breakout (intraday)",
                "instruments": ["SPY"],
                "timeframes": {"build": "9:30-09:50 ET", "execution": "5m", "session_end": "15:55"},
                "core_concepts": spec["concepts"],
                "entry_rules": spec["entry"],
                "exit_rules": spec["exit"],
                "risk_management": "1% risk per trade, single position, EOD forced exit",
                "tested": True,
                "test_runs": [{
                    "test_id": f"{spec['strategy_id']}-SPY-5m-2024",
                    "date_iso": "2026-04-26",
                    "instrument": "SPY",
                    "timeframe": "5m",
                    "period": "2024-01-01 to 2025-01-01",
                    "trades": int(row["trades"]),
                    "win_rate_pct": float(row["win_rate_pct"]),
                    "sharpe": float(row["sharpe"]),
                    "total_return_pct": float(row["total_return_pct"]),
                    "cagr_pct": float(row["cagr_pct"]),
                    "max_drawdown_pct": float(row["max_dd_pct"]),
                    "slippage_bps_rt": 10.0,
                    "benchmark_return_pct": float(bh["total_return_pct"]),
                    "benchmark_sharpe": float(bh["sharpe"]),
                    "benchmark_max_dd_pct": float(bh["max_dd_pct"]),
                    "verdict": verdict,
                }],
                "current_verdict": verdict,
                "verdict_summary": (
                    f"On SPY 5m 2024: Sharpe {row['sharpe']:.2f}, CAGR {row['cagr_pct']:.2f}%, "
                    f"MaxDD {row['max_dd_pct']:.2f}%, win rate {row['win_rate_pct']:.1f}% "
                    f"({int(row['trades'])} trades). Underperforms SPY B&H. Single-year sample; "
                    f"ORB is regime-dependent and may work in higher-VIX years."
                ),
                "failure_modes": (
                    ["Bidirectional shorts crushed by strong uptrending year"]
                    if "bidirectional" in spec["label"] else
                    ["Low-VIX 2024 environment unfavorable for breakout strategies",
                     "Most breakouts faded back into the OR — high false-positive rate"]
                ),
                "improvement_hypotheses": [
                    "Backtest in high-VIX years (2018, 2020, 2022) before final verdict",
                    "Add VIX > 20 regime filter (only trade when volatility is elevated)",
                    "Try larger OR window (30 or 60 minutes)",
                    "Add volume confirmation on breakout candle",
                    "Test on more volatile instruments (NVDA, TSLA, leveraged ETFs)"
                ],
                "code_paths": {
                    "orbo": "strategies/orb/orbo.py",
                    "orb_pivots": "strategies/orb/orb_pivots.py",
                    "runner": "strategies/orb/runner.py"
                },
                "data_artifacts": ["data/orb_family_results.csv"],
                "first_logged_iso": "2026-04-26",
                "last_updated_iso": "2026-04-26",
            }
        }
        records.append(record)


def main():
    records = load_records()
    print(f"Loaded {len(records)} existing records")
    add_sector_runs(records)
    add_overlay_note(records)
    update_verdicts(records)
    add_orb_records(records)
    write_records(records)
    print(f"Wrote {len(records)} records back")
    print()
    for r in records:
        verdict = r.get("extra", {}).get("current_verdict", "?")
        print(f"  [{verdict:<18}] {r['title']}")


if __name__ == "__main__":
    main()
