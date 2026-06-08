# Intraday Long-Only Momentum Continuation: Research Findings
**Compiled:** 2026-06-08  
**Strategy profile:** Candlestick-triggered long entries on high-beta liquid US equities + 3x ETFs, ATR stop, trailing stop exit, long-only.  
**Data pulled from:** thepatternsite.com (Bulkowski), tradethatswing.com (Edgeful ORB), quantpedia.com, tradesviz.com, quantifiedstrategies.com, Yahoo Finance (live 3-month window for universe screening).

---

## Section 1: Candlestick Pattern Reliability Statistics

### Source: Thomas Bulkowski, "Encyclopedia of Candlestick Charts" (direct scrape of thepatternsite.com, 2026-06-08)

Bulkowski tested ~5 million candle lines across hundreds of stocks. His "reversal rate" = percentage of patterns that break out in the predicted direction. His "overall performance rank" = how far price travels after breakout (rank 1 = best out of 103 patterns). **These are daily-scale statistics.** Intraday data is noted separately below.

#### Bullish Reversal / Continuation Patterns (Long Entry Candidates)

| Pattern | Bulkowski Reversal Rate | Overall Perf Rank (/103) | Freq Rank | Key Insight |
|---|---|---|---|---|
| **Morning Star** | 78% | 12 | 66 | Best bullish reversal stats on the list; rare but reliable; upward breakouts from uptrend retraces are the premium setup |
| **Three White Soldiers** | 82% | 32 | 67 | Highest reversal rate of bullish patterns; BUT upward-breakout post-move is weak; value is in continuation confirmation, not target |
| **Piercing Pattern** | 64% | 13 | 40 | Strong overall performance + good frequency; best in bear market; avoid if primary trend is down |
| **Bullish Engulfing** | 63% | 84 | 12 | Very common; reversal rate decent but post-breakout trend is short-lived (rank 84); best as pullback-in-uptrend signal, not reversal |
| **Hammer** | 60% | 65 | 36 | Reversal rate barely above coin-flip; mid-range performance; white body hammer outperforms black body; best at yearly lows |

**Bulkowski's key insight on bullish engulfing:** "The best chance of success is trading bullish engulfing when the primary trend is upward and you see a downward retrace." This aligns exactly with pullback-to-EMA continuation entries.

**Bulkowski on morning star:** "Look for the morning star to appear in a downward retrace of the primary uptrend. When an upward breakout occurs, price joins with the rising price trend already in existence and away the stock goes."

#### Bearish Reversal Patterns (Long Exit Triggers)

| Pattern | Bulkowski Reversal Rate | Overall Perf Rank (/103) | Key Insight |
|---|---|---|---|
| **Bearish Engulfing** | 79% | 91 | Very reliable reversal but post-move is short-lived (rank 91); good exit trigger, not worth shorting against |
| **Evening Star** | 72% | 4 | High reversal rate + excellent post-move performance (rank 4); the strongest bearish signal on the list |
| **Shooting Star (1-line)** | 59% | 55 | Near-random 59%; Bulkowski calls it "near random performance" explicitly; do NOT treat as reliable exit alone |

#### TradesViz 2025 Quantitative Backtest (ES Futures + AAPL, 15-minute bars, May–Oct 2025)

Source: tradesviz.com blog (verified 2026-06-08). These are intraday backtests — more directly applicable than Bulkowski's daily stats.

| Pattern | Win Rate | Profit Factor | Sharpe | Notes |
|---|---|---|---|---|
| Bearish Engulfing (traded LONG) | 75.76% | 2.73 | 1.98 | Counter-intuitive: "bearish" pattern works as bullish signal intraday on ES |
| Hammer | 71.79% | 1.94 | 2.17 | Solid; 78 trades; context matters |
| Three White Soldiers + RSI<35 filter | 83.33% | 2.68 | 2.50 | Best performer; RSI filter was the key lift |
| Morning Star + SMA + ATR filter | 51.85% | 0.79 | negative | LOSING strategy without better filters; proves filters are essential |
| Doji (basic) | 65.98% | 1.10 | barely positive | Too many signals, too weak a filter |
| Bearish Engulfing, conservative (RSI<40 + 1.5x vol) | 61.90% | 2.97 | 2.14 | Fewer trades, higher quality |

**Critical finding from TradesViz:** "Most of what you've been taught about candlestick patterns is wrong. Context beats patterns. A mediocre pattern with good filters beats a perfect pattern in isolation every time."

#### Synthesis for Strategy

**Patterns that have measurable intraday edge (in context):**
1. Three White Soldiers with RSI oversold filter — 83% win rate intraday
2. Hammer at support/EMA in uptrend — 71% intraday, 60% daily (Bulkowski)
3. Bearish Engulfing on pullback (used as long entry trigger) — 75% intraday counter-trend
4. Piercing Pattern in uptrend retrace — 64% daily, strong post-move (rank 13)
5. Morning Star in uptrend retrace — 78% daily, strong post-move (rank 12)

**Patterns that are near coin-flips (avoid or filter heavily):**
- Shooting Star alone: 59% — explicitly called "near random" by Bulkowski
- Bullish Engulfing in a downtrend: poor (trend context kills it)
- Any pattern without trend/volume confirmation: degrade toward 50-55%

**What this means for the strategy:** Do NOT enter on pattern alone. The pattern is the final trigger, not the primary signal. The edge comes from patterns appearing within an established uptrend during a pullback to a dynamic level (EMA9/EMA20/VWAP). Filters that demonstrably lift edge: RSI oversold at pattern, above 200-period MA, above VWAP, volume spike on pattern candle (>1.5x average).

---

## Section 2: Confirmation Factors That Lift Pattern Edge

### Volume Confirmation

- **Bulkowski's universal finding:** Tall candle bodies + above-average volume at pattern formation consistently rank higher in his studies. Specifically cited for Three White Soldiers (page 798-799) and Morning Star.
- **TradesViz data:** The conservative bearish-engulfing-as-bullish with volume >1.5x SMA(20) produced Profit Factor 2.97 vs 1.51 for looser volume filter. The volume filter removed noise trades significantly.
- **Rule of thumb (widely cited, unverified empirical number):** Volume >150% of 20-period average at pattern candle lifts win rate by ~5-10 percentage points across most bullish reversal patterns.
- **UNVERIFIED:** Specific percentage lift numbers from academic papers for intraday volume confirmation on individual patterns are not publicly available in searchable form. The above is practitioner consensus, not cited research.

### Trend Filters (EMA Stacks)

- **EMA9/EMA20 alignment:** QuantifiedStrategies.com backtest showed 9-EMA crossover alone is a losing strategy on US equities (ugly equity curve). Adding a 200-MA trend filter improved it but it remained marginal for stocks. **Conclusion: simple EMA crossover alone does not work for stocks. EMA must be used as a context filter (price above EMA stack = long-only zone), not a mechanical entry signal.**
- **EMA9 as pullback target:** The practitioner standard (widely described but no single peer-reviewed backtest found) is: in a strong uptrend, price pulling back to the EMA9 and forming a bullish candle has meaningfully higher continuation probability than a random entry. Multiple TradingView community studies suggest 60-70% continuation within the next 2-3 bars when the pullback touches EMA9/EMA20 without closing below, with volume tailing off.
- **EMA20 in uptrend:** Strike.money and tradingsim.com document that bullish engulfing at EMA20 in an uptrend raises win rate from ~55% standalone to 63-70% with confirmation.

### VWAP as Confirmation

- **VWAP as filter:** TradesViz backtest explicitly implemented VWAP (approximated as 20-period VWAP) as a filter: "[close > (sma(close * volume, 20) / sma(volume, 20))]" — price above VWAP is required for bullish entries. This is one of the most consistent intraday edge factors documented across practitioner literature.
- **VWAP pullback entries:** Pulling back to VWAP and reclaiming it (close above after touching) is one of the highest-frequency documented setups. Specific win rate numbers from published backtests: Edgeful platform data (referenced in tradethatswing.com) shows VWAP-reclaim patterns on NQ/ES have ~65-75% hit rates in uptrending sessions. **Note: these numbers are from vendor platforms and may be optimism-biased; treat as directional, not exact.**
- **VWAP as exit:** Price losing VWAP intraday after a long position is a strong exit signal, independent of candlestick pattern.

### Support/Resistance Alignment

- Bulkowski's explicit tip across multiple patterns: "Candles that appear within a third of the yearly low perform best." The corollary for intraday is: patterns at a known level (prior day high, round number, prior day close) outperform patterns in open air.
- **Rule:** Pattern + level alignment is more important than pattern type. A weak pattern at a strong level > strong pattern in open air.

### Time of Day

See Section 4 for detailed time-of-day analysis.

**What this means for the strategy:** The entry signal chain should be: (1) EMA stack bullish (9>20>50 or at minimum price above EMA20), (2) VWAP alignment (price above VWAP or pulling back to it), (3) pattern at a level (EMA9/EMA20/VWAP/round number/prior level), (4) pattern candle with volume >1.0x average (1.5x is ideal), (5) bullish candle trigger. All five conditions are not always present — set a minimum of 3 of 5.

---

## Section 3: Intraday Continuation Strategies

### Bull Flag

**What it is:** Strong impulsive move up (flagpole), followed by tight consolidation with parallel declining channels or horizontal base (flag), then breakout continuation.

**Entry rules (practitioner standard, widely documented):**
- Flagpole: minimum 2-3% move in 1-5 candles (on 5-min chart)
- Flag: consolidation should not retrace more than 30-50% of flagpole
- Entry: break above flag high or first candle closing above upper flag channel
- Stop: below flag low (or below flag midpoint for tighter stop)
- Target: flagpole height added to flag base (measured move)

**Published win rates:** No single peer-reviewed quantitative study found on bull flags specifically. Practitioner consensus from multiple sources (tradingsim.com, tradethatsewing, community backtests): win rates cited in 55-65% range for proper setups (flagpole > 2%, consolidation < 30% retrace, volume declining in flag). TradesViz notes that most candlestick-based patterns only fire 5-15% of the time, making bull flags relatively infrequent.

**For our universe:** NVDA, TSLA, SOXL, AMD form recognizable bull flags multiple times per week during trending sessions.

### Pullback-to-VWAP

**What it is:** Strong opening move establishes price well above VWAP; pullback to VWAP during mid-morning; bounce/reclaim entry.

**Entry rules:**
- Morning drive establishes 1%+ move above VWAP
- Price pulls back to within ~0.1-0.3% of VWAP on declining volume
- Bullish candle forms at VWAP touch (hammer, engulfing, inside bar breakout)
- Entry on close of trigger candle or break above its high
- Stop: below VWAP (tight) or 0.5 ATR below entry
- Target: prior day high, session high, or 1:2 R:R minimum

**Evidence:** Edgeful platform data (referenced in tradethatswing.com, Oct 2025): "How often the market finishes higher based on the opening hour" — in uptrending sessions, VWAP reclaim after a pullback has 65-75% continuation rate. **Caveat: Edgeful is a vendor platform; these stats are directionally credible but may be optimized for presentation.** No independent academic replication found.

### Opening Range Breakout (ORB)

**Source:** tradethatswing.com, Cory Mitchell CMT, Edgeful platform, March 2026 article (direct scrape verified 2026-06-08).

**Strategy rules (verified from article):**
- Opening range = first 15 minutes of RTH (9:30-9:45 AM ET)
- Entry signal: 5-minute candle closes above OR high (long) or below OR low (short)
- Stop: opposite side of opening range (capped at 50 NQ points / 1% equivalent)
- Target: 50% of opening range width (appears counter-intuitive but high win rate compensates)
- 1 trade per day maximum; long-only in uptrend

**Quantified backtest results (NQ E-mini futures, 1 year ending Oct 2025):**
- Long-only, opening range cap 0.8% of price
- 114 trades
- Win rate: 74.56%
- Profit factor: 2.512
- Max drawdown: $2,725 (~12% of $10k account if scaled to position)
- Average winning trade: ~$846; average losing trade: ~$987 (near 1:1 R:R with high win rate)
- 3 instances of 2 consecutive losses; 0 instances of 3+ consecutive losses
- Annualized return on $10k account: 433% (leveraged futures; ETF equivalent dramatically lower)

**IMPORTANT CAVEATS:**
1. These results are for NQ futures, not individual stocks. Different instruments have different ORB personalities.
2. The backtest covers a period (2024-2025) that was predominantly uptrending. Author explicitly notes downtrend performance was poor and needs separate research.
3. No commission/slippage adjustment except noting it "didn't change results much" given 1 trade/day frequency.
4. Forward-testing required — settings change over time.

**ORB for our universe:** The strategy was tested on NQ but the principle applies to any liquid instrument. NVDA, TSLA, AMD, COIN all have well-defined opening ranges. Recommended adaptation: cap OR at 0.5-1.0% for individual high-beta stocks; adjust target to 1:1 R:R given higher individual stock volatility.

### EMA9/EMA20 Trend-Follow (Pullback Entry)

**What it is:** In a strong intraday uptrend (EMA9 > EMA20, both rising), enter on pullbacks that touch EMA9 and form a bullish candle.

**Published data from QuantifiedStrategies.com (2025):**
- Simple 9-EMA crossover on SPY/stocks: losing strategy (explicitly documented)
- 9-EMA + 200-MA filter: improved but still marginal for daily stocks
- 9-EMA works for trending assets (Bitcoin showed positive equity curve with 284 trades, avg gain 2.65%)
- Conclusion: EMA9 as standalone crossover entry is not reliable for US equities. As a pullback level within an established intraday trend (price above EMA20, EMA20 rising, price touches EMA9 and reverses), it functions as a level trigger, not an entry signal by itself.

**Practitioner consensus (multiple sources, no single authoritative backtest):** EMA9 pullback in uptrend + bullish candle confirmation cited at 60-65% win rate across multiple community backtests on momentum stocks. No independent peer-reviewed source confirmed.

**Entry/exit rules for EMA9/EMA20 system:**
- Filter: price > EMA20 > EMA50 (or at minimum price > EMA20, both rising)
- Entry: price pulls back to EMA9, forms hammer/engulfing/inside-bar-break on 5-minute chart
- Volume: pullback volume declining; entry candle volume expanding or at average
- Stop: below EMA20 (wider) or below pattern low (tighter, preferred)
- Exit: trailing stop at EMA9 (aggressive) or EMA20 (patient), or bearish reversal pattern
- Time filter: avoid entries after 2:00 PM ET unless power hour momentum confirms

**What this means for the strategy:** The three highest-confidence intraday setups in order are:
1. **ORB long** (9:30-9:45 AM, above-OR close on 5-minute): highest quantified evidence (74.56% win rate, PF 2.5)
2. **VWAP pullback long** (10:00 AM - 12:00 PM, established trend + VWAP touch + bullish candle): strong practitioner evidence, ~65-75% on aligned days
3. **EMA9 pullback + candlestick trigger** (10:00 AM - 2:00 PM, uptrend confirmed): moderate evidence, ~60-65% when conditions aligned

Bearish reversal patterns (evening star 72%, bearish engulfing 79%) serve as **exit triggers**, not short entries, consistent with long-only mandate.

---

## Section 4: Intraday Seasonality / Time-of-Day Effects

### Source: Quantpedia.com "Lunch Effect in U.S. Stock Market Indices" (August 2024, direct scrape verified 2026-06-08)

Study period: May 2010 - May 2024, SPY hourly data. Independent quantitative research from Cyril Dujava, Quant Analyst at Quantpedia.

#### Quantified Hourly Return Pattern (SPY, 2010-2024)

| Time Window | Pattern | Quality of Evidence |
|---|---|---|
| 9:30 - 11:00 AM | Modest positive gains; opening drive momentum | Documented, quantified |
| 11:00 AM - 12:00 PM | Negative drift / pullback | Documented, quantified |
| 12:00 PM - 1:00 PM | Positive reversal (lunch bounce) | Documented, quantified ("Lunch Effect") |
| 1:00 PM - 2:00 PM | Continuation of lunch recovery | Documented, quantified |
| 2:00 PM - 4:00 PM | Churning / sideways ("Power Hour" is inconsistent) | Mixed evidence |

**Quantpedia's specific finding on Power Hour:** "It is often churning and moving sideways, not having a distinctly trending move up until close, including the last hour." This contradicts the popular "power hour" narrative. Power Hour gains are driven by index rebalancing and options flows, not consistent directional momentum.

**Quantpedia's "Lunch Effect" strategy:** Short 11 AM - 12 PM, long 12 PM - 2 PM produced a viable equity curve but with lower Sharpe than expected. The after-lunch long-only version (12-2 PM) is the more actionable takeaway.

**Overnight anomaly context:** Academic research (Cooper, Cliff, Gulen; Branch and Ma) finds that the majority of US equity premium is earned overnight (close-to-open), not intraday. The intraday session has historically been net neutral to negative for the index. **Implication for our strategy:** We are fighting the intraday drag; therefore filtering to the strongest trend periods (opening drive + lunch recovery) is essential to overcome this headwind.

#### Recommended Trading Windows for Continuation Strategy

| Window | Rationale | Strategy Fit |
|---|---|---|
| **9:30 - 10:30 AM** | Opening drive; highest volatility; ORB breakouts; trend establishment | ORB longs; first pullback entries |
| **10:30 AM - 11:00 AM** | Transition; first pullback to VWAP/EMA9; continuation entries | Bull flag entries; EMA9 pullbacks |
| **11:00 AM - 12:00 PM** | Lull / negative drift; avoid new long entries; tighten stops | Stop management only; no new entries |
| **12:00 PM - 2:00 PM** | Lunch recovery; strongest secondary opportunity | VWAP reclaim entries; second-leg continuation |
| **2:00 PM - 3:30 PM** | Choppy; position-dependent; wait for power hour only if strong trend day | Reduce size; tighten stops |
| **3:30 PM - 4:00 PM** | Can see final-hour momentum on strong trend days | Hold if trailing stop not hit; no new entries |

**Practical filter rule:** Do not initiate new long entries between 11:00 AM and 12:00 PM ET. This is the highest-probability losing window for intraday momentum longs. This rule alone likely improves the strategy Sharpe ratio materially.

**Monday / Friday effects:** Quantpedia notes day-of-week effects are real (Edgeful data confirms certain days perform better for ORB) but no universal rule applies. Recommend backtesting day-of-week filters after initial strategy deployment.

**Earnings / macro event days:** Opening range is dramatically wider on these days; ORB cap (0.8%) filters most of them out naturally. For pattern-based entries, avoid within 1 session of major earnings on the individual stock.

**What this means for the strategy:**
- Primary active window: 9:30-11:00 AM + 12:00-2:00 PM
- Dead zone (avoid new longs): 11:00 AM - 12:00 PM
- No new entries after 2:30 PM unless in a clearly defined power hour trend
- ATR-based trailing stop handles exit timing; time-of-day filter handles entry timing

---

## Section 5: Universe Screening Data

### Data Source and Methodology

Live data pulled from Yahoo Finance (yfinance API), 3-month window ending 2026-06-07. Metrics calculated:
- **avg_daily_dollar_volume_M:** Average daily dollar volume in millions (shares * close price, averaged over 3 months)
- **atr_pct:** 14-period Average True Range as % of closing price (rolling 14-day avg, last observation)
- **avg_daily_range_pct:** Average (High-Low)/Close as % over 3-month window

**Note:** ATR% and range% are daily-scale. Intraday ATR on a 5-minute bar is roughly 1/6 of daily ATR as a starting estimate (sqrt(78 bars in 6.5-hour session) scaling), but actual intraday values vary significantly by time of day.

### Ranked Universe Table

| Symbol | Avg Daily $ Vol ($M) | ATR 14% | Avg Daily Range% | Intraday Rank | Notes |
|---|---|---|---|---|---|
| SOXL | $9,056M | 15.70% | 9.43% | 1 | 3x semiconductor; extreme range; deep stop required; best on strong trend days |
| COIN | $1,966M | 5.80% | 5.62% | 2 | Crypto-linked; high beta; excellent range; lower $ volume limits size |
| AMD | $12,165M | 6.61% | 4.82% | 3 | High liquidity + high range; top continuation candidate |
| PLTR | $6,563M | 5.07% | 4.35% | 4 | High beta AI name; consistent range; strong trend character 2025-2026 |
| TSLA | $23,526M | 3.62% | 3.52% | 5 | Massive liquidity; news-sensitive; primary long continuation target |
| NVDA | $33,074M | 3.97% | 3.03% | 6 | Deepest dollar volume on list (tied with QQQ); premier momentum name |
| META | $10,346M | 3.08% | 2.61% | 7 | High liquidity; cleaner intraday trends than NVDA; less news noise |
| TQQQ | $5,381M | 4.96% | 4.14% | 8 | 3x QQQ; good liquidity + range; leveraged tech exposure |
| SMH | $4,730M | 4.21% | 2.78% | 9 | Semiconductor ETF; less decay risk than SOXL; good range |
| AMZN | $10,833M | 2.82% | 2.42% | 10 | High liquidity; cleaner trends; lower vol than TSLA/AMD |
| AAPL | $12,358M | 1.86% | 1.97% | 11 | Massive liquidity but low range; marginal for pattern entries |
| SPXL | $688M | 2.87% | 2.98% | 12 | 3x S&P; low $ volume vs peers; TQQQ preferred |
| IWM | $9,511M | 1.89% | 1.66% | 13 | Low range; better as macro filter |
| XLK | $2,191M | 2.74% | 1.96% | 14 | Low range; better as trend filter |
| QQQ | $33,088M | 1.63% | 1.41% | 15 | Deepest liquidity but range too small for pattern entries; use as trend filter |
| SPY | $46,908M | 0.96% | 1.00% | 16 | Deepest liquidity on any US market; use only as macro trend filter |

### Practical Tier System

**Tier 1 — Core Momentum Targets** (high range + high liquidity, primary watchlist):
- TSLA, NVDA, AMD, COIN, PLTR

**Tier 2 — Secondary Momentum** (solid but slightly lower range or liquidity):
- META, TQQQ, SOXL (caution on position size due to volatility decay), SMH, AMZN

**Tier 3 — Leverage ETFs for Strong Trend Days Only**:
- SOXL (semiconductor trend confirmed), TQQQ (Nasdaq trend confirmed), SPXL (S&P trend confirmed)

**Trend Filters (not trade vehicles)**:
- QQQ, SPY, IWM, XLK — use these to confirm macro trend before entering individual names

### SOXL Specific Note

SOXL's 15.7% ATR is exceptional range but introduces two complications for this strategy:
1. ATR stops will be very wide (e.g., 1x daily ATR = $40 on a $262 price = 15%), requiring smaller position sizes to control dollar risk
2. Leveraged ETF decay means multi-day holding is structurally disadvantaged — this must be a same-day exit or tight next-morning exit
3. Yahoo Finance data shows SOXL avg volume of ~76M shares/day x ~$262 price = ~$20B daily dollar volume — the $9B figure above is from the 3-month window which includes the post-crash recovery; actual current dollar volume is substantially higher

**What this means for the strategy:** The primary intraday long-only targets in order of suitability are:
1. **AMD** — best balance of range (4.82%), liquidity ($12B), and trend consistency
2. **TSLA** — massive liquidity + solid range; must filter for news events
3. **PLTR** — strong AI momentum character; 4.35% range; manageable size
4. **NVDA** — premier name; slightly lower range than AMD but unmatched dollar volume
5. **COIN** — highest range (5.62%) but lowest $ volume; cap position size proportionally

---

## Cross-Section Synthesis: What This Means for Building the Strategy

### Signal Architecture (Priority Order)

1. **Macro trend filter** (daily/60-min): SPY or QQQ above EMA20 = long-only mode active
2. **Symbol trend filter** (15-min): Individual stock above EMA20, EMA9 > EMA20
3. **Time filter**: Active window only (9:30-11:00 AM or 12:00-2:00 PM ET; NOT 11:00 AM - 12:00 PM)
4. **Level alignment**: Pattern must form at EMA9, EMA20, VWAP, prior day high, or round number
5. **Pattern trigger** (5-min bar): Hammer, bullish engulfing, morning star, three white soldiers, piercing — in that order of intraday applicability
6. **Volume confirmation**: Pattern bar volume > 1.0x average (1.5x is high-confidence)
7. **Entry**: Close of pattern candle or break above pattern high +1 tick
8. **Stop**: 1x ATR (14-period, 5-min) below pattern low (or below the level that triggered)
9. **Trail**: Ratchet stop up to EMA9 once in profit by 1x ATR; exit on close below EMA9 or bearish engulfing/evening star signal
10. **Hard time exit**: Flatten all longs by 3:50 PM ET (already system policy)

### Pattern Priority for Long Entries (Intraday)

1. **Three White Soldiers + RSI oversold** — 83% intraday win rate (TradesViz, ES futures); use at first pullback of the day
2. **Hammer at EMA/VWAP** — 71% intraday (TradesViz); 60% daily (Bulkowski); high frequency
3. **Bullish Engulfing at pullback level** — 63-75% depending on context; most common signal
4. **Morning Star (3-bar) at support** — 78% daily (Bulkowski); rarer intraday; high-confidence when it appears
5. **ORB breakout** — 74.56% win rate (Edgeful/NQ backtest); not a candlestick pattern but highest quantified evidence

### Exit Pattern Priority

1. **Bearish Engulfing above entry** — 79% reversal rate (Bulkowski); exit immediately
2. **Evening Star** — 72% reversal (Bulkowski); rank 4 overall = strong ensuing trend; exit immediately
3. **Trailing stop hit** — mechanical; takes precedence over pattern waiting
4. **Shooting Star** — 59% (near-random); do NOT exit on shooting star alone; wait for confirmation candle

### Gaps in the Research

- **Intraday-specific Bulkowski stats do not exist** — all his data is daily. The TradesViz backtest on 15-min ES is the closest intraday proxy found.
- **Bull flag win rates** — widely cited in practitioner community (55-65%) but no independent peer-reviewed quantitative study was found. Treat as directional.
- **VWAP pullback win rates** (65-75%) come from Edgeful, a vendor platform. Independent replication not found.
- **EMA9 pullback win rates** (60-65%) come from community backtests and practitioner sources, not academic papers.
- **Dark cloud cover stats** not scraped in this session — Bulkowski page exists at thepatternsite.com/DarkCloudCover.html if needed.
- **Intraday time-of-day data for individual stocks** (NVDA, TSLA etc.) not found — Quantpedia data is SPY/index level. Individual stocks will vary; TSLA in particular is highly news-sensitive and may deviate from index patterns around its own catalysts.

---

## Quick Reference: Confirmed Numbers

| Source | Claim | Verified |
|---|---|---|
| Bulkowski | Bullish Engulfing reversal rate: 63% | Direct scrape, thepatternsite.com |
| Bulkowski | Hammer reversal rate: 60% | Direct scrape |
| Bulkowski | Morning Star reversal rate: 78%, overall rank 12/103 | Direct scrape |
| Bulkowski | Three White Soldiers reversal rate: 82% | Direct scrape |
| Bulkowski | Piercing Pattern reversal rate: 64%, overall rank 13/103 | Direct scrape |
| Bulkowski | Bearish Engulfing reversal rate: 79% (but short post-move, rank 91) | Direct scrape |
| Bulkowski | Evening Star reversal rate: 72%, overall rank 4/103 | Direct scrape |
| Bulkowski | Shooting Star reversal rate: 59% (explicitly "near random") | Direct scrape |
| TradesViz | Three White Soldiers + RSI<35, 15-min ES: 83.33% WR, PF 2.68 | Direct scrape, 2025 backtest |
| TradesViz | Hammer, 15-min ES: 71.79% WR, PF 1.94, Sharpe 2.17 | Direct scrape |
| TradesViz | Morning Star + filters, AAPL: 51.85% WR, PF 0.79 (LOSING) | Direct scrape; proof filters matter |
| Edgeful/TTS | ORB long-only NQ, 1 year 2025: 74.56% WR, PF 2.512, 114 trades | Direct scrape of March 2026 article |
| Quantpedia | Lunch lull: negative drift 11 AM - 12 PM SPY, 2010-2024 | Direct scrape, quantified study |
| Quantpedia | Post-lunch recovery: positive 12 PM - 2 PM SPY | Direct scrape |
| Quantpedia | Power Hour: "churning and sideways, not distinctly trending" | Direct scrape; contradicts popular belief |
| Yahoo Finance | NVDA 3-month ADV: $33,074M, ATR14% 3.97%, range% 3.03% | Live API pull, 2026-06-07 |
| Yahoo Finance | TSLA 3-month ADV: $23,526M, ATR14% 3.62%, range% 3.52% | Live API pull |
| Yahoo Finance | SOXL 3-month ADV: $9,056M, ATR14% 15.70%, range% 9.43% | Live API pull |
| Yahoo Finance | AMD 3-month ADV: $12,165M, ATR14% 6.61%, range% 4.82% | Live API pull |
| QuantifiedStrategies | 9-EMA standalone crossover: losing strategy on US stocks | Direct scrape, explicit finding |
