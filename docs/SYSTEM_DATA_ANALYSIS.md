# System Data Analysis — Profit Generation

Generated 2026-06-02 from `data/trading.db` + backtest CSVs + scraped corpus. Read-only analysis; no code/config/DB modified.

> **Scope & data-integrity caveats (read first — they change how every number below should be interpreted):**
> 1. **`outcomes` is an EOD backtest dataset, not live execution.** All 1,853 closed rows are `bar_interval='1d'`, every single exit is `exit_reason='long_exit_signal'`, and **`mfe_pct`/`mae_pct` are 100% NULL** and `pyramid_tier` is 100% NULL. This means the table is signal-to-signal P&L on daily botnet101/trend strategies spanning 2024-05 → 2026-06. **There are ZERO intraday closed outcomes and NO stop/trailing/EOD-close exit records anywhere in `outcomes`.**
> 2. **Consequence:** Section 5's core ask (MFE/MAE capture, stop-band breach, trailing tightness) **cannot be computed from `outcomes`** — the columns are empty. It is reconstructed instead from the live `paper_trades` round-trips and the backtest CSVs, and the gap itself is flagged as a top weakness.
> 3. **Live track record is tiny and young.** Real paper orders began 2026-05-18 (15 days). 196 orders, 65 closed FIFO round-trips, equity 100,000 → 100,163 (+0.16%). Live intraday P&L per-strategy has n=1–24 — directional but **not statistically conclusive**.

---

## Executive Summary

- **EOD mean-reversion (botnet101) is the real edge.** Across 1,853 closed daily trades: **65.5% win rate, +1.33% expectancy/trade, profit factor 2.22**, +2,463% summed return over 2 years of signals. Top strategies `botnet101-3-bar-low` (n=390, +2.18%/trade, PF 2.92) and `botnet101-4bar-momentum-reversal` (n=361, +2.10%, PF 3.31) carry the book.
- **Intraday/ORB is a net drag with negative backtest AND negative live evidence.** The ORB family backtest is **uniformly negative** (orbo-bidirectional −12.8%, orbo-long −12.9%, orb-pivots −4.8% vs SPY buy-and-hold **+22.6%**). Live, `intraday-1m-orb` is the single biggest loser (**−$55.91, 22% win rate, n=9**) and `intraday-1m-vwap-reclaim` is also negative (−$7.39, 39% WR, n=18). The Ross-Cameron gap-and-go study (`momentum_drift`) is **−3.16% mean, 32% WR**.
- **Two botnet101 strategies are unprofitable / marginal and should be flagged:** `botnet101-consec-bearish` (n=168, **−0.06% expectancy, PF 0.95** — loses money) and `botnet101-buy-5day-low` (n=243, +0.33%, PF 1.28, payoff 0.57 — thin).
- **Trend strategies have catastrophic closed-outcome stats but n is tiny:** `trend-donchian-breakout-20` n=26, **0% win rate, −8.0%/trade**; `trend-ma-cross-20-50` n=4, 0% WR. Too thin to damn outright but currently all-loss.
- **A $250/share price cap is vetoing the most liquid, highest-edge symbols.** `price_too_high` fired **6,338 times** (cap=$250) on SPY/QQQ/NVDA/AVGO/SMH etc. — `intraday-1m-momentum` alone lost **5,139** entry signals to it. This is the dominant *real* edge leak in the skip data.
- **Capital is massively under-deployed.** Latest snapshot: only **30.6% of equity deployed**, $69.5k cash idle, $169.7k buying power unused. Median filled position is **$914 (max single $2,546)** against a **$10,000 cap** — ~9× headroom unused. The "full-deployment" sizing fix has not taken effect in the live fills.
- **Symbol universe is concentrated in sector ETFs and that's where the edge is.** Best EOD names by expectancy (n≥20): XME +2.80%, QQQ +2.42%, XBI +2.27%, XLE +1.96%, IWM +1.62%, GDX +1.50%. Weakest profitable: XHB +0.48%, KRE +0.61%. No n≥20 symbol is net-negative — the daily universe is well-chosen.
- **The strategy "corpus" is small and largely exhausted.** Only **18 scraped records**; 14 are already implemented. The genuine gap is not quantity but *category*: no working gap-fill, pairs/stat-arb, trend-pullback continuation, or short side (donchian-retest-short exists but n=0 live/closed).

---

## 1. Overall Performance (closed `outcomes`, all daily/EOD)

| Metric | Value |
|---|---|
| Closed trades | 1,853 |
| Win rate | 65.5% (1,214 W / 519 L / 120 flat) |
| Mean return (= expectancy) | **+1.329%/trade** |
| Median return | +1.209% |
| Profit factor | **2.22** (gross +4,475% / −2,012%) |
| Avg win / avg loss | +3.69% / −3.88% |
| Payoff ratio | 0.95 (wins ≈ losses in size; edge is the high hit rate) |
| Per-trade Sharpe-ish (mean/σ) | 0.19 (σ=6.98%) |
| Best / worst trade | +93.5% / −31.4% |
| Summed return | +2,463% (2 yrs of signals, not compounded equity) |

**Return distribution:**

| Bucket | Count |
|---|---|
| < −10% | 32 |
| −10..−5% | 133 |
| −5..−2% | 123 |
| −2..0% | 351 |
| 0..2% | 563 |
| 2..5% | 476 |
| 5..10% | 120 |
| > 10% | 55 |

Right-skewed with a fat positive tail (55 trades >10%, best +93%) and a controlled left tail (32 worse than −10%). The edge is a **high win rate with near-1.0 payoff** — classic mean-reversion signature. Vulnerable to a payoff collapse if losers fatten (see stop analysis §5).

**Live equity curve** (`equity_snapshots`, 2026-05-18 → 2026-06-02, 325 snaps): start 100,000 → end **100,162.87 (+0.163%)**, max equity 100,323.94, min 99,951.29, **max drawdown −0.277%**. Essentially flat over 15 days — consistent with tiny position sizes (§7).

---

## 2. Per-Strategy Breakdown (closed `outcomes`, ranked by expectancy)

| Strategy | n | WR | Expectancy | PF | Payoff | Avg W | Avg L | Verdict |
|---|---|---|---|---|---|---|---|---|
| botnet101-3-bar-low | 390 | 72.1% | **+2.183%** | 2.92 | 1.13 | +4.61 | −4.07 | Core winner |
| botnet101-turn-of-month | 37 | 89.2% | +2.107% | **8.73** | 1.06 | +2.67 | −2.52 | Excellent, thin-ish |
| botnet101-4bar-momentum-reversal | 361 | 60.1% | +2.096% | 3.31 | 1.26 | +5.00 | −3.95 | Core winner |
| botnet101-turn-around-tuesday | 60 | 75.0% | +1.585% | 3.64 | 1.21 | +2.91 | −2.40 | Good |
| botnet101-consec-below-ema | 564 | 63.5% | +1.485% | 2.76 | 1.13 | +3.67 | −3.24 | Core winner (largest n) |
| botnet101-buy-5day-low | 243 | 69.1% | +0.334% | 1.28 | **0.57** | +2.19 | −3.82 | **Marginal** — losers 1.7× wins |
| botnet101-consec-bearish | 168 | 66.7% | **−0.056%** | **0.95** | 0.48 | +1.73 | −3.63 | **Loses money** |
| trend-ma-cross-20-50 | 4 | 0.0% | −4.870% | 0.00 | — | — | −4.87 | All-loss, n too thin |
| trend-donchian-breakout-20 | 26 | 0.0% | **−7.999%** | 0.00 | — | — | −8.00 | **All-loss, n=26** |

**Statistically thin (n<30):** turn-of-month (37 — borderline OK), trend-ma-cross (4), trend-donchian (26). Everything else has n≥60.

**EOD vs intraday split:** EOD = all 1,853 closed outcomes. **Intraday closed outcomes = 0** — intraday strategies have produced no resolved P&L in this table; their only evidence is (a) the negative ORB-family backtest CSV and (b) live paper round-trips (§ below).

**Live realized round-trips** (`paper_trades` FIFO, n=65, total **+$119.09**, PF 1.90, 49.2% WR):

| Strategy | n trips | Realized $ | WR |
|---|---|---|---|
| botnet101-3-bar-low | 3 | **+$140.95** | 100% |
| botnet101-consec-below-ema | 5 | +$33.05 | 80% |
| intraday-1m-momentum | 24 | +$11.83 | 54% |
| botnet101-4bar-momentum-reversal | 1 | +$3.73 | 100% |
| intraday-mr-3bar-low-15m | 1 | +$2.00 | 100% |
| botnet101-turn-around-tuesday | 2 | −$4.34 | 50% |
| trend-ma-cross-20-50 | 2 | −$4.83 | 0% |
| intraday-1m-vwap-reclaim | 18 | −$7.39 | 39% |
| **intraday-1m-orb** | 9 | **−$55.91** | **22%** |

Live corroborates backtest: **EOD botnet101 carries the entire live profit; intraday-ORB and vwap-reclaim bleed; trend is negative.** (23 lots still open, unrealized — see §5.)

---

## 3. By Bar Interval / Time-of-Day

**By interval (closed outcomes):** 100% are `1d`. No 1m/5m/15m trade has ever *closed* in `outcomes`, so an interval edge comparison from realized P&L is impossible. (Signal *generation* is heavily intraday: 16,884 of 23,626 signals are 1m, but none convert to closed outcomes.)

**Time-of-day — live intraday entries** (`paper_trades` filled buys, UTC; 09:30 ET = 13:30 UTC):

| Hour UTC | Filled buys | ~ET session |
|---|---|---|
| 13:00 | 52 | open / pre-open drive |
| 14:00 | 24 | first hour |
| 15:00 | 4 | mid-morning |
| 16:00 | 3 | midday |
| 17:00 | 2 | midday |
| 18:00 | 3 | afternoon |

**76 of 88 (86%) intraday entries fire in the first two hours (13:00–14:59 UTC).** This is exactly where the losing ORB strategies concentrate — the open-drive bias is real, but it's pointed at strategies with negative edge. No conclusion possible on a *profitable* intraday time window because no profitable intraday strategy has enough closed trades.

---

## 4. By Symbol (closed `outcomes`, 37 distinct symbols traded)

| Symbol | n | WR | Expectancy | PF | Summed ret |
|---|---|---|---|---|---|
| GDX | 313 | 67.1% | +1.495% | 3.53 | +468% |
| KRE | 306 | 63.4% | +0.608% | 1.51 | +186% |
| XHB | 256 | 65.6% | +0.480% | 1.29 | +123% |
| XBI | 236 | 72.5% | +2.265% | 4.05 | +535% |
| XME | 232 | 59.9% | **+2.799%** | 3.74 | **+649%** |
| IWM | 200 | 68.5% | +1.619% | 2.64 | +324% |
| XOP | 137 | 61.3% | +0.612% | 1.86 | +84% |
| XLE | 76 | 72.4% | +1.962% | 3.11 | +149% |
| QQQ | 68 | 82.4% | +2.422% | 5.27 | +165% |

Symbols with n≥20: **every one is net-positive.** Best by expectancy: XME, QQQ, XBI, XLE. Weakest-but-still-positive: XHB (+0.48%), KRE (+0.61%), XOP (+0.61%). The handful of net-negative symbols (CVX, COST, HAL, KO…) are all n=1–2 — noise, not signal.

**Universe sizing:** EOD trades concentrate in **9 liquid sector ETFs** (GDX/KRE/XHB/XBI/XME/IWM/XOP/XLE/QQQ) — appropriately matched to the mean-reversion edge (sector ETFs revert; single names gap on news). `liquidity_snapshots` tracks **538 symbols**; the daily strategies only act on ~37. The universe is **well-targeted, arguably too narrow** for the trend/breakout strategies (only SPY/QQQ/IWM in their `active_on_json`) but correctly narrow for mean-reversion. Backtest confirms the ETF picks: top botnet101 backtests are XHB consec-bearish (Sharpe 0.80, +290%), XBI 4bar-reversal (Sharpe 0.77, +440%), XHB 3-bar-low (Sharpe 0.68, +505%); worst are XLE avg-hl-range-ibs (−27%) and XLE bb-reversal-ibs (−23%) — strategies not currently live.

---

## 5. STOP / TRAILING / EXIT Effectiveness

**The requested analysis cannot be done from `outcomes`** — `mfe_pct`, `mae_pct` are 100% NULL and `exit_reason` is uniformly `long_exit_signal` (no stop/trailing/EOD rows exist). This is itself the #1 instrumentation weakness: **the system is not recording how trades actually exit or how much excursion they capture.** What can be reconstructed:

**Exit-reason distribution (`outcomes`):** 1,853/1,853 = 100% `long_exit_signal`. Either (a) the daily strategies genuinely exit only on signal (no stop ever hit in backtest), or (b) stop/trailing exits are not being written to `outcomes`. Given config defines ATR 2.5× / MR 2.0× stops and 3.0× ATR trailing, **(b) is the likely truth — the exit-reason field is not populated by the live stop/trailing engine.** Live `paper_trades` carry `entry_stops='atr_initial'` on 61 fills, confirming stops ARE attached at entry — but their *triggering* is invisible in the outcome data.

**Loser-size proxy for stop tightness:** avg loss = −3.88%, worst = −31.4%, 32 trades worse than −10%. With a 2.5× ATR stop, a −31% loss means **either the stop didn't fire or it's far looser than 2.5× ATR on high-vol names.** Conversely median loss-holding is 11 bars vs 5 for winners — losers are held ~2× longer, suggesting **stops are too loose / late**, letting losers run (consistent with the fat −10%+ tail).

**Winner give-back:** cannot quantify without MFE, but payoff ratio 0.95 (avg win +3.69% barely exceeds avg loss −3.88%) combined with a 72% win rate is the fingerprint of **trailing/exit cutting winners short relative to how far losers run** — the opposite of the desired asymmetry. If MFE were captured we could confirm; right now it's a strong inference, not a measurement.

**Live unrealized lots (23 open):** heavy intraday-ORB exposure still open (NVDA orbo+orb-pivots 10+10, TSLA 5+5, SPY/QQQ across 3 ORB variants) — the same negative-edge strategies are accumulating open risk.

**Pyramiding:** `pyramid_tier` is **100% NULL** in both `outcomes` and `paper_trades` (196 rows). **No pyramiding has occurred** despite `pyramidable` strategies existing and 11 `pyramid_not_pyramidable` skips logged. The feature is built but dormant — cannot evaluate add-on performance.

**Sensitivity sketch (data-limited):** Because losers (med 11 bars, avg −3.88%, tail to −31%) run longer and larger than the ATR-2.5× policy implies, a **tighter initial stop (e.g. 1.5–2.0× ATR) would likely truncate the −10%+ tail (165 trades)** and lift payoff. But without MFE/MAE we cannot rule out that a tighter stop also clips eventual winners — this needs the MFE/MAE columns populated before optimizing. **Flag: instrument MFE/MAE/exit_reason first; stop optimization is currently un-measurable.**

---

## 6. What We're Skipping (`intraday_skips`, 200,099 rows, 2026-05-26 → 06-02)

Most skip volume is benign noise: **187,814 (94%) are `no_open_position` on `long_exit` signals** — i.e., exit signals fired with nothing to sell. Filtering to **real entry vetoes (`signal_type='long_entry'`)**:

| Gate | Count | Edge implication |
|---|---|---|
| **price_too_high** | **6,338** | **Cap=$250/share vetoes SPY/QQQ/NVDA/AVGO/SMH** — biggest leak |
| intraday_symbol_cap | 3,843 | Per-symbol position cap hit |
| already_submitted | 677 | Dedup (benign) |
| max_open_per_strategy | 597 | Per-strategy 5-open cap |
| cool_down | 246 | Loser cool-down (working as designed) |
| pyramid_not_pyramidable | 11 | Pyramiding declined |
| ineligible | 2 | Eligibility gate |

**`price_too_high` is the dominant real veto.** `intraday-1m-momentum` lost **5,139** entries to it (vwap-reclaim 839, mr-3bar-low 145). The vetoed symbols are the *most liquid, tightest-spread* names (SPY 682, QQQ 556, SMH 536, IWM 536, NVDA 455, AVGO 487). A flat $250/share cap is a crude proxy for position-sizing and **systematically excludes the best intraday vehicles.** Replace with a notional cap (already have `max_position_usd`) so high-priced liquid names are sized down, not excluded.

Caveat: we can't prove those vetoed signals *would* have been profitable (no intraday closed outcomes), and the strategies they belong to (1m-momentum, ORB) currently show negative live edge — so **fixing the cap only helps once the underlying intraday strategies are made profitable.** Lower-priority than killing the negative-edge intraday strategies.

---

## 7. Capital Deployment / Sizing

| Signal | Value |
|---|---|
| Equity deployed (latest) | **30.6%** ($30.6k of $100.2k) |
| Idle cash | $69,535 |
| Unused buying power | $169,698 |
| Median filled position notional | **$914** |
| Mean / max filled position | $1,302 / $2,546 |
| Position cap | $10,000 (tiers 5k/7.5k/10k/10k) |

**Severe under-deployment.** Despite the recent "full-deployment" sizing commits, live fills are **~9× below the $10k cap** and 30% of capital is working. Root cause is structural: `kelly_quarter` × `fraction_of_kelly=0.25` × `max_position_fraction=0.1` compounds to tiny sizes, and intraday strategies are stuck at **grace-period quarter size** because they have `n=0` closed outcomes (eligibility gate `min_outcomes=30` never met → permanent grace-period haircut). Qty distribution skews to 1–4 shares (48 of 88 buys).

**Sizing-vs-edge mismatch:** sizing is uncorrelated with edge — the highest-expectancy strategy (3-bar-low, +2.18%) gets the same kelly_quarter treatment as the money-losing consec-bearish (−0.06%). No evidence of "sizing up losers," but there's a clear **failure to size up proven winners.** The tiered `tier_3_min_sharpe=0.3` graduation never triggers for intraday (no Sharpe computable without closed outcomes).

**Config conflict flagged:** `auto_trade.skip_intraday_signals = true` AND `auto_trade.intraday_enabled = true` are both set. These appear contradictory; worth confirming which path actually governs intraday routing.

---

## 8. Strategy Corpus Gap

**Scraped corpus = 18 records** (`records.jsonl`), not a large harvested universe. Breakdown:
- **9 botnet101 daily mean-reversion** — all implemented and live (these ARE the edge).
- **3 ORB intraday** (orbo-bidir, orbo-long, orb-pivots) — implemented, **all negative in backtest**.
- **2 discretionary** (TJR/SMC, Ross-Cameron gap-and-go) — both tagged `fail`, not systematized.
- **4 UNTESTED** (RSI2, RSI14, inside-day-breakout, bollinger-bandit) — coded (`compute_*` exists) but no closed outcomes; phase2-demo/user-supplied.

29 strategies in the `strategies` roster; **~14 have real signal flow.** So the corpus is *not* under-exploited by count — it's nearly fully implemented. The real gaps are **categorical**:

| Category | Status |
|---|---|
| Daily mean-reversion (long) | ✅ Strong edge, fully exploited |
| Opening-range breakout | ⚠️ Implemented but **negative edge** — should be cut/fixed |
| Gap-and-go momentum | ❌ Studied (`momentum_drift`), −3.16% mean, abandoned |
| **Short side** | ❌ `donchian-retest-short` exists but n=0 — long-only book in a system that could hedge |
| **Trend-pullback / continuation** | ❌ Not present (only breakout, which fails) |
| **Pairs / stat-arb / relative-value** | ❌ Not present |
| **Gap-fill (mean-reversion of overnight gap)** | ❌ Not present — natural extension of the working MR edge |
| RSI(2)/RSI(14)/inside-day/bollinger-bandit | ⚠️ Coded, untested, no live data |

The highest-ROI corpus move is **not harvesting more strategies** — it's (a) killing the negative-edge ORB/momentum intraday set, (b) extending the *proven* daily-MR edge into adjacent forms (gap-fill, RSI2 on the same ETF universe), and (c) validating the 4 untested coded strategies on the ETF universe before live capital.

---

## Diagnosed Weaknesses & Opportunities

Each item is tied to a computed stat.

1. **MFE/MAE/exit_reason are not instrumented (CRITICAL, blocks all stop optimization).** 100% NULL `mfe_pct`/`mae_pct`; 100% uniform `exit_reason='long_exit_signal'`. *We cannot measure stop tightness, trailing give-back, or capture ratio at all.* Fix instrumentation first; every stop/trailing optimization is currently un-measurable guesswork.

2. **Intraday ORB/VWAP strategies have negative edge in BOTH backtest and live and should be cut or quarantined.** ORB backtest: orbo-bidir −12.8%, orbo-long −12.9%, orb-pivots −4.8% vs SPY +22.6%. Live: intraday-1m-orb −$55.91 (22% WR), vwap-reclaim −$7.39 (39% WR). They consume open-position slots and capital with no demonstrated edge.

3. **`botnet101-consec-bearish` loses money (PF 0.95, −0.06% expectancy, n=168)** and **`botnet101-buy-5day-low` is marginal (PF 1.28, payoff 0.57, n=243).** These dilute the strong core. Candidates for deactivation or parameter rework.

4. **$250/share price cap excludes the best intraday vehicles.** 6,338 `price_too_high` vetoes (5,139 on 1m-momentum) on SPY/QQQ/NVDA/AVGO/SMH. Replace the per-share cap with the existing notional `max_position_usd` so liquid high-priced names are sized down, not excluded.

5. **Capital is ~70% idle and positions are ~9× under the cap.** 30.6% deployed, $69.5k cash, median position $914 vs $10k cap. Kelly-quarter × 0.25 × 0.1 fraction stacking plus permanent grace-period haircut for intraday (n=0 closed outcomes never clears `min_outcomes=30`) crush size. Decouple sizing from the broken eligibility gate for grace strategies, or raise the kelly fraction for proven winners.

6. **Sizing is flat across edge tiers.** +2.18%-expectancy 3-bar-low is sized identically to −0.06% consec-bearish. No edge-weighted sizing despite tiered config existing (`tier_3_min_sharpe=0.3` never fires for intraday). Opportunity: route capital to top-PF strategies (3-bar-low PF 2.92, 4bar-reversal PF 3.31, turn-of-month PF 8.73).

7. **Losers run ~2× longer than winners (median 11 vs 5 bars) with a fat −10%+ tail (165 trades, worst −31%).** Payoff ratio 0.95 means winners barely outsize losers despite 65.5% WR. Suggests stops fire late/loose — but **un-confirmable until MFE/MAE is instrumented** (see #1). Likely a tighter initial stop (1.5–2.0× ATR) truncates the tail.

8. **Pyramiding is built but 100% dormant** (`pyramid_tier` NULL on all 196 paper_trades + 2,048 outcomes; 11 `pyramid_not_pyramidable` skips). Either enable it on the proven trending winners or remove the dead code path.

9. **Trend/breakout strategies are 0% win rate where they have any data** (donchian-breakout-20: 26 trades, all losers, −8.0% avg; ma-cross: 4 trades, all losers). n is thin but the signal is uniformly bad — do not scale until the entry logic is fixed.

10. **Config contradiction:** `skip_intraday_signals=true` and `intraday_enabled=true` are both set — verify intended routing before optimizing intraday at all.

11. **Categorical strategy gaps:** no working short side (donchian-retest-short n=0), no gap-fill, no trend-pullback, no pairs. Highest-ROI is extending the *proven daily-MR edge* (gap-fill, RSI2 on the ETF universe) rather than adding more intraday breakout variants that have repeatedly failed.

### Data-too-thin honesty notes
- All "intraday strategy" verdicts rest on a 15-day live sample (n=1–24/strategy) plus backtest CSVs — directional, not conclusive. The intraday-ORB negativity is the most robust (negative in backtest *and* live).
- trend-ma-cross (n=4) and turn-of-month (n=37) are statistically thin.
- No intraday strategy has a single closed `outcomes` row, so all interval/time-of-day edge questions are answered only from entry timing and live FIFO P&L, not from a clean realized-return series.
