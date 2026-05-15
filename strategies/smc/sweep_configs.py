"""
sweep_configs.py — Try several TJR config variations on the SAME data.
Honest: this risks overfitting. Treat any winning config as a hypothesis to
out-of-sample validate, not a green light to trade.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from collections import Counter
import pandas as pd

from backtest import BacktestEngine, load_bars, summarize
from strategies.smc.strategy import TJRConfig, TJRStrategy


def run(df, cfg, label):
    strat = TJRStrategy(symbol="SPY", ltf_df=df, config=cfg)
    engine = BacktestEngine(
        data={"SPY": df}, strategy=strat,
        initial_cash=100_000.0, slippage_bps=5.0,
    )
    portfolio = engine.run()
    rep = summarize(portfolio, periods_per_year=252 * 78)
    reasons = Counter(t["exit_reason"] for t in strat.trade_log)
    return {
        "label": label,
        "trades": rep.num_round_trips,
        "fills": rep.num_fills,
        "ret_pct": rep.total_return_pct,
        "cagr_pct": rep.cagr_pct,
        "sharpe": rep.sharpe,
        "mdd_pct": rep.max_drawdown_pct,
        "win_rate": rep.win_rate_pct,
        "exit_reasons": dict(reasons),
    }


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2025-01-01"
    print(f"Loading SPY 5m {start} -> {end} ...")
    df = load_bars(["SPY"], start=start, end=end, interval="5m")["SPY"]
    print(f"{len(df):,} bars\n")

    configs = [
        ("baseline (require HTF sweep, 2R)", TJRConfig()),
        ("no sweep req",  TJRConfig(require_htf_sweep=False)),
        ("3R target",     TJRConfig(r_multiple_target=3.0)),
        ("1R target",     TJRConfig(r_multiple_target=1.0)),
        ("longer hold",   TJRConfig(max_bars_in_trade=144)),
        ("tighter buffer", TJRConfig(stop_buffer_atr=0.10)),
        ("looser FVG",    TJRConfig(fvg_min_size_atr=0.10)),
        ("no sweep + 1R", TJRConfig(require_htf_sweep=False, r_multiple_target=1.0)),
        ("no sweep + 3R", TJRConfig(require_htf_sweep=False, r_multiple_target=3.0)),
    ]

    rows = [run(df, c, lbl) for lbl, c in configs]
    rows_df = pd.DataFrame(rows)
    print(rows_df.to_string(index=False))


if __name__ == "__main__":
    main()
