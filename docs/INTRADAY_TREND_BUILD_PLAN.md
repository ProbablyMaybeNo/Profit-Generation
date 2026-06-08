# Intraday Trend-Following Build Plan

**Created:** 2026-06-08
**Owner:** Ross
**Goal:** A single-purpose, automated intraday **long-only trend-following** system: detect the start of an upward move via candlestick patterns + confirmation, buy, place an ATR stop, ratchet the stop up as price rises, and exit on a trailing-stop hit or a bearish reversal pattern. Profile = **many small wins across many liquid symbols**.

This document is the durable reference. Nothing advances a stage until its **Go-gate is 100% green**. Every green gate gets a dated line in the Build Log at the bottom.

---

## 1. Scope (and what is explicitly OUT)

**In scope:** one strategy family — intraday bullish-continuation entries, ATR initial stop, ratcheting trailing stop, bearish-pattern + trailing exit.

**Out of scope (do not touch):** EOD mean-reversion, the Donchian trend strategy, the paused 18 strategies, crypto, shorts, options. This build does not modify any existing live strategy.

---

## 2. Strategy spec

Long-only intraday continuation. Per symbol in the watchlist, on each new closed bar:

1. **Entry** — bullish pattern fires **with confirmation** (uptrend filter + volume) → market buy.
2. **Initial stop** — ATR-based, placed immediately (`monitoring/stops.py`).
3. **Trail** — stop ratchets up on new highs only (`monitoring/trailing_stops.py`, `atr_trail`).
4. **Exit** — first of: trailing stop crossed, **or** bearish reversal pattern fires.
5. **Sizing** — intraday profile: 0.5× multiplier, `$800` floor, capped by `max_open_positions: 12`.

**Honest premise:** candlestick patterns in isolation have weak intraday edge. The edge lives in **exit discipline (trailing stop) + universe selection**; the pattern is the *trigger*, not the *edge*. Confirmation filters and measurement are therefore front-loaded.

---

## 3. What already exists (verified 2026-06-08)

| Capability | Status | Location |
|---|---|---|
| Intraday bar loading (1m/5m/15m, Alpaca IEX, cached) | ✅ works | `backtest/data.py` `load_intraday_bars()` |
| Strategy contract `compute_fn(df)→df` w/ `long_entry`/`long_exit` | ✅ works | `monitoring/config.py` + `strategies/` |
| Initial ATR stop, sub-penny-safe quantization | ✅ works (bug fixed) | `monitoring/stops.py` `quantize_stop_price` |
| Trailing stop (atr_trail/chandelier/percent, ratchet-up, floored) | ✅ works | `monitoring/trailing_stops.py` |
| Intraday execution pipeline | ✅ works | `monitoring/auto_trader_intraday.py` `process_intraday()` |
| Backtest engine (bar-by-bar, next-bar-open fills) | ✅ works | `backtest/engine.py` |
| **Candlestick pattern detection** | ❌ **does not exist** | — (build target) |
| **Intraday outcome measurement (MFE/MAE, closed outcomes)** | ❌ **gap — all outcomes are `1d`** | Stage 0 target |
| Historical intraday bars for backtesting (months of depth) | ❌ **data gap** | Track B target |

---

## 4. Decisions (locked)

- **Universe:** liquid high-beta singles + 3× ETFs — NVDA, TSLA, AMD, COIN, PLTR, AMZN, META, AAPL + TQQQ, SOXL, SPXL + existing liquid sector/index ETFs. Screened by $-volume and intraday ATR% into `data/universes/intraday_candidates.csv`. **Low-float small-cap gappers stay as research only — not the live universe** (halts/gaps/slippage are hostile to automation on Alpaca).
- **Validation:** **two tracks in parallel.**
  - **Track A (live, gated):** Stages 0→8, one trading day per gate.
  - **Track B (offline, no capital):** collect historical intraday bars → backtest the pattern library → expectancy numbers. Runs ahead of Track A without blocking it.
  - **Reconciliation (Stage 8):** go/no-go = live expectancy **agrees with** backtest expectancy. Disagreement is itself the diagnostic (overfit vs. execution drag).

---

## 5. Staged build plan

Each stage: **Build → Test (unit) → Verify (real market data) → Go-gate**. Do not advance until the gate is 100% green. Stages 0–1 risk zero capital; capital enters at Stage 4, tiny.

| Stage | Build | Verify with real data | Go-gate (100%) | Failure signature → look at |
|---|---|---|---|---|
| **0. Measurement** | Close intraday `outcomes` on every intraday exit; record MFE/MAE; add intraday section to daily report | One live intraday day (or manual position) | Every intraday entry → matching closed outcome w/ non-null exit_price, exit_reason, MFE, MAE | outcome-closing path in `auto_trader_intraday` exit branch |
| **1. Pattern library** | `strategies/intraday/candle_patterns.py` — pure fns: bullish engulfing, hammer, morning star (entry); bearish engulfing, shooting star (exit) | Unit fixtures from textbook bars + ~20 real bars vs chart | 100% fixtures classify correctly; no-lookahead shift test passes | failing fixture; body/wick ratio thresholds |
| **2. Backtest** *(Track B)* | Run patterns through `backtest/engine.py` on historical 1m/5m/15m bars | Backtest candidate universe 6–12mo (needs Track B data) | Backtest completes; per-pattern expectancy + trade count → CSV in `data/` | data coverage gaps; pattern firing 0× (thresholds too tight) |
| **3. Signal-only (paper)** | Register one pattern strategy, observe-only on 3–5 symbols | Live day; signals → `signals` table | 100% of day's fires are correct pattern instances (spot-check each vs chart) | `intraday_fires.py` interval handling; `bar_ts` timezone |
| **4. Entry + initial stop (paper, tiny)** | Flip strategy live, smallest size, 3 symbols | Live day | Every fill has ATR stop **resting on Alpaca book** (verify via broker); no sub-penny rejects | `submit_atr_stop` return; Alpaca order status |
| **5. Trailing ratchet** | Enable `atr_trail` | Live day w/ ≥1 winner | Stop rises with new highs in `trailing_stops`; exit fires when crossed | `advance_stop` / `should_exit_on_trailing_stop` |
| **6. Bearish-pattern exit** | Add bearish patterns to `long_exit` | Live day | ≥1 exit attributed to pattern (not just stop); beats stop on MFE-capture spot-check | exit precedence (trailing vs signal) |
| **7. Universe scale** | Expand to full liquid watchlist | Live day | Portfolio respects `max_open_positions`/`max_open_per_strategy`; no oversell/short; slippage acceptable | `risk.py` gates; `position_manager` ownership |
| **8. Edge decision** | Measurement only | 10–20 live days + Track B reconcile | Live expectancy > 0 after costs AND agrees with backtest | red → kill or re-tune; honest go/no-go on the whole idea |

### Critical path (parallel tracks)

```
Day 0 : Stage 0 (measurement) + kick off Track B data collection
Day 1 : Stage 1 (pattern library)      | Track B: backtest as data lands
Day 2 : Stage 3 (signal-only paper)    | Track B: per-pattern expectancy CSV
Day 3 : Stage 4 (entry + initial stop, tiny)
Day 4 : Stage 5 (trailing ratchet)
Day 5 : Stage 6 (bearish-pattern exit)
Day 6 : Stage 7 (universe scale)
Day 7+: Stage 8 (edge decision, live-vs-backtest reconcile)
```
Stage 2 lives entirely in Track B, so it never blocks the live path.

---

## 6. Additions to collect / research

**Data (biggest gap):**
- Historical intraday bars (1m/5m/15m) for the candidate universe, 6–12 months (Alpaca historical). Blocks Stage 2.
- Per-symbol intraday profile: avg daily $-volume, intraday ATR%, RVOL by time-of-day → `data/universes/intraday_candidates.csv`.
- Intraday gap data + existing earnings calendar (for `earnings_veto`).

**Strategies to add (continuation > reversal for "ride the trend"):**
- Bull-flag / pullback-to-VWAP continuation (highest priority).
- EMA9-over-EMA20 trend filter (gate every candle entry behind "in an uptrend").
- Opening-range breakout as a trend-start trigger (reuse existing ORB code).
- Higher-high/higher-low market-structure confirmation.

**Research to add to DB:**
- Bulkowski candlestick hit-rate statistics (which patterns actually have intraday edge).
- Pattern-plus-context (volume/trend confirmation) lift studies.
- Intraday seasonality (open drive / lunch lull / power hour) for time-of-day filters.

### Research-driven refinements (2026-06-08, from `docs/INTRADAY_RESEARCH_FINDINGS.md`)

- **Entry is a confirmation chain, not a pattern.** Require ≥3 of 5: (1) symbol uptrend (price > EMA20, EMA9>EMA20), (2) VWAP alignment (above or reclaiming), (3) pattern at a level (EMA9/EMA20/VWAP/prior-day-high/round number), (4) pattern-bar volume > 1.0× avg (1.5× ideal), (5) bullish pattern trigger. Evidence: Morning Star + basic filters = PF 0.79 (loser); Three White Soldiers + RSI<35 = 83% WR.
- **Entry pattern priority:** Three White Soldiers, Hammer, Bullish Engulfing, Morning Star, Piercing.
- **Exit pattern priority:** Bearish Engulfing (79%), Evening Star (72%). **Shooting Star is near-random (59%) — never an exit on its own.**
- **Time-of-day filter (free Sharpe):** allow new longs only 09:30–11:00 + 12:00–14:00 ET; **block new entries 11:00–12:00**; no new entries after 14:30 unless a clear power-hour trend. (Quantpedia SPY 2010–2024.)
- **EMA9 crossover alone loses on US equities** — use EMA9/EMA20 as *levels/filters*, not crossover entries.
- **ORB** is the highest-quantified single edge (74.56% WR, PF 2.5 on NQ, uptrend period) — add as a trend-start trigger but forward-test on our equities before trusting.
- **Universe tiers (live data, `data/universes/intraday_candidates.csv`):** Tier 1 AMD/TSLA/PLTR/NVDA/COIN; Tier 2 META/TQQQ/SOXL/SMH/AMZN; SOXL = wide ATR stop, same-day only; **SPY/QQQ/IWM/XLK = trend filters, not trade vehicles** (range too small).

---

## 7. Stage 0 status (2026-06-08)

Real-data verification corrected two assumptions:

- **Intraday outcomes ARE measured.** 176 closed intraday outcomes exist with MFE/MAE (155×1m, 15×5m, 6×15m). The earlier "intraday unmeasured / all outcomes are 1d" claim was false. The close + excursion code is in place (`auto_trader._process_exit` for in-loop exits; `close_intraday_positions._close_outcome_for_eod` for the EOD flatten, which correctly uses the `intraday_bars` table for intraday-resolution MFE/MAE).
- **The real Stage 0 gap is lifecycle RELIABILITY, not measurement.** Exit-reason breakdown (all-time): `eod_close` 10 + `trailing_stop` 2 + `long_exit_signal` 5 = **12 clean (7%)** vs `stale_intraday_flatten_missed` 114 + `reconciled_no_position` 45 = **159 leaked (90%)**. Intraday positions historically were not flattened same-session; they leaked overnight/over-days until a later sweep booked them (often at −4% to −9%). The EOD flatten (`close_intraday_positions`, wired in `run_daily.bat:29`) plus the M5–M9 commits (paused-strategy flatten, M6 flat-assertion, M7 post-fill stop verify) were built to cure this; the cure is **unproven on a clean live day**.

**Instrument built:** `scripts/verify_intraday_lifecycle.py` (read-only) + `tests/test_verify_intraday_lifecycle.py` (7 tests, green). Turns each session into a GREEN/RED gate: green only when every intraday entry that session closed clean (`eod_close`/`trailing_stop`/`long_exit_signal`) AND fully measured, with no overnight carry. Baseline: every recent session RED except 2026-05-20; **`open=0` now** (no current overnight carry).

**Stage 0 go-gate (revised):** the verifier reports **GREEN for one full live session** with ≥1 intraday entry. Because the intraday strategies are currently paused (Donchian-only), the first GREEN session will occur when Stage 4 puts the first candle-pattern position on — so Stage 0's *instrument* is done; its *gate* is cleared during Stage 4's first clean day. Run each trading day:
`py -3.13 -m scripts.verify_intraday_lifecycle --session <YYYY-MM-DD>`

## 8. Build Log

Append one dated line per green gate.

- _(2026-06-08) Plan created._
- _(2026-06-08) Stage 0 instrument shipped — `verify_intraday_lifecycle.py` + 7 tests green; baseline 7% clean (12/176), no current overnight carry. Gate pends first live intraday session._
- _(2026-06-08) Stage 1 GREEN — `strategies/intraday/candle_patterns.py` (5 bullish + 3 bearish pure detectors) + `tests/test_candle_patterns.py` (10 tests incl. no-lookahead property). Verified on real AMD 5m bars (162): sensible fire counts, eyeballed hammers textbook-correct. Detectors are triggers only — confirmation chain comes in the entry strategy (Stage 3+)._
- _(2026-06-08) Continuation entry strategy built — `strategies/intraday/candle_continuation.py` (3-of-5 confirmation chain: trend/VWAP/level/volume/pattern + hard time-of-day filter; bearish-pattern + EMA-break exits) + `tests/test_candle_continuation.py` (8 tests). Real-data smoke: AMD 7 entries, NVDA 10 (all in active windows, none in 11-12 lull), TSLA 0 (downtrend correctly blocked). Shared dependency for Stage 2 (backtest) and Stage 3 (signal-only paper)._
- _(2026-06-08) Build-readiness batch job — `schedulers/run_intraday_build_check.bat` + `scripts/intraday_build_check.py`: runs the intraday test suite + lifecycle verifier + real-data strategy smoke, prints READY / NOT READY. Run before each live step._
- _(2026-06-08) Stage 2 / Track B M2A — `scripts/collect_intraday_history.py` (Alpaca historical data client, IEX, offline only) + `tests/test_collect_intraday_history.py` (10 tests green). Collected ~9mo (2025-09-03..2026-06-08) for Tier-1+2 universe: 5m=159,650 bars, 15m=56,031 bars, all 10 symbols, no gaps. Cached to `data/intraday_history_<interval>.pkl` (pkls are regenerable caches, not committed — matches repo convention). commit 49a45a1._
- _(2026-06-08) **Stage 2 GO-GATE GREEN** — `scripts/backtest_candle_continuation.py` + `tests/test_backtest_candle_continuation.py` (8 tests green). Live-faithful model: next-bar-open fills, ATR initial stop (entry−2.5×ATR14), ratcheting ATR trail (HH−3.0×ATR14, up-only), exit on stop/long_exit/EOD (no overnight). Per-symbol + aggregate expectancy → `data/intraday_continuation_backtest.csv`. Result: 5m agg 6027 trades / 36.6% WR / PF 1.05 / +0.009% exp; 15m agg 2417 trades / 37.2% WR / PF 1.18 / +0.048% exp (15m stronger — TSLA PF 1.58, SOXL 1.21, COIN 1.28, TQQQ 1.32, AMD 1.32). Thin edge as the plan predicted — pattern is the trigger, exit discipline + timeframe carry it. commit 20b7a46._
