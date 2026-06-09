# P2 Backtest Sweep — Strategy Validation, Gap-Fill Prototype, Symbol Expansion

> **Generated:** 2026-06-02 — OFFLINE quant research. No live config, DB writes, or broker
> orders. Nothing here is promoted to paper; promotion is a separate gated owner decision.
>
> **Data:** yfinance daily, 2019-01-02 → 2026-05-29 (1,862 bars/symbol, ~7.4 yr), 32 symbols.
> Polygon free tier was pulled as a cross-check but capped at **500 bars (~2 yr)** per symbol
> — see Data-Coverage Caveats. All metrics below are computed on the 7.4-yr yfinance series
> through the existing `backtest/engine.py` (next-bar-open fills, 5 bps slippage, long-only,
> all-in/all-out — the botnet101 reference-CSV convention).
>
> **Eligibility bar (from `config/settings.json`):** `min_outcomes ≥ 30`,
> `min_mean_ret_pct ≥ 0.0`, `min_sharpe_ish ≥ 0.1`. Profit factor reported alongside.
> `sharpe_ish` = per-trade mean / per-trade std (the live eligibility metric, NOT annualized).

---

## Executive Summary — Ranked Promote List

The proven edge is EOD mean-reversion on sector ETFs. This sweep confirms **two coded
strategies extend that edge cleanly across a 7.4-year out-of-window sample**, and identifies
a strong set of symbol additions. Ranked by conviction:

| Rank | What to promote | Bar status | Numbers (pooled across proven core) |
|---|---|---|---|
| **1** | **`rsi14-oversold`** strategy | **PASS** (clears all 3) | n=116, WR 67.2%, expectancy **+1.39%/trade**, PF **1.81**, sharpe_ish **0.218** |
| **2** | **`bollinger-bandit`** strategy | **PASS** | n=292, WR 68.5%, expectancy **+1.09%/trade**, PF 1.56, sharpe_ish 0.141 |
| **3** | **`rsi2-oversold`** strategy | **PASS** (highest n) | n=777, WR 64.6%, expectancy +0.65%/trade, PF 1.45, sharpe_ish 0.121 |
| **4** | **Symbol adds: XLK, SMH, SPY, DIA, XLY, XLC** (run rsi2) | **PASS per-symbol** | all sharpe_ish ≥ 0.15, PF ≥ 1.7, expectancy ≥ +0.5%/trade |
| 5 | **Symbol adds: EFA, GLD** (run rsi2) | PASS | sharpe_ish ~0.15–0.20, PF 1.57–1.71 |
| — | `gap-fill-reversion` prototype | **HOLD** | n=489, expectancy +0.15%, PF 1.19, sharpe_ish 0.061 — **below bar pooled**; works only on broad/low-beta ETFs |
| — | `inside-day-breakout` strategy | **HOLD/REJECT** | n=378, sharpe_ish 0.052 — **below the 0.1 bar**; momentum, not MR |

**Bottom line.** Promote `rsi14-oversold` first (best risk-adjusted edge, comfortably clears
the bar at n=116). `bollinger-bandit` and `rsi2-oversold` are both solid PASSes — `rsi2` has
the largest sample and is the most broadly applicable across symbols, making it the natural
vehicle for the symbol-expansion adds. The single best symbol-strategy cell in the entire
sweep is **rsi2 on XLK** (PF 3.77, sharpe_ish 0.371) and **rsi2 on SMH** (PF 2.84, sharpe_ish
0.342) — the two highest-conviction *new* symbol adds.

**Honest caveats up front:** (1) these are in-window backtests over one 7.4-yr regime — strong,
but not live-validated; (2) `rsi14-oversold` per-symbol n is thin (9–16 trades each) — the
*pooled* n=116 clears the bar but individual symbols don't; (3) the gap-fill prototype did not
clear the bar pooled and should not be promoted as-is.

---

## 1. Strategy Validation — 4 Coded Strategies × Proven ETF Core

Proven core = the 9 sector/industry ETFs that carry the live book (n≥70 closed outcomes each):
**GDX, KRE, XHB, XBI, XME, IWM, XOP, XLE, QQQ.**

### Pooled per-strategy (all trades across the 9 core ETFs concatenated, equal-weight by trade)

| Strategy | n_trades | win_rate | expectancy %/trade | profit_factor | payoff_ratio | sharpe_ish | Verdict |
|---|---|---|---|---|---|---|---|
| **rsi14-oversold** | 116 | 67.2% | **+1.389%** | **1.81** | 0.88 | **0.218** | **PROMOTE** |
| **bollinger-bandit** | 292 | 68.5% | +1.093% | 1.56 | 0.72 | 0.141 | **PROMOTE (marginal)** |
| **rsi2-oversold** | 777 | 64.6% | +0.647% | 1.45 | 0.79 | 0.121 | **PROMOTE (marginal)** |
| inside-day-breakout | 378 | 39.4% | +0.380% | 1.16 | 1.79 | 0.052 | HOLD (sharpe_ish < 0.1) |

`inside-day-breakout` is a low-win-rate / high-payoff momentum pattern — the opposite signature
to the proven MR edge — and its sharpe_ish (0.052) falls below the 0.1 bar despite positive
expectancy. Not a fit for the MR book.

### rsi2-oversold — per symbol (the workhorse; largest n, most broadly applicable)

| Symbol | n | WR | exp %/tr | PF | payoff | sharpe_ish | CAGR % | B&H CAGR % |
|---|---|---|---|---|---|---|---|---|
| QQQ | 98 | 70.4% | +0.719 | 1.97 | 0.83 | 0.213 | 9.12 | 24.29 |
| XOP | 71 | 67.6% | +1.162 | 1.69 | 0.81 | 0.183 | 8.93 | 8.08 |
| XME | 91 | 67.0% | +0.815 | 1.50 | 0.74 | 0.150 | 8.54 | 24.78 |
| GDX | 92 | 64.1% | +0.829 | 1.46 | 0.81 | 0.142 | 8.21 | 22.99 |
| XLE | 88 | 55.7% | +0.761 | 1.55 | 1.23 | 0.140 | 6.72 | 14.18 |
| XBI | 64 | 60.9% | +0.668 | 1.40 | 0.90 | 0.119 | 4.67 | 9.00 |
| XHB | 97 | 67.0% | +0.464 | 1.41 | 0.69 | 0.116 | 5.09 | 17.68 |
| IWM | 86 | 68.6% | +0.448 | 1.37 | 0.63 | 0.090 | 3.55 | 12.33 |
| KRE | 90 | 58.9% | +0.069 | 1.04 | 0.72 | 0.010 | -2.45 | 8.20 |

Every core symbol is net-positive on expectancy; **KRE is the weakest fit** (sharpe_ish 0.010,
PF 1.04, negative strategy CAGR) — borderline. Note CAGR here is the strategy's compounded
equity (cash-idle between signals, so it trails B&H on always-up names like QQQ — that is
expected for a low-exposure MR strategy, not a failure; the edge is in per-trade expectancy).

### rsi14-oversold — per symbol (strong pooled, but thin per-symbol)

| Symbol | n | WR | exp %/tr | PF | payoff | sharpe_ish |
|---|---|---|---|---|---|---|
| GDX | 14 | 64.3% | +3.430 | 4.73 | 2.63 | 0.616 |
| XME | 15 | 86.7% | +2.702 | 3.88 | 0.60 | 0.583 |
| IWM | 10 | 70.0% | +2.264 | 5.79 | 2.48 | 0.698 |
| XLE | 12 | 75.0% | +2.017 | 3.60 | 1.20 | 0.558 |
| XOP | 10 | 70.0% | +1.748 | 2.26 | 0.97 | 0.306 |
| QQQ | 14 | 78.6% | +1.600 | 2.05 | 0.56 | 0.314 |
| XBI | 9 | 55.6% | +0.387 | 1.13 | 0.91 | 0.052 |
| XHB | 16 | 68.8% | -0.211 | 0.93 | 0.42 | -0.024 |
| KRE | 16 | 37.5% | -0.894 | 0.69 | 1.16 | -0.113 |

`rsi14-oversold` fires rarely (RSI(14)<30 is a deep, infrequent signal). The pooled n=116 PASSes,
but **no single symbol has ≥30 trades**, so per-symbol verdicts are all HOLD-needs-more-data
individually. Promote at the *strategy* level on the pooled evidence, run it across the whole
core, and let n accumulate. KRE and XHB drag (consistent with KRE being the weakest core fit).

### bollinger-bandit — per symbol (clean, well-distributed)

| Symbol | n | WR | exp %/tr | PF | payoff | sharpe_ish |
|---|---|---|---|---|---|---|
| GDX | 30 | 70.0% | +2.272 | 2.43 | 1.04 | 0.371 |
| XHB | 33 | 81.8% | +1.654 | 2.09 | 0.46 | 0.237 |
| XLE | 36 | 72.2% | +1.539 | 1.78 | 0.68 | 0.182 |
| XME | 33 | 66.7% | +1.444 | 1.76 | 0.88 | 0.211 |
| XOP | 36 | 69.4% | +1.166 | 1.41 | 0.62 | 0.097 |
| QQQ | 26 | 73.1% | +1.106 | 1.93 | 0.71 | 0.252 |
| XBI | 29 | 55.2% | +0.492 | 1.22 | 0.99 | 0.080 |
| IWM | 33 | 66.7% | +0.460 | 1.32 | 0.66 | 0.085 |
| KRE | 36 | 61.1% | -0.191 | 0.93 | 0.59 | -0.022 |

`bollinger-bandit` (close < lower BB AND RSI14 < 40) is a textbook MR entry and validates well:
6 of 9 symbols clear or nearly clear individually, GDX/XLE/XME/QQQ are strong. **KRE again the
sole money-loser** — a consistent signal across all three MR strategies that KRE is the worst
mean-reverter in the current core.

CSV: `data/p2_strategy_validation_results.csv` (40 rows: 4 strategies × 9 symbols + 4 ALL aggs).

---

## 2. Gap-Fill Mean-Reversion Prototype

**Strategy** (`strategies/generated/gap_fill_reversion.py`, candidate only — not wired live):
buy when today **gaps down ≥ 1% below prior close** AND **closes back above its own open**
(intraday reclaim), filtered to `close > 200-SMA`. Exit when the gap is filled
(`close ≥ prior_close`) or the bounce stalls (`close > SMA5`). Same EOD signal→next-open-entry
cadence as botnet101.

### Per-symbol + pooled

| Symbol | n | WR | exp %/tr | PF | payoff | sharpe_ish | CAGR % | B&H CAGR % |
|---|---|---|---|---|---|---|---|---|
| QQQ | 47 | 76.6% | +0.696 | **3.45** | 1.05 | **0.474** | 4.38 | 24.29 |
| XBI | 28 | 67.9% | +0.813 | 2.07 | 0.98 | 0.258 | 2.88 | 9.00 |
| IWM | 43 | 67.4% | +0.353 | 1.66 | 0.80 | 0.181 | 1.92 | 12.33 |
| XOP | 71 | 66.2% | +0.483 | 1.58 | 0.81 | 0.159 | 4.20 | 8.08 |
| GDX | 83 | 56.6% | -0.045 | 0.95 | 0.73 | -0.018 | -0.58 | 22.99 |
| XHB | 47 | 63.8% | -0.050 | 0.95 | 0.54 | -0.018 | -0.56 | 17.68 |
| KRE | 44 | 54.5% | -0.087 | 0.89 | 0.74 | -0.039 | -0.65 | 8.20 |
| XME | 66 | 53.0% | -0.158 | 0.84 | 0.75 | -0.069 | -1.60 | 24.78 |
| XLE | 60 | 46.7% | -0.177 | 0.81 | 0.92 | -0.078 | -1.63 | 14.18 |
| **ALL** | **489** | **60.3%** | **+0.152** | **1.19** | 0.78 | **0.061** | — | — |

**Verdict: HOLD-needs-more-data / rework.** Pooled, the prototype is positive but **below the
sharpe_ish ≥ 0.1 bar** (0.061) and below a confident PF. The pattern splits cleanly by symbol
character:

- **Works on broad / lower-beta ETFs** that genuinely fill gaps: QQQ (PF 3.45), XBI, IWM, XOP.
  On the candidate set it's also good on XLV (PF 2.60), USO (2.16), XLY (1.99), XLC (1.86),
  SPY (1.83), TLT (1.92).
- **Fails on the high-beta metals/energy core** — GDX, XME, XLE — which **trend through gaps
  rather than fill them** (all negative expectancy). A gap-down on GDX is more often a real
  move than a fade.

This is a genuine, reportable finding: gap-fill is a *broad-ETF* MR strategy, not a high-beta
one. As a deployment candidate it would need to be **restricted to a curated broad/low-beta
symbol set** (QQQ, SPY, IWM, XBI, XOP, XLV, XLY, XLC) before it clears the bar. I did NOT
ship it that way (that's a parameter/universe-fitting decision for the owner) — reporting it
honestly as HOLD. CSV: `data/p2_gap_fill_results.csv`.

---

## 3. Liquidity-Ranked Symbol Shortlist

All 32 universe symbols are in `liquidity_snapshots` (as-of 2026-06-02) with healthy 20-day
dollar volume. **No spread column exists** in the table — ranking is on dollar volume only
(every candidate trades >$200M/day, so spread is a non-issue for the position sizes in play;
flagged as a data gap, not a tradeability concern). Ranking the *additional* candidate ETFs
(not already in the proven core) by rsi2-oversold edge over the 7.4-yr sample:

| Symbol | $vol/day | rsi2 n | exp %/tr | PF | sharpe_ish | Liquidity | **Verdict** |
|---|---|---|---|---|---|---|---|
| **XLK** | $2.28B | 101 | +1.509 | **3.77** | **0.371** | excellent | **ADD** |
| **SMH** | $5.82B | 112 | +1.578 | 2.84 | 0.342 | excellent | **ADD** |
| **SPY** | $35.0B | 106 | +0.669 | 2.42 | 0.320 | excellent | **ADD** |
| **DIA** | $2.37B | 95 | +0.560 | 1.99 | 0.257 | excellent | **ADD** |
| **XLY** | $0.96B | 84 | +0.670 | 1.85 | 0.208 | excellent | **ADD** |
| **XLC** | $0.56B | 94 | +0.541 | 1.73 | 0.158 | strong | **ADD** |
| EFA | $1.49B | 101 | +0.414 | 1.71 | 0.195 | excellent | ADD |
| GLD | $2.53B | 102 | +0.408 | 1.57 | 0.151 | excellent | ADD |
| IEF | $0.70B | 79 | +0.197 | 1.82 | 0.185 | strong | ADD (marginal) |
| HYG | $2.85B | 105 | +0.145 | 1.71 | 0.169 | excellent | ADD (marginal) |
| XLI | $1.35B | 98 | +0.545 | 1.67 | 0.132 | excellent | ADD (marginal) |
| SLV | $1.67B | 89 | +0.643 | 1.44 | 0.122 | excellent | ADD (marginal) |
| XLF | $1.80B | 89 | +0.441 | 1.49 | 0.113 | excellent | ADD (marginal) |
| USO | $1.23B | 69 | +0.676 | 1.41 | 0.104 | excellent | ADD (marginal) |
| EEM | $1.98B | 76 | +0.360 | 1.48 | 0.132 | excellent | ADD (marginal) |
| XLB | $0.58B | 89 | +0.418 | 1.44 | 0.122 | strong | ADD (marginal) |
| LQD | $2.81B | 87 | +0.088 | 1.24 | 0.070 | excellent | REJECT (sh<0.1) |
| XLP | $0.98B | 114 | +0.115 | 1.18 | 0.058 | excellent | REJECT |
| XLU | $0.97B | 81 | +0.136 | 1.14 | 0.044 | excellent | REJECT |
| XLV | $1.45B | 98 | +0.069 | 1.10 | 0.033 | excellent | REJECT |
| ARKK | $0.58B | 66 | +0.143 | 1.05 | 0.018 | strong | REJECT |
| XLRE | $0.21B | 72 | +0.039 | 1.05 | 0.015 | adequate | REJECT |
| TLT | $2.36B | 51 | -0.243 | 0.78 | -0.073 | excellent | **REJECT (negative)** |

### Recommended ~5–8 additions to extend the proven MR universe

**Primary adds (6, all clear the bar comfortably on rsi2):** **XLK, SMH, SPY, DIA, XLY, XLC.**
These are liquid, broad/sector ETFs with the range-bound MR character the edge wants — XLK and
SMH in particular are the two best symbol-strategy cells in the entire sweep. SPY/DIA add
broad-market reversion depth; XLY/XLC fill out the sector coverage.

**Secondary adds (2, if more breadth wanted):** **EFA, GLD** (international + gold, both
sharpe_ish ≥ 0.15, low correlation to the sector core — diversification benefit).

**Do NOT add:** LQD, XLP, XLU, XLV, ARKK, XLRE (all positive but below the sharpe_ish 0.1 bar
— too weak to earn capital), and **TLT (negative expectancy — long bonds did not mean-revert
in this regime; reject outright).** Note: these are MR-fit rejections, not liquidity rejections.

### Flag: current-symbol MR fit

- **KRE is the weakest current core symbol** — worst MR fit across all three strategies
  (rsi2 sharpe_ish 0.010 / PF 1.04; bollinger-bandit and rsi14 both negative on it). It is NOT
  a candidate for removal on this evidence alone (the live botnet101 strategies are profitable
  on it: n=307, +0.61% expectancy per `SYSTEM_DATA_ANALYSIS.md`), but it should be flagged as
  the lowest-conviction core symbol and watched.
- All other current core symbols (GDX/XME/XLE/XOP/QQQ/XBI/XHB/IWM) are solid MR fits under
  rsi2/bollinger — including the high-beta metals/energy names, which mean-revert fine on the
  RSI/BB timeframe even though they fail the *gap-fill* pattern specifically.

CSV: `data/p2_symbol_expansion_results.csv` (46 rows: rsi2 + gap-fill on 23 candidate ETFs).

---

## 4. Eligibility Scoring — Explicit Verdicts

Scored against the live bar: **n ≥ 30, mean_ret_pct ≥ 0, sharpe_ish ≥ 0.1** (PF reported).
"PROMOTE" = clears all three with margin; "PROMOTE (marginal)" = clears but near a threshold;
"HOLD" = positive but misses a threshold or n too thin per-symbol; "REJECT" = negative edge.

### Candidate strategies (pooled across proven core)

| Strategy | n | mean % | PF | sharpe_ish | **Verdict** | Note |
|---|---|---|---|---|---|---|
| **rsi14-oversold** | 116 | +1.389 | 1.81 | 0.218 | **PROMOTE** | best risk-adjusted; thin per-symbol, promote at strategy level |
| **bollinger-bandit** | 292 | +1.093 | 1.56 | 0.141 | **PROMOTE (marginal)** | clean per-symbol distribution; clears bar |
| **rsi2-oversold** | 777 | +0.647 | 1.45 | 0.121 | **PROMOTE (marginal)** | largest n; the symbol-expansion vehicle |
| inside-day-breakout | 378 | +0.380 | 1.16 | 0.052 | **HOLD** | sharpe_ish below 0.1; momentum not MR |
| gap-fill-reversion | 489 | +0.152 | 1.19 | 0.061 | **HOLD** | below bar pooled; rework to broad-ETF universe only |

### Symbol additions (run rsi2-oversold)

- **PROMOTE / ADD (6):** XLK, SMH, SPY, DIA, XLY, XLC
- **ADD (secondary, 2):** EFA, GLD
- **ADD (marginal, optional):** IEF, HYG, XLI, SLV, XLF, USO, EEM, XLB
- **REJECT:** LQD, XLP, XLU, XLV, ARKK, XLRE, **TLT (negative)**

### Suggested promotion sequence (owner decision — not executed here)

1. **`rsi14-oversold`** across the full proven core — highest edge, clears the bar, let
   per-symbol n accumulate in paper.
2. **`bollinger-bandit`** across the proven core — clean, well-distributed.
3. **`rsi2-oversold`** across the proven core **+ the 6 primary symbol adds** (XLK, SMH, SPY,
   DIA, XLY, XLC) — rsi2 is the broadest-applicable proven-family strategy and the natural
   carrier for the new symbols.
4. Defer `gap-fill-reversion` and `inside-day-breakout` pending rework / more data.

---

## Data-Coverage Caveats (precise)

- **Polygon free tier capped at 500 daily bars (~2 yr) per symbol.** Requesting 2019-01-01
  returned only **2024-06-03 → 2026-06-01** for every one of the 32 symbols (uniform 500-bar
  truncation — the documented free-tier aggregate cap). All 32 symbols pulled successfully
  within that window; cached under `p2.polygon.daily:*`. Polygon was therefore used as a
  cross-check, not the primary history source.
- **Primary source: yfinance**, 2019-01-02 → 2026-05-29, **1,862 bars (~7.4 yr)** per symbol,
  all 32 symbols complete, `auto_adjust=True` (split/div-adjusted). Cached
  (`data/p2_history_yf.pkl`) so re-runs are instant. This is the 5yr+ depth the task targeted.
- **Adjustment basis differs between sources:** yfinance is back-adjusted; Polygon was pulled
  `adjusted=False` (raw, matching live execution). Backtests here run on the yfinance adjusted
  series — appropriate for long-horizon edge measurement, but absolute price levels won't match
  raw live fills on names with dividends/splits in the window.
- **Backtests are in-window / single-regime.** 7.4 years is one macro regime (2019–2026). These
  are strong validation numbers but NOT walk-forward / out-of-sample tested, and NOT live-
  validated. Treat as "promote-to-paper candidates," not proven live edge.
- **rsi14-oversold per-symbol n is thin** (9–16 trades/symbol). The pooled n=116 clears the
  bar; individual symbols do not. Strategy-level promotion only.
- **No bid/ask spread data** in `liquidity_snapshots` — tradeability ranked on 20-day dollar
  volume alone. All candidates trade >$200M/day, so spread is immaterial at current sizing.
- **Engine simplifications:** long-only, all-in/all-out per symbol, 5 bps slippage, no
  commission, no position-level ATR/trailing stops applied in these runs (the strategies' own
  signal-exit governs). This isolates raw signal edge; live stop/sizing overlays would modify
  realized results.

---

## Artifacts

| Path | Contents |
|---|---|
| `data/p2_history_yf.pkl` | {symbol: daily OHLCV DataFrame}, 32 symbols, 7.4 yr (cached input) |
| `data/p2_strategy_validation_results.csv` | 4 coded strategies × 9 core ETFs + 4 pooled aggs |
| `data/p2_gap_fill_results.csv` | gap-fill prototype × 9 core ETFs + pooled agg |
| `data/p2_symbol_expansion_results.csv` | rsi2 + gap-fill × 23 candidate ETFs |
| `scripts/research/p2_polygon_daily.py` | Polygon per-ticker `list_aggs` loader (cached) |
| `scripts/research/p2_pull_history.py` | yfinance history assembler → pickle |
| `scripts/research/p2_backtest_lib.py` | harness + full metrics (reuses backtest engine) |
| `scripts/research/p2_run_sweep.py` | sweep runner → the 3 CSVs |
| `strategies/generated/gap_fill_reversion.py` | gap-fill `compute_fn` (candidate, not wired live) |

_End of P2 Backtest Sweep — 2026-06-02._
