"""
_seed_records.py — Generate the initial records.jsonl from in-code records.
Run once to bootstrap the bundle. After that, edit records.jsonl directly
and re-run build.py.

    py -3.13 _seed_records.py
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "records.jsonl"


TJR_DESCRIPTION_FULL = """\
# TJR Smart Money Concepts (2025 Day Trading Tutorial)

## Source
- ~9-hour YouTube video transcript hosted on Google Docs
- 359-page document; ~50% mindset/psychology, ~30% sales pitch for paid Blueprint
  mentorship, ~20% actual tradable mechanics
- Author: Tyler "TJR" Robinson — retail SMC/ICT-style influencer

## Methodology — multi-timeframe state machine

**Step 1**: 4-hour bias from swing structure (HH/HL = bull, LH/LL = bear).
**Step 2**: 1-hour bias.
  - Aligned with 4h → execute on 5m
  - Opposed → execute on 15m
**Step 3**: Wait for an HTF (1h or 4h) liquidity sweep against the bias direction.
  - Bullish bias → wait for low to be swept (stop run below recent swing low).
  - Bearish bias → wait for high to be swept.
**Step 4**: Wait for a Break of Structure (BOS) on the execution timeframe in
  the bias direction.
**Step 5**: Wait for price to revisit a "third confluence" zone:
  - Fair Value Gap (FVG) — 3-bar imbalance
  - Order Block (OB) — last opposing candle before displacement
  - Breaker block — failed OB that flips polarity
  - Equilibrium — 50% retrace of impulse leg
  - BPR (Balance Price Range) — opposite-direction FVG overlap
**Step 6**: Enter on bullish/bearish reaction. Optional: scale to 1m for tighter
  entry via 1m BOS.

## Risk
- Stop placement (preference order):
  1. Above/below the liquidity sweep extreme (most invalidating)
  2. Above/below the HL/LH inside the confluence
  3. Above/below the whole confluence zone
- Target: 2R fixed, then trailing on next draws on liquidity
- Never extend stops or targets after entry
- "Three strikes you're out" daily loss rule

## Implementation built (Phase B Sprint 1)
- `strategies/smc/primitives.py` — swing points, ATR, FVG, OB, equilibrium
- `strategies/smc/structure.py` — BOS detection, liquidity sweep, multi-TF bias
- `strategies/smc/strategy.py` — full state machine with risk-anchored sizing
- `strategies/smc/backtest_run.py` — runner + buy-and-hold comparison
- `strategies/smc/sweep_configs.py` — parameter variant sweep

## Test results (2026-04-26)
Backtested on SPY 5m via Alpaca IEX feed across three out-of-sample years:

| Year | Strategy return | B&H return | Strategy Sharpe | B&H Sharpe | Trades | Win rate |
|------|---|---|---|---|---|---|
| 2022 (bear) | -5.09% | -19.53% | -1.62 | -0.79 | 28 | 28.6% |
| 2023 (bull) | -0.76% | +23.09% | -0.32 | +1.60 | 24 | 54.2% |
| 2024 (bull) | -1.59% | +23.66% | -1.09 | +1.70 | 23 | 30.4% |

3-year combined: ≈ -7.4% strategy vs ≈ +27% B&H. Strategy underperformed
buy-and-hold every year, on Sharpe and on absolute return.

## Why it failed
1. **Counter-trend logic in trending markets** — 2023 and 2024 were strong bull
   years; the strategy spent them shorting bullish breakouts (after sweep of
   highs) and buying bearish sweeps that got reversed.
2. **Late entries** — by the time HTF sweep + LTF BOS + confluence revisit have
   all confirmed, much of the move has already happened. 50%+ of trades
   time-out (3 hours, no resolution) — neither stop nor target hit.
3. **Subjective primitives at retail latency** — FVG/OB detection on 5m bars
   has many false positives that visual screen-time filters but a mechanical
   engine cannot.
4. **Matches academic literature** — published studies on ICT/SMC at retail
   latency consistently find no risk-adjusted edge after costs.

## Improvement hypotheses (not pursued)
- Add 4h bias filter (was used for direction but not as veto)
- Add session/kill-zone time filters (London open, NY open)
- Add news catalyst veto (skip CPI/FOMC days, which TJR himself recommends)
- Try BPR overlap instead of single FVGs
- Increase displacement threshold for OB detection (filter false signals)
- Better entry: scale to 1m on confluence touch and wait for 1m BOS (TJR's
  "better entry" technique) — not implemented in v1
"""


ROSS_DESCRIPTION_FULL = """\
# Ross Cameron Five-Pillar Momentum (Warrior Trading)

## Source
- ~3-hour video transcript captured to Word document (38,539 words)
- Author: Ross Cameron — Warrior Trading founder
- Audited PnL: $583 → $12.3M (2017-2024)
- Sales funnel for paid Warrior Pro mentorship + Day Trade Dash software

## Methodology — Five Pillars + Micro-Pullback

### Universe filter (Five Pillars)
| Pillar | Threshold | Why |
|---|---|---|
| Price | $1–$20 (sweet spot $2–$10) | Affordability creates retail demand |
| Float | < 10M shares | Low supply → big % moves on demand |
| News catalyst | Within 2–12 hours | The demand spark |
| Daily % gain | ≥ 25% | Already in motion |
| Relative volume | ≥ 5× (ideally 10×) | Confirms attention |

### Setup ("micro-pullback" / "bull flag")
1. Stock makes 3+ green 1m candles (impulse)
2. Pulls back 1–3 red candles on lighter volume
3. Wait for first candle to make a new high vs prior candle
4. Entry at that breakout

### Stop and target
- Stop = low of pullback
- Target 1 = high of day, target 2 = 2R, target 3 = 3R (scale out)
- Multiple exit indicators (any one triggers): high-vol red candle, MACD cross,
  topping tail/doji, VWAP break, 9 EMA break, big seller on level 2

### Time of day
- 7:00–10:00 AM ET (pre-market + first 30 min)
- Optimal "macro" window 1:30–3:00 PM ET for PM session

### Position sizing & meta-rules
- 1% risk per trade
- First trade quarter or half size; full only after a winner
- Max 3 losses in a row → stop for the day
- Trade only 1st and 2nd pullback (not 3rd+)
- After big loss: cut share size to 25% until 50% recovery

## Implementation built (Phase B Sprint 2)

### Strategy 1: Universe drift baseline
- `backtest/polygon_data.py` — Polygon grouped daily fetcher (rate-limited,
  cached; uses adjusted=False to match Alpaca raw intraday)
- `strategies/momentum/scanner.py` — Five-Pillar filter (3 of 5 pillars
  applied: price, gap%, absolute volume; float and news filters deferred)
- `strategies/momentum/drift.py` — minute-bar drift measurement at multiple
  horizons (30min, 60min, 2h, close), with MFE/MAE
- `strategies/momentum/baseline_run.py` — orchestrator + reporter

### Strategy 2: Mechanical micro-pullback execution
- `strategies/momentum/execution.py` — state machine (WAIT_IMPULSE →
  WAIT_PULLBACK → ARMED → IN_TRADE), with EMA-9 trailing exit, slippage
  modeling, R-multiple tracking, pullback ordinal counting
- `strategies/momentum/strategy2_run.py` — runner with slippage sensitivity
  sweep (10/50/100/200 bps round-trip)

## Test results (2026-04-26)

### Strategy 1 — June 2024 universe drift (50 qualifiers, 17 days)

| Horizon | Mean | Median | Hit rate |
|---|---|---|---|
| +30 min | +1.96% | -1.09% | 40.8% |
| +60 min | -1.32% | -3.07% | 34.7% |
| +2 hour | -1.31% | -4.16% | 34.0% |
| close   | -3.16% | -4.92% | 32.0% |

**Path characteristics — KEY FINDING:**
- Mean max favorable excursion (peak from open): +25.7% (median +14.3%)
- Mean max adverse excursion (trough from open): -15.1% (median -12.9%)

**Verdict: PASS_WITH_NUANCE** — naive buy-and-hold loses (gappers fade by
close), BUT the universe has asymmetric intraday volatility (bigger peaks
than troughs). The opportunity is in capturing the upside before the fade.

### Strategy 2 — June 2024 execution (16 trades)

| Slippage RT | Win rate | Avg R | PF | Sharpe | $ P&L |
|---|---|---|---|---|---|
| 10 bps | 56.2% | +0.35 | 1.76 | 4.44 | +$555 |
| 50 bps | 56.2% | +0.26 | 1.53 | 3.28 | +$409 |
| **100 bps** | **56.2%** | **+0.18** | **1.34** | **2.18** | **+$271** |
| 200 bps | 37.5% | -0.13 | 0.79 | -1.66 | -$224 |

Looked promising — survives 100 bps RT slippage with Sharpe 2.18.

### Strategy 2 — April-June 2024 (48 trades, 41 days, extended sample)

Polygon free tier capped us at 2 years history (NOT_AUTHORIZED for dates
before April 2024). Got 3 months instead of the planned 6.

| Slippage RT | Win rate | Avg R | PF | Sharpe | $ P&L |
|---|---|---|---|---|---|
| 10 bps | 52.1% | +0.22 | 1.37 | 2.91 | +$1,046 |
| 50 bps | 50.0% | +0.07 | 1.10 | 0.88 | +$294 |
| **100 bps** | **39.6%** | **-0.16** | **0.75** | **-2.57** | **-$789** |
| 200 bps | 29.2% | -0.42 | 0.49 | -6.69 | -$1,863 |

**Verdict: FAIL** — June was a small-sample fluke. Triple the trades, the
mechanic loses at realistic 100 bps RT slippage. Stop:target ratio is 2:1
(26 stops, 13 targets) — entries are systematically too late.

### Pullback ordinal breakdown (extended sample, 100 bps RT)
- Pullback #1: n=36, WR 38.9%, avg -0.19R
- Pullback #2: n=12, WR 41.7%, avg -0.07R

Pullback #2 is marginally better than #1, weakly consistent with Ross's claim,
but neither is profitable mechanically.

## Why it failed
1. **Late entries** — buying the first new high after a pullback often catches
   the back end of the squeeze. By the time the trigger fires, the easy money
   is gone and you're in for the fade.
2. **Slippage compounds against small caps** — $2-$10 stocks have 0.5–2%
   bid/ask spreads. Round-trip costs eat the entire mechanical edge.
3. **The published edge probably exists in things not modeled** — visual tape
   reading, level 2 order flow, discretionary stock-picking from scanner
   output, early stop tightening, position-by-position sizing decisions.
4. **Strategy 1 finding is informative** — universe selection is real
   (asymmetric MFE/MAE). The execution mechanic just doesn't capture it
   without human/AI judgment in the loop.

## Improvement hypotheses (not pursued)
- Strategy 6: News-catalyst attribution filter (Polygon News API, key wired)
- Strategy 9: Float < 5M filter (yfinance, more restrictive supply)
- Restrict to first 30 minutes only (avoid late entries during fade phase)
- Test 1.5R targets (higher win rate even if smaller per-trade gain)
- Require VWAP + 9 EMA confluence at entry, not just pattern
- Use LLM (Ollama qwen2.5) to triage scanner output: read news catalyst, score
  legitimacy, veto pump-and-dumps before they enter the universe
"""


def main():
    records = []

    records.append({
        "url": "https://docs.google.com/document/d/1i7op3dFjbl8dzT_c9QmrzD07VajNPj-0nO8RLsL-3vo",
        "title": "TJR Day Trading Tutorial 2025 — Smart Money Concepts",
        "author": "Tyler 'TJR' Robinson",
        "description": "Multi-timeframe SMC/ICT state machine: HTF bias → liquidity sweep → BOS → confluence entry. Tested on SPY 5m 2022-2024.",
        "source": "youtube_transcript via google_docs",
        "date_scraped": "2026-04-26",
        "tags": ["SMC", "ICT", "discretionary", "multi-timeframe", "intraday", "fail"],
        "extra": {
            "agent_summary": (
                "TJR's published Smart Money Concepts repackaging of ICT methodology. "
                "Multi-timeframe state machine fails on SPY 5m across 2022-2024 "
                "(Sharpe -1.0, return -7.4% vs SPY +27%). Counter-trend logic "
                "loses in trending markets; FVG/OB primitives have too many false "
                "positives at retail latency. Three years of consistent negative "
                "results. Matches academic findings on ICT/SMC mechanizability."
            ),
            "description_full_readable": TJR_DESCRIPTION_FULL,
            "strategy_id": "tjr-smc-2025",
            "methodology_family": "Smart Money Concepts / ICT",
            "instruments": ["SPY", "ES", "NQ", "GBP/USD", "GBP/JPY", "Gold"],
            "timeframes": {
                "bias": ["4h", "1h"],
                "execution": ["5m", "15m"],
                "better_entry": "1m"
            },
            "core_concepts": [
                "Liquidity sweep",
                "Break of Structure (BOS)",
                "Fair Value Gap (FVG)",
                "Order Block (OB)",
                "Breaker block",
                "Equilibrium (50% retrace)",
                "BPR (Balance Price Range)",
                "Multi-TF state machine"
            ],
            "entry_rules": (
                "1. 4h bias from swing structure. "
                "2. 1h bias; if aligned with 4h execute on 5m, else 15m. "
                "3. Wait for HTF liquidity sweep against bias direction. "
                "4. Wait for LTF BOS in bias direction. "
                "5. Enter on price revisit to FVG/OB/equilibrium/breaker/BPR confluence."
            ),
            "exit_rules": (
                "Stop: above/below sweep extreme (preferred), then HL/LH in confluence, "
                "then beyond confluence zone. Target: 2R fixed multiple, then trailing "
                "on next draws on liquidity. Never extend stops or targets after entry."
            ),
            "risk_management": "1% per trade, 'three strikes you're out' daily limit",
            "tested": True,
            "test_runs": [
                {
                    "test_id": "tjr-spy-5m-2022",
                    "date_iso": "2026-04-26",
                    "instrument": "SPY",
                    "timeframe": "5m",
                    "period": "2022-01-01 to 2023-01-01",
                    "trades": 28,
                    "win_rate_pct": 28.6,
                    "profit_factor": None,
                    "sharpe": -1.62,
                    "total_return_pct": -5.09,
                    "max_drawdown_pct": -5.69,
                    "benchmark_return_pct": -19.53,
                    "benchmark_sharpe": -0.79,
                    "verdict": "FAIL"
                },
                {
                    "test_id": "tjr-spy-5m-2023",
                    "date_iso": "2026-04-26",
                    "instrument": "SPY",
                    "timeframe": "5m",
                    "period": "2023-01-01 to 2024-01-01",
                    "trades": 24,
                    "win_rate_pct": 54.2,
                    "profit_factor": None,
                    "sharpe": -0.32,
                    "total_return_pct": -0.76,
                    "max_drawdown_pct": -2.95,
                    "benchmark_return_pct": 23.09,
                    "benchmark_sharpe": 1.60,
                    "verdict": "FAIL"
                },
                {
                    "test_id": "tjr-spy-5m-2024",
                    "date_iso": "2026-04-26",
                    "instrument": "SPY",
                    "timeframe": "5m",
                    "period": "2024-01-01 to 2025-01-01",
                    "trades": 23,
                    "win_rate_pct": 30.4,
                    "profit_factor": None,
                    "sharpe": -1.09,
                    "total_return_pct": -1.59,
                    "max_drawdown_pct": -2.55,
                    "benchmark_return_pct": 23.66,
                    "benchmark_sharpe": 1.70,
                    "verdict": "FAIL"
                }
            ],
            "current_verdict": "FAIL",
            "verdict_summary": (
                "Strategy fails to beat SPY buy-and-hold across three out-of-sample "
                "years on SPY 5m (-7.4% vs +27%). Counter-trend logic loses in "
                "trending markets. ~50% of trades time-out without resolution, "
                "indicating entries are too late. Consistent with academic literature "
                "on retail-latency ICT/SMC strategies."
            ),
            "failure_modes": [
                "Counter-trend during strong trending years (2023, 2024)",
                "Late entries — 50%+ of trades time-out",
                "FVG/OB detection has high false-positive rate at retail latency",
                "Subjective primitives don't mechanize cleanly"
            ],
            "improvement_hypotheses": [
                "Add session/kill-zone time filters (London/NY open)",
                "Add news catalyst veto (CPI/FOMC blackout)",
                "Test BPR overlap instead of single FVGs",
                "Increase OB displacement threshold to filter false signals",
                "Implement 1m BOS 'better entry' (not in v1)",
                "Try ATR-scaled targets instead of fixed 2R"
            ],
            "code_paths": {
                "primitives": "strategies/smc/primitives.py",
                "structure": "strategies/smc/structure.py",
                "strategy": "strategies/smc/strategy.py",
                "backtest_runner": "strategies/smc/backtest_run.py",
                "config_sweep": "strategies/smc/sweep_configs.py"
            },
            "data_artifacts": [],
            "first_logged_iso": "2026-04-26",
            "last_updated_iso": "2026-04-26"
        }
    })

    records.append({
        "url": "file:///Z:/Downloads/Ross Camera Day Trade Guide.docx",
        "title": "Ross Cameron Beginner's Day Trade Tutorial — Five-Pillar Momentum",
        "author": "Ross Cameron (Warrior Trading)",
        "description": "Pre-market gap scanner (Five Pillars) + micro-pullback 1m execution. Universe selection works; mechanical entry fails after realistic small-cap slippage.",
        "source": "video_transcript via word_doc",
        "date_scraped": "2026-04-26",
        "tags": ["momentum", "small-cap", "gap-and-go", "warrior-trading", "intraday", "fail"],
        "extra": {
            "agent_summary": (
                "Ross Cameron's Five-Pillar gap-and-go strategy. Universe shape is "
                "real (gappers have +25.7% mean MFE / -15.1% MAE asymmetry — Strategy 1 "
                "PASS_WITH_NUANCE) but mechanical micro-pullback execution fails at "
                "realistic 100bps RT slippage (Apr-Jun 2024: 48 trades, 39.6% WR, "
                "PF 0.75, Sharpe -2.57). Stop:target ratio of 2:1 indicates entries "
                "are too late. Slippage on $2-$10 stocks eats the edge. Edge probably "
                "exists in discretionary tape reading and L2 order flow — not "
                "mechanizable from the doc alone."
            ),
            "description_full_readable": ROSS_DESCRIPTION_FULL,
            "strategy_id": "ross-cameron-five-pillar",
            "methodology_family": "Small-cap momentum / gap-and-go",
            "instruments": ["US small-caps $1-$20", "low-float news gappers"],
            "timeframes": {
                "scan": "daily premarket",
                "execution": "1m",
                "context": ["10s", "5m", "daily"]
            },
            "core_concepts": [
                "Five Pillars stock selection",
                "Micro-pullback (bull flag) entry",
                "MACD + Volume filter",
                "VWAP / 9 EMA / 20 EMA / 200 EMA",
                "First/second pullback only rule",
                "9 EMA trailing exit",
                "Three-strikes-you're-out daily limit",
                "Halt-and-go (paused trading auctions)"
            ],
            "entry_rules": (
                "Universe (Five Pillars): price $1-$20, float < 10M, news catalyst "
                "within 2-12h, gap >= 25%, RVol >= 5x. Pattern: 3+ green 1m candles "
                "(impulse) → 1-3 red pullback on lighter volume → first bar making "
                "new high vs prior bar. Trade 1st and 2nd pullback only."
            ),
            "exit_rules": (
                "Stop = pullback low. Targets: HOD / 2R / 3R (scale out). Early exit on "
                "any: high-volume red candle, MACD cross down, topping tail/doji, "
                "VWAP break, 9 EMA break, big seller on level 2."
            ),
            "risk_management": (
                "1% risk per trade. First trade quarter or half size; full only after "
                "winner. Max 3 losses in a row → stop for day. After big loss: 25% "
                "size until 50% recovery."
            ),
            "tested": True,
            "test_runs": [
                {
                    "test_id": "ross-strategy1-drift-jun2024",
                    "date_iso": "2026-04-26",
                    "metric": "open-to-close drift baseline",
                    "instrument": "Five-Pillar gapper universe",
                    "timeframe": "1m",
                    "period": "2024-06-01 to 2024-06-30",
                    "qualifiers": 50,
                    "trading_days": 17,
                    "mean_open_to_close_pct": -3.16,
                    "median_open_to_close_pct": -4.92,
                    "hit_rate_pct": 32.0,
                    "mean_max_favorable_excursion_pct": 25.7,
                    "mean_max_adverse_excursion_pct": -15.1,
                    "verdict": "PASS_WITH_NUANCE"
                },
                {
                    "test_id": "ross-strategy2-execution-jun2024",
                    "date_iso": "2026-04-26",
                    "metric": "mechanical micro-pullback execution",
                    "instrument": "Five-Pillar gapper universe",
                    "timeframe": "1m",
                    "period": "2024-06-01 to 2024-06-30",
                    "trades": 16,
                    "win_rate_pct": 56.2,
                    "avg_r": 0.18,
                    "profit_factor": 1.34,
                    "sharpe": 2.18,
                    "max_drawdown_pct": -3.6,
                    "slippage_bps_rt": 100,
                    "verdict": "MARGINAL",
                    "note": "Small sample fluke — see extended run"
                },
                {
                    "test_id": "ross-strategy2-execution-apr-jun2024",
                    "date_iso": "2026-04-26",
                    "metric": "mechanical micro-pullback execution (extended)",
                    "instrument": "Five-Pillar gapper universe",
                    "timeframe": "1m",
                    "period": "2024-04-01 to 2024-06-30",
                    "trades": 48,
                    "trading_days": 41,
                    "win_rate_pct": 39.6,
                    "avg_r": -0.16,
                    "profit_factor": 0.75,
                    "sharpe": -2.57,
                    "max_drawdown_pct": -20.1,
                    "slippage_bps_rt": 100,
                    "verdict": "FAIL",
                    "note": "Polygon free tier capped at 2 years history; full 6-month plan blocked"
                }
            ],
            "current_verdict": "FAIL",
            "verdict_summary": (
                "Strategy 1 (universe scan) PASSes — Five-Pillar gappers have real "
                "asymmetric intraday volatility (+25.7% MFE > -15.1% MAE). "
                "Strategy 2 (mechanical execution) FAILs at realistic 100bps RT "
                "slippage on extended sample. June-only result was small-sample "
                "fluke. Stop:target ratio 2:1 indicates systematic late entry. "
                "Edge likely lives in discretionary judgment + L2 tape reading "
                "that the doc describes but doesn't mechanize."
            ),
            "failure_modes": [
                "Late entries — 'first new high after pullback' catches end of squeeze",
                "Stop:target ratio 2:1 (26 stops vs 13 targets in extended sample)",
                "Small-cap bid/ask spreads (50-200 bps) eat mechanical edge",
                "Universe contains pump-and-dumps without legitimate news",
                "No news/legitimacy filter applied in v1 (Pillar 3 deferred)"
            ],
            "improvement_hypotheses": [
                "Strategy 6: news catalyst attribution via Polygon News API",
                "Strategy 9: float < 5M filter via yfinance",
                "Restrict to first 30 minutes (avoid fade phase)",
                "Try 1.5R targets vs 2R (higher win rate)",
                "Require VWAP + 9 EMA confluence at entry",
                "LLM (Ollama qwen2.5) news triage to veto pump-and-dumps",
                "Tighter ATR-scaled stops on entry",
                "Test halt-and-go re-entry on resumption auction"
            ],
            "code_paths": {
                "polygon_loader": "backtest/polygon_data.py",
                "scanner": "strategies/momentum/scanner.py",
                "drift_measurement": "strategies/momentum/drift.py",
                "execution": "strategies/momentum/execution.py",
                "strategy1_runner": "strategies/momentum/baseline_run.py",
                "strategy2_runner": "strategies/momentum/strategy2_run.py"
            },
            "data_artifacts": [
                "data/momentum_drift_results.csv",
                "data/strategy2_trades.csv"
            ],
            "first_logged_iso": "2026-04-26",
            "last_updated_iso": "2026-04-26"
        }
    })

    with open(OUT, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Wrote {len(records)} records to {OUT}")


if __name__ == "__main__":
    main()
