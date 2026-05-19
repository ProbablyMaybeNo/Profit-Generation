# Dashboard — Section-by-Section Guide

What every card on the dashboard shows, what the numbers mean, and how to use them when trading. No fluff.

---

## 0. The two pages

| Page | URL | Purpose |
|---|---|---|
| **Monitor** | `/` | Live paper-trading view. Everything happening *today*. Open during market hours. |
| **Research** | `/research` | Historical edge analytics. Updated by EOD pipeline. Open when reviewing strategy performance, not when watching the market. |

Switch via the nav tabs at the top of either page.

---

## 1. Monitor — global top strip

### Kill switch banner (red, only when engaged)

> `⛔ AUTO-TRADER HALTED — refusing all new entries (paper + live)`

**When you see it:** the kill switch is engaged. Auto-trader will refuse every new entry signal (paper AND live) until released. Existing open positions are unaffected and will still hit their exits.

**How to release:** click the `release` button on the banner. Confirms via popup, then the auto-trader resumes on the next pipeline run.

**How to engage:** runs through `POST /api/kill_switch` from a script, or by editing `data/kill_switch.json`. Typically triggered by the auto-deactivation system when a strategy diverges badly from backtest.

### Macro strip

Three values across the top: **VIX · T10Y2Y · DXY**. From FRED, refreshed by the daily `monitoring.macro_fetcher` schtask at 18:30.

| Indicator | What it tells you |
|---|---|
| **VIX** | Fear gauge. <15 = calm, 15-25 = normal, >25 = elevated, >40 = panic. Strategies behave very differently across regimes — the `edge slices > vix quartile` card on Research shows this per strategy. |
| **T10Y2Y** | 10-year minus 2-year Treasury yield. **Negative = inverted curve** = historically precedes recessions. Currently +0.50 = barely positive. |
| **DXY** | Trade-weighted broad dollar index. Rising USD = headwind for international + commodities. |

**How to use:** glance at VIX before market open. If it's spiking, mean-reversion strategies tend to underperform (oversold gets more oversold). Trend strategies tend to overperform.

---

## 2. Monitor — action queue

> Lists positions that need a manual decision right now.

Triggered when a position is **down >8%** with **no exit signal** — the system flags it for review rather than waiting for the strategy's own exit logic.

Each row shows:
- Strategy that opened it
- Symbol + entry → current price
- % loss
- A copy-paste string (click to select, Ctrl+C to copy) so you can paste into a chat or Notion
- "open in TV ↗" link to the TradingView chart

**How to use:** these are not auto-exits. The action queue is your "you should look at this" inbox. You decide whether to hold (strategy hasn't exited yet for a reason — maybe a rebound is likely) or manually close.

---

## 3. Monitor — auto-trader · control

Two toggles + a mode badge.

| Mode badge | Meaning |
|---|---|
| **DISABLED** | `auto_trade.enabled = false`. Nothing fires. Pure observation mode. |
| **DRY-RUN** | Enabled but logs would-be orders without submitting. Use this to verify a config change before real fills. |
| **ACTIVE** | Enabled + dry_run off. Real Alpaca paper orders fire on every eligible signal. |

**Two buttons:**
- `turn ON` / `turn OFF` — flips `auto_trade.enabled`
- `go ACTIVE` / `go DRY-RUN` — flips `auto_trade.dry_run`

Both POST to `/api/auto_trade/toggle` and update `config/settings.json` in place.

**Eligibility thresholds** are shown below the buttons: a strategy must have ≥30 closed outcomes, mean return ≥0%, and sharpe-ish ≥0.10 to fire.

**Live capital note:** "ACTIVE" means real *paper* orders. Live capital is a separate config (`auto_trade.live_strategies` array) and is empty today. When you flip a strategy to live, the kill-switch protects both modes.

---

## 4. Monitor — manual triggers

Three buttons that spawn subprocesses on-demand:

| Trigger | What it runs |
|---|---|
| **Run daily report now** | The full EOD pipeline: snapshots + fires + news + outcomes + Notion post + Telegram summary + auto-trader pass. Same thing that runs nightly via schtask. |
| **Run intraday scan now** | Synthesizes today's in-progress bar and projects EOD fires. Helps you preview what will fire after close. |
| **Run auto-trader now** | Walks today's 1d signals and submits (or logs) paper orders. Honors dry_run setting. |

Feedback goes to: Telegram, Notion daily-reports DB, and `trading.db`.

**Common use:** if you've just changed `config/settings.json` (sizing, thresholds, etc.), run the auto-trader to see the effect immediately rather than waiting for next EOD.

---

## 5. Monitor — TV webhook · cloudflare tunnel

Shows whether the Cloudflare quick tunnel is up — it's how TradingView alerts reach the local webhook receiver.

**If empty/missing:** run `schedulers/start_tv_tunnel.bat` in a separate shell. URL appears in `data/tunnel_url.txt` within ~5 seconds and shows up here on next dashboard refresh.

**Why it matters:** TradingView alerts → tunnel URL → local webhook → `signals` table → auto-trader. No tunnel = no TV-triggered signals.

---

## 6. Monitor — today's report

The latest row from the `daily_reports` table.

| Empty state | What it means |
|---|---|
| `(no report yet today)` | EOD pipeline hasn't run since midnight. Either it's pre-close, or the schtask hasn't fired yet. |
| Has content | EOD ran. Card shows P&L, fires count, outcomes count, top strategy, top losers. |

**How to use:** this is your "what happened today" summary. Match it against your expectations — if the auto-trader fired 4 orders but `paper orders today` shows 0, something blocked them (regime gate, drawdown throttle, cool-down).

---

## 7. Monitor — account (alpaca paper)

Live state from Alpaca API:

| Field | What it is |
|---|---|
| **portfolio** | Total account value = cash + position market value |
| **cash** | Buying-side liquidity (T+1 in real markets, instant in paper) |
| **buying power** | Cash × margin multiplier (typically 2× for regulation T) |
| **equity** | Same as portfolio for paper; differs from portfolio in real margin accounts |
| **day-trades** | Count of round-trips today. >3 in a 5-day window on accounts < $25k triggers PDT (pattern-day-trader) restrictions. Not an issue at our paper size but worth knowing. |

**Heartbeat:** `(heartbeat Nm ago)` chip next to the card title. >5 min stale = the monitor loop is probably down. Check `logs/heartbeat.log`.

---

## 8. Monitor — open positions

The most important card on the page. Every row is an open paper position.

| Column | Meaning |
|---|---|
| **strategy** | Which strategy opened it |
| **symbol** | Ticker + TV chart link (`↗ TV`) |
| **entry date** | When the open fill happened |
| **entry** | Fill price at open |
| **current** | Latest price from `snapshots` (EOD close, refreshed by daily pipeline) |
| **unreal** | Unrealized P&L %. Green = winning, red = losing |
| **stop** | Effective stop level. Hover for breakdown (entry stop vs trailing). `—` = no stop recorded (mean-reversion strategies typically don't trail; they exit on signal). |
| **to stop** | % distance from current price to stop. **Green ≥5%** = comfortable cushion. **Amber 2-5%** = getting close. **Red <2%** = about to trigger. |
| **days** | Days held |

**How to use during the day:**
- Scan the **unreal** column for any position deeply red (-8% or worse) — it'll already be in the action queue but glance for ones approaching that threshold
- Scan the **to stop** column for red entries — those are positions about to auto-exit on next bar
- Click the `↗ TV` link to see the chart and decide if you should manually close before the trailing stop fires

**Currently:** all positions show `—` for stop because they're mean-reversion strategies (no trailing stop method declared). Once the trend strategies (donchian, ma_cross, new_high_volume) start opening positions, those rows will show actual stop levels.

---

## 9. Monitor — auto-trader · paper orders today

Orders the auto-trader submitted today. Empty most days (signals are infrequent).

| Field | Meaning |
|---|---|
| **time** | When submitted (ET) |
| **side** | buy / sell |
| **strategy + symbol** | What fired the order |
| **qty + fill price** | Quantity + fill price from Alpaca |
| **status** | submitted / filled / rejected / cancelled |
| **pyramid_tier** | If pyramidable: 0 = initial entry, 1+ = add-ons |

**How to use:** sanity check that the auto-trader actually placed orders for the signals you expected. If a signal fired but no order appears, something blocked it — check the daily report card for a SKIP reason.

---

## 10. Monitor — today's signals

The raw signals table for today. One row per `(strategy, symbol)` fire.

A signal is just "this strategy said BUY/SELL on this bar." It may or may not have been acted on — gating happens at the auto-trader layer (eligibility, regime, cool-down, earnings veto, etc.).

**How to use:** if you see signals here but no corresponding orders in section 9, the auto-trader filtered them out. Usually visible in `today's report` SKIP reasons.

---

## 11. Monitor — recent news

The 15 most recent news items from the `news` table, populated by `monitoring.news_fetcher`.

Each row shows: source · timestamp · primary symbol tag · headline (linked to article).

**How to use:** quick context when a symbol moves unexpectedly. "Why is XHB down 3%?" → scan the news card for XHB headlines.

The **news sentiment overlay** card on the Research page slices outcome P&L by entry-day news mood.

---

## 12. Research — strategy edge

The headline edge table. One row per strategy with ≥1 closed outcome (1d bars only).

| Column | Meaning | Healthy range |
|---|---|---|
| **strategy** | Strategy id (botnet101 prefix stripped). Hover shows confidence + signal count over X days. |
| **n** | Closed outcome count. **Statistically significant** at ≥30. |
| **mean** | Mean per-trade return % | Mean-reversion: +0.5% to +3% typical. Trend: +2% to +10%. |
| **win rate** | Win % | Mean-reversion: 60-80% typical. Trend: 30-45% typical. |
| **sharpe-ish** | Per-trade mean / per-trade stdev. **NOT annualized Sharpe — don't compare to fund Sharpes.** | >0.20 = real edge. >0.50 = strong. |
| **max loss / max win** | Worst and best single trade |

**Degraded badge** (orange "DEGRADED"): the auto-deactivation system has flagged this strategy because its last-N sharpe is <50% of all-time. Either the edge has decayed or it's in a temporary slump. Worth reviewing.

**Confidence tier** (high / medium / low): based on n + observation_days. High = trust the numbers, low = small sample, suspect.

**How to use:** mean × win-rate combo tells the story. A strategy with +0.2% mean and 80% win rate is fine but has tiny edge per trade — sizing matters more. A strategy with +2% mean and 60% win rate carries real punch.

---

## 13. Research — equity curves

One row per active strategy. **All numbers are pre-cost backtest** at 10% per-trade sizing — *not* live paper.

| Column | Meaning |
|---|---|
| **strategy** | Strategy name |
| **sparkline** | Visual of the compound equity curve (start at 1.0) |
| **CAGR** | Compound annual growth rate. **The single best comparison number.** |
| **max DD** | Max drawdown on the compound curve. Bounded -100% (can't lose more than your bankroll). |
| **n trades · period** | Sample size + time window covered |

**Click any row** to open the full equity curve chart with drawdown overlay.

**Sizing assumption:** every trade gets 10% of running equity. So a +2% trade at $100k equity adds $200, not $2000. This is roughly fractional-Kelly territory — realistic for diversified mean reversion. Tooltip on CAGR explains this.

**What paper will actually deliver:**
- The backtest CAGR is **pre-cost** — no slippage, no spread, no transaction friction
- Realistic paper run typically delivers **30-50%** of backtest CAGR
- So +50%/yr backtest → expect ~15-25%/yr paper. Still very good if it holds.

---

## 14. Research — edge slices

The same strategy edge sliced three ways:

### day of week
Some strategies are day-specific. `turn-around-tuesday` famously fires Mondays after sell-off Fridays. Look for cells with dramatically different mean/sharpe by day — that's a real seasonality effect.

### market regime
Slices by the regime classifier (trending_up / chop / trending_down / unknown). **"(unknown)"** rows = signals fired when regime classifier had low confidence; treat with normal weight. Other rows tell you which regimes a strategy actually works in.

### vix quartile
Slices by VIX-at-entry into Q1 (calmest) through Q4 (most volatile). **Most strategies degrade in Q4** — mean reversion fails because oversold gets more oversold. Trend works better in Q3-Q4. Currently shows "waiting on FRED VIX overlay" until enough historical signal+VIX joins accumulate.

**How to use:** spot strategies that ONLY work in certain conditions. If 3-bar-low has +3% mean in calm VIX and -0.5% in panic VIX, you'd want the regime router to suppress it during VIX spikes.

---

## 15. Research — strategy correlation

A pairwise correlation matrix of daily P&L across strategies. Values from -1 (anti-correlated) to +1 (perfectly correlated).

**Color-coded:** strong correlations stand out visually.

**Why it matters:** if two strategies are correlated >0.7, they're effectively the same strategy from a portfolio-risk standpoint. You're doubling down on the same edge with double the capital — which means double the drawdown when that edge fails. The card flags these as `redundant pairs (|r| ≥ 0.70)` at the bottom.

**How to use:** if you see a redundant pair, consider dropping one or reducing sizing. The `consec-bearish ↔ buy-5day-low: 0.86` pair currently flagged means those two fire on the same setups.

---

## 16. Research — slippage burn

> backtest edge vs paper fills

Currently shows "no strategies with both backtest baseline and closed paper pairs yet." Populates once a strategy has at least N closed paper round-trips (entry + exit).

When populated: shows for each strategy:
- **Backtest mean** = what the closed outcomes table says
- **Paper mean** = what actual Alpaca paper fills delivered
- **Burn** = backtest − paper (positive = paper is worse, which is normal)

**How to use:** a burn of 20-40% is normal (slippage + spread). A burn >60% means the strategy is hitting a price-impact ceiling — either symbols are too illiquid for the size, or fills are slipping badly. Reduce sizing or drop the symbol.

---

## 17. Research — fill latency

> submitted_at → filled_at per strategy

For Alpaca paper: latency is typically 1-5 seconds. Real broker fills can be 100ms-2s.

**Populates** once you have any filled paper trades with both timestamps populated.

**How to use:** if latency suddenly jumps (15s+ median), either Alpaca is degraded or the auto-trader process is sleeping somewhere it shouldn't. Check `logs/auto_trader.log`.

---

## 18. Research — edge diff

> backtest vs paper fills

Like slippage burn but more granular — compares not just mean but win rate, sharpe, distribution.

**Populates** once you have closed paper round-trips.

**How to use:** if the paper win-rate is 10+ points lower than backtest win-rate, you've got a real backtest-vs-reality gap. Either backtest is over-fit, or you're entering during conditions backtest didn't fully cover.

---

## 19. Research — news sentiment overlay

For each strategy, slices closed outcomes by the news sentiment on the entry day (±1d):

- **no news** = no headlines around entry date
- **positive / negative / mixed** = sentiment classifier verdict from headlines

**How to use:** some strategies have very different P&L profiles based on news mood at entry. If a strategy makes +3% mean on no-news entries but -0.5% on negative-news entries, you'd want the `negative_sentiment_threshold` veto turned on (it's in `auto_trade_settings`).

---

## 20. Daily trading workflow — what to do when

### Before market open (9:00 ET)
1. Open `/` (Monitor). Glance at: Kill switch (should be hidden), Account heartbeat (should be recent), Macro (especially VIX).
2. Check Action queue — anything that needs a manual decision today?
3. Check Open positions `to stop` column — anything close to triggering?

### During market hours
The monitor auto-refreshes every 30s. Cards to watch:
- **Today's signals** — anything fire?
- **Today's paper orders** — were they actually submitted?
- **Open positions** — any new ones from today's fills?
- **Recent news** — context for unexpected moves

### After close (16:30 ET)
The EOD pipeline runs automatically. After ~5 minutes:
- Open `/research`. Check **strategy edge** — any DEGRADED badges newly appearing?
- Check **equity curves** — visual sanity check
- Glance at **slippage burn** + **edge diff** if populated — paper vs backtest gap healthy?

### Weekly
- Run `scripts/weekly_divergence_report.py` (or wait for the Sunday Notion post)
- Review any strategies flagged for auto-deactivation
- Consider `--demote` on persistent under-performers

---

## 21. Reading charts — patterns that signal a trend start

This section is for when you're looking at a TradingView chart and trying to decide whether a setup is worth taking. The system's strategies fire on quantitative rules (close > 20-day high, RSI < 10, etc.) — but as a human you can layer pattern recognition on top to filter for higher-quality entries.

**Each pattern below:**
- ASCII diagram (rough — open a real chart to confirm)
- What the candles are telling you
- Which strategy in our system most likely fires alongside this pattern
- Common false-positive trap

### 21.0 Candle anatomy refresher

![Candle anatomy](/api/assets/candle_anatomy.svg)

**Green body:** close > open. Buyers won the bar.
**Red body:** close < open. Sellers won the bar.
**Long wick:** price went somewhere but didn't stay (rejection).
**Big body, small wicks:** strong directional move.
**Small body, big wicks (doji):** indecision.

---

### 21.1 Bullish engulfing — reversal at lows

![Bullish engulfing](/api/assets/bullish_engulfing.svg)

A red bar then a green bar whose body is larger than the red's body (engulfs it from open to close). The green bar's open is below the red's close; the green's close is above the red's open.

**What it means:** buyers came in hard and overwhelmed the sellers. Often marks the end of a short pullback in an uptrend, or the bottom of a longer decline.

**Pairs with our strategies:** ★ **3-bar-low**, ★ **buy-5day-low**, **consec-bearish** — all mean-reversion buys at oversold extremes. The bullish-engulfing candle right after the system fires is strong confirmation.

**False-positive trap:** engulfing in the middle of a strong downtrend often gets sold into the next day. Look for one near support (200-day MA, prior swing low) — that's the high-quality version.

---

### 21.2 Morning star — three-bar reversal

![Morning star](/api/assets/morning_star.svg)

Three bars: (1) big red, (2) small body (color irrelevant — often a doji), (3) big green that closes above the midpoint of bar 1.

**What it means:** sellers pushed hard, then indecision, then buyers took over. The middle bar is the pivot — the moment momentum flipped.

**Pairs with our strategies:** ★ **3-bar-low**, ★ **turn-around-tuesday** (Monday = bar 1, Tuesday = bars 2-3 reversal).

**False-positive trap:** if bar 3 doesn't close above bar 1's midpoint, it's not a real morning star — it's a dead-cat bounce that'll likely roll back over.

---

### 21.3 Hammer / pin bar — single-bar rejection at lows

![Hammer](/api/assets/hammer.svg)

One candle with a small body near the top and a long lower wick (at least 2× the body length). Color of body matters less than the rejection.

**What it means:** during the bar, price probed lower, hit a buyer wall, got pushed back up. The lower wick is the footprint of failed selling.

**Pairs with our strategies:** ★ **3-bar-low**, ★ **consec-below-ema**, **consec-bearish** — mean reversion. Especially valuable if the hammer forms at a key level (200-day MA, prior swing low).

**False-positive trap:** a hammer in the middle of a downtrend with no support level beneath it is often just a brief rest, not a real bottom. Need confirmation: the NEXT bar should close above the hammer's high.

---

### 21.4 Three white soldiers — trend confirmation

![Three white soldiers](/api/assets/three_white_soldiers.svg)

Three consecutive green bars, each opening within the prior body and closing above the prior close. Each bar adds higher highs and higher closes. Body sizes should be roughly similar (not shrinking).

**What it means:** sustained buying pressure across three sessions. Trend continuation or the start of one. Very common after a base / consolidation breaks.

**Pairs with our strategies:** ★ **trend-donchian-breakout-20**, ★ **trend-new-high-volume**, ★ **trend-ma-cross-20-50** — all trend-following strategies. If you see this pattern AND a Donchian fire on the same bar, that's a top-tier setup.

**False-positive trap:** if bar 3 has tiny body / long upper wick, momentum is stalling — likely a pullback coming. Want bodies expanding or at least holding steady.

---

### 21.5 Bullish flag — continuation in an uptrend

![Bullish flag](/api/assets/bullish_flag.svg)

Strong rally up (the "pole"), then a brief 2-5 bar consolidation that tilts slightly down or sideways (the "flag"). Volume drops during the flag, then expands on the breakout.

**What it means:** market took a breather. Buyers are still in control — they're just waiting for the next leg.

**Pairs with our strategies:** ★ **trend-donchian-breakout-20** (when the flag breaks above its high). Pyramiding strategies love these — they're how add-on entries get triggered.

**False-positive trap:** if the flag tilts UP (against the trend), it's actually a bearish wedge. Flags should tilt slightly against the prior move (down in an uptrend).

---

### 21.6 Inside bar breakout — coiled spring

![Inside bar breakout](/api/assets/inside_bar.svg)

A bar whose entire range (high to low) is inside the prior bar's range. Compressed price action = volatility about to expand.

**What it means:** market is consolidating, equilibrium between buyers and sellers. The first close OUTSIDE the inside bar's range is the breakout direction — that's the signal.

**Pairs with our strategies:** **trend-donchian-breakout-20**, **trend-new-high-volume** — both like the energy release from compression.

**False-positive trap:** multiple inside bars in a row (3+) tend to be lower-conviction breakouts — too much indecision. Best signals come from ONE inside bar after a strong move.

---

### 21.7 Donchian breakout — what our trend strategy actually trades

![Donchian breakout](/api/assets/donchian_breakout.svg)

Look at the past 20 daily highs. Today's close is above ALL of them. **That's the Donchian-20 signal — `trend-donchian-breakout-20` fires today.**

**What it means:** sustained buying broke the multi-week ceiling. The price level that capped the market for 20 days is now in the rearview mirror. Statistically, breakouts to fresh highs tend to continue.

**Pairs with our strategies:** ★ This IS what `trend-donchian-breakout-20` fires on. The entry is here. Trailing stop tracks the highest high since entry, exits when close drops below 10-day low.

**False-positive trap:** breakouts on thin volume often fail. Confirmation = volume on the breakout day ≥ 150% of 20-day average. This is exactly why we also have `trend-new-high-volume` as a separate strategy — it adds the volume filter.

---

### 21.8 Higher highs + higher lows — the trend itself

![Higher highs + higher lows](/api/assets/higher_highs_lows.svg)

Each subsequent **swing high** is higher than the prior swing high (HH). Each subsequent **swing low** is higher than the prior swing low (HL). This IS what an uptrend is.

**What it means:** literally the definition. If you're trying to decide "is this in an uptrend or not?" — just check whether the most recent swing high and swing low are both above the previous ones.

**Pairs with our strategies:** ★ All trend strategies want to be entering during HH/HL sequences. Mean-reversion strategies want to fade them at the swing lows (buy the dip when HL forms).

**False-positive trap:** noise on small timeframes. Confirm on daily bars at minimum. A single break to a lower low doesn't end the trend — usually need TWO consecutive failures (lower high + lower low) to confirm reversal.

---

### 21.9 Volume confirmation — the missing variable

![Volume confirmation](/api/assets/volume_confirmation.svg)

Volume doesn't have a candle shape, but it's the single most underrated indicator. Three rules:

| Setup | Volume should | Why |
|---|---|---|
| **Breakout** (price above resistance) | EXPAND (≥150% avg) | Real breakouts pull in new buyers. Thin breakouts get re-tested and often fail. |
| **Pullback in uptrend** | CONTRACT | Healthy. Means sellers aren't piling on — just taking small profits. |
| **Reversal candle (hammer, engulfing)** | EXPAND | Confirms the rejection had real flow behind it. Thin reversals are often just gaps in liquidity. |

**Pairs with our strategies:** ★ `trend-new-high-volume` literally has this baked in. For the others, use volume as a manual filter — if 3-bar-low fires but volume is below average, the bounce has less force.

---

### 21.10 What patterns DON'T tell you

Patterns are **probabilistic**, not deterministic. A bullish engulfing has maybe a 55-60% chance of triggering a bounce over the next 5 bars — meaningful edge, but not certainty.

**Three things to layer with pattern reading:**

1. **Macro context** (Section 1's VIX / T10Y2Y) — bullish patterns in a panic VIX regime fail more often.
2. **Strategy edge data** (Research page) — if our system's strategy ALSO fires on the same bar as your pattern, that's compounded evidence.
3. **Position sizing** — even great patterns fail 30-40% of the time. Size so a loss doesn't ruin your week.

**Pattern fishing trap:** if you stare at a chart long enough, you'll find a pattern. Don't go looking — let the strategy fires come to you, then check whether the pattern adds conviction. The system's signals are the trigger; patterns are the confirmation.

---

## 22. Glossary — explained like you're new to this

Grouped by category so concepts cluster together. Within each group, terms are alphabetical for lookup.

### 22.1 Trading basics — universal concepts

- **Bar (or candle)** — One unit of price data covering a time slice. A "daily bar" = the open / high / low / close prices for one trading day. A "5-minute bar" = the same four prices over 5 min. Each bar is one row in our DB and what strategies look at.
- **Bar interval** — How long each bar covers: `1d` (daily), `15m`, `5m`, `1h`. Our daily strategies look at 1d bars; intraday strategies at 5m or 15m.
- **Bid / Ask / Spread** — Bid is the highest price someone will pay to buy. Ask is the lowest someone will sell at. The gap = spread. KRE has a 1-2 cent spread; less-traded ETFs can have 5+ cents. You pay the spread on every round-trip.
- **Day trade** — Buying and selling the same symbol within the same trading day. **Pattern Day Trader (PDT) rule**: under-$25k accounts can only do 3 day-trades per 5-day window. Paper accounts bypass this; live accounts must obey.
- **Edge** — The statistical advantage a strategy has. "Edge" = the average per-trade return over many trades minus what random chance would produce. A strategy with mean return +2% per trade has a real edge; one near 0% doesn't. The Research page Strategy Edge card is literally measuring this for each strategy.
- **Entry / Exit** — When you open a position (entry, e.g., BUY) and when you close it (exit, e.g., SELL). Each round-trip is one trade with an entry and exit. A trade isn't a real result until you've exited.
- **Fill** — When your order actually executes at a price. Order submitted at $100 might fill at $100.02 if there's spread/slippage. That's the **fill price**.
- **Fill latency** — Time between submitting the order and getting it filled. Paper Alpaca: 1-5 seconds. Live ETFs: ~100ms-2s.
- **Long / Short** — Long = buying expecting price to go up. Short = borrowing and selling expecting price to go down. Our system is **long-only** today.
- **Market order** — "Fill me at whatever the next available price is." Fast but risky (slippage). Our auto-trader uses market orders.
- **Limit order** — "Fill me at $X or better." Slower fills but predictable price.
- **Position** — Shares you currently own (or owe, if short). An "open position" = you've bought but haven't yet sold. A "closed position" = round-trip complete; profit/loss is realized.
- **Round-trip** — A complete entry-then-exit cycle. Buy 10 KRE → Sell 10 KRE = one round-trip.
- **Slippage** — Difference between the price you expected and the price you actually got. Real money cost. Backtests usually ignore this; real fills don't.
- **Stop loss (or just "stop")** — A pre-set price at which you exit a losing position to cap losses. "Stop at $95" = sell automatically if price hits $95.
- **Take profit** — Opposite of stop loss. Pre-set exit price to lock in gains.

### 22.2 Strategy concepts

- **Backtest** — Running a strategy against historical bars to see how it would have performed. Backtest numbers are upper bounds — real trading is always worse due to slippage, spread, and execution friction.
- **Compute function** — The Python function each strategy uses to decide "buy or not." Takes a dataframe of bars in, returns a dataframe with `long_entry` / `long_exit` boolean columns.
- **Fire** — A strategy's compute function returned `long_entry=True` on a bar. Doesn't necessarily mean an order is placed — see eligibility gates.
- **Mean reversion** — Strategy class that bets prices revert to their average after extreme moves. Buy oversold, sell when it bounces. High win rate (60-80%) but small per-trade profits. Loses badly in trending markets.
- **Trend following** — Opposite of mean reversion. Bets prices continue in the current direction once a breakout happens. Low win rate (30-40%) but the wins are 5-10× the average loss. The Turtle strategy is the famous example.
- **Breakout** — Strategy class that fires when price breaks above a defined level (e.g., 20-day high). Examples: Donchian, ORBO.
- **Pyramiding** — Adding to a winning position with smaller-size add-ons. If you bought 10 shares at $100 and price runs to $105, you add 5 more, then 2.5 more at $110, etc. Multiplies upside on trends but multiplies risk if it reverses. Our trend strategies pyramid; mean-reversion does not.
- **Signal** — Generic term for the strategy saying "act now." A signal can be a long_entry, long_exit, etc. Same thing as a "fire" essentially.

### 22.3 The strategies in this system

Each one's `compute_fn` lives in `strategies/<family>/<name>.py`. Here's what each tries to do in plain English.

**Mean-reversion (Botnet101 family — all long-only, daily bars):**

- **3-bar-low** — Buy when today's close is the lowest in the past 3 days. Bet: short-term oversold often bounces.
- **buy-5day-low** — Buy when today's close is the lowest in the past 5 days. Same idea, slightly more selective.
- **consec-bearish** — Buy after N consecutive red (down) bars. Bet: extreme sell pressure exhausts itself.
- **consec-below-ema** — Buy after N consecutive bars closing below the 200-day exponential moving average. Bet: brief dips in an uptrend.
- **4bar-momentum-reversal** — Buy after 4-bar momentum signal flips bearish-to-bullish. A reversal-of-reversal play.
- **turn-around-tuesday** — Specifically: Monday closes down ≥1%, buy at Tuesday's open. Famous retail/institutional rotation pattern — Friday's pain gets resold Monday by retail, then institutions buy Tuesday.
- **turn-of-month** — Buy on the last 1-2 days of the month and hold into the first 3-4 days of next month. Bet: 401k contributions and rebalancing flow create predictable end-of-month demand.

**Trend (3 new strategies, currently in grace period — daily bars):**

- **trend-donchian-breakout-20** — Buy when price closes above its 20-day high. Exit when it closes below its 10-day low. Classic Turtle channel breakout — the original quantitative trend strategy from the 1980s.
- **trend-ma-cross-20-50** — Buy when the 20-day EMA crosses above the 50-day EMA. Exit on opposite cross. Smooth, slow signals; works best in clean trends.
- **trend-new-high-volume** — Buy when price makes a new 52-week high accompanied by ≥150% of average volume. Volume confirmation filters out fakeouts.

**Intraday (just shipped — fire on 5m or 15m bars during market hours):**

- **intraday-mr-3bar-low-15m** — 3-bar-low logic ported to 15-minute bars. Fires when the most recent 15m close is the lowest of the last 3 bars.
- **intraday-orbo-5m** — Opening Range Breakout. The first 20 minutes of the trading day (09:30-09:50 ET) form the "opening range." First 5m close above that range's high = buy.
- **intraday-orb-pivots-5m** — Same as ORBO but requires a confirming break above the prior day's R1 floor pivot. Lower fire rate, higher quality.

### 22.4 Performance metrics — how we measure strategies

- **Annualized return** — Total return scaled to a 1-year period. A strategy that made +10% in 6 months has a +21% annualized return (compounded). Use this to compare across strategies with different time periods.
- **CAGR (Compound Annual Growth Rate)** — Same as annualized return. The headline number on the equity curve card. If a backtest shows +50% CAGR, that's "if you let it compound, you'd grow your account by 50% per year." Real-world paper trading usually delivers 30-50% of backtest CAGR after slippage.
- **Drawdown** — How far you're down from a previous peak. If your account hits $110k then falls to $98k, the drawdown is -10.9% (from peak, not from start). Drawdowns are how strategies actually fail — even a winning strategy goes through them.
- **Max drawdown** — The worst peak-to-trough decline over the period. A strategy with +50% CAGR but -40% max drawdown is unpleasant to hold. -10-20% is normal; -40%+ is severe.
- **Mean return** — Average per-trade return. Don't confuse with total return. If you do 100 trades averaging +2% each, that's +2% mean (not +200% total — depends on sizing/compounding).
- **Sharpe ratio (and "sharpe-ish")** — Risk-adjusted return. Mean return divided by volatility. Higher = smoother returns. >1.0 is good for a real fund. **Our "sharpe-ish"** is a simplified per-trade version (mean ÷ stdev across trades, not annualized). Compare it across our strategies, not to professional fund Sharpes. See section 22.4.
- **Volatility (or stdev)** — How much returns vary trade-to-trade. High volatility = some big wins AND big losses; harder to size confidently.
- **Win rate** — Out of 100 trades, how many made money. **Misleading on its own**: a 70% win rate sounds great but a strategy with 70% wins of +1% and 30% losses of -5% loses money. Always check win rate alongside mean return.

### 22.5 System-specific concepts

- **ACTIVE mode** — Auto-trader is enabled AND dry_run is off → real paper orders flow to Alpaca. Different from "LIVE capital" (which we haven't enabled).
- **Auto-trader** — The engine that takes signals and turns them into actual paper orders. Lives in `monitoring/auto_trader.py`. Runs at EOD (and now also during the day, after Phase 5).
- **bar_interval** — The bar timeframe a strategy operates on: `1d` for daily, `15m` for 15-minute, `5m` for 5-minute. Our strategies declare this and the auto-trader respects it.
- **Burn (slippage burn)** — Difference between backtest mean and real-fill mean. A "20% burn" means real trading captures 80% of backtest edge. Normal for liquid ETFs; >60% is a warning sign.
- **Cool-down** — When a strategy's recent N closed trades have all been losers, the auto-trader pauses new entries for X days. Stops you from doubling down into a losing streak. Configurable in `auto_trade.cool_down_losers` / `cool_down_days`.
- **dry_run** — Auto-trader logs what orders it *would* submit but doesn't actually send them. Used for testing config changes safely.
- **Eligibility (gates)** — Conditions a signal must pass before becoming a real order. Today's gates: at least 30 closed outcomes (proven sample), mean return ≥ 0%, sharpe-ish ≥ 0.10. Strategies with `grace_period: true` bypass the 30-outcomes gate but trade smaller.
- **Earnings veto** — Skip entries on a symbol within N days of its next earnings announcement. Earnings cause gaps that break mean-reversion logic. Set via `auto_trade.earnings_veto_days`.
- **Grace period** — Lets new strategies (with n=0 closed trades) fire orders to start collecting data, but at 25% normal size. Auto-graduates once they hit 30+ closed outcomes.
- **Kill switch** — Big red button that halts all auto-trader entries (paper AND live). Existing positions stay open and run their normal exits. Engaged via dashboard banner button, `data/kill_switch.json`, or Telegram command.
- **Live strategies (`auto_trade.live_strategies`)** — List of strategy IDs authorized to use REAL money (live capital). Today this is empty — everything is paper. Adding a strategy here routes its orders to the `alpaca_live` credentials block.
- **Outcome** — A closed round-trip (entry paired with its exit). Each row has return_pct, bars_held, entry/exit prices. The Strategy Edge card aggregates outcomes per strategy.
- **paper_trades** — The DB table tracking every actual order submitted to Alpaca. Has order_id, fill_price, status, timestamps. Empty until a strategy fires.
- **Promote / Demote** — `scripts/promote_strategy.py --promote <id>` adds a strategy to TRACKED_STRATEGIES (it starts getting daily fire-checked). `--demote` removes it.
- **Regime** — The market's current character: `trending_up`, `trending_down`, `chop` (sideways), or `unknown`. Calculated from price action across major indices. Drives the regime allocator.
- **Regime allocator** — Routes capital between strategy classes based on regime. In a trend regime: 70% capital to trend strategies, 30% to mean-reversion. In chop: reverse. Mixed/unknown: 50/50.
- **Sharpe-ish** — See section 22.4 — our per-trade simplified Sharpe.
- **Signal** — A row in the `signals` table representing a strategy firing on a specific (symbol, bar). Different from a paper_trade — a signal might be filtered out by gates and never become an order.
- **TRACKED_STRATEGIES** — The list in `monitoring/config.py` of all strategies the system actively monitors. Currently 13: 7 daily mean-reversion, 3 daily trend, 3 intraday.
- **Trailing stop** — A stop-loss that moves up as price advances (for longs). Never moves down. Three methods: `atr_trail` (default — N × ATR below current price), `chandelier` (high-since-entry minus N × ATR), `percent_trail` (fixed % below highest high).

### 22.6 Market indicators (shown in the macro strip)

- **DXY (US Dollar Index)** — Tracks the dollar against a basket of major currencies. Rising DXY = stronger dollar = headwind for international stocks, commodities, and exporters. Currently uses FRED's DTWEXBGS series.
- **ETF (Exchange-Traded Fund)** — A basket of stocks traded like one share. SPY tracks the S&P 500. KRE tracks regional banks. We trade ETFs not individual stocks because they're more liquid and we get sector exposure without single-stock risk.
- **T10Y2Y** — The 10-year minus 2-year Treasury yield. **Negative = inverted curve** = historically a recession-warning signal. Positive (currently +0.50) = normal expansion. Big tool for reading macro context.
- **VIX (CBOE Volatility Index)** — "Fear gauge." Measures expected S&P 500 volatility from options prices. <15 = calm market. 20-25 = nervous. >30 = panic. Mean-reversion strategies degrade in high VIX; trend strategies tend to outperform.
- **VIX quartile** — VIX values are bucketed into 4 ranges (Q1 calmest, Q4 most volatile). The Research page slices strategy P&L by quartile so you can see which strategies survive a panic.

### 22.7 Tickers you'll see frequently

- **SPY** — S&P 500 ETF. The "broad market" proxy.
- **QQQ** — Nasdaq-100 ETF. Tech-heavy.
- **IWM** — Russell 2000 ETF. Small-cap stocks. More volatile than SPY/QQQ.
- **XLE** — Energy sector ETF (Exxon, Chevron, etc.).
- **XOP** — Oil & gas exploration ETF. More volatile than XLE.
- **XBI** — Biotech ETF. High-volatility sector.
- **KRE** — Regional banking ETF. Sensitive to interest rates.
- **XME** — Metals & mining ETF.
- **GDX** — Gold miners ETF.
- **XHB** — Homebuilders ETF.
- **BTC-USD / ETH-USD / SOL-USD** — Major cryptocurrencies. Tracked but live-crypto trading not enabled yet.

### 22.8 Dashboard-specific terms

- **Action queue** — Cards on the Monitor page listing positions that need a manual decision (typically: down >8% with no exit signal yet).
- **Edge slices** — Strategy edge broken down by condition (day-of-week, regime, VIX quartile). Reveals which strategies work in which conditions.
- **Forecast** — Per-strategy projection on the edge table showing expected fires-per-month and confidence. Based on the last N days of signal frequency.
- **Heartbeat** — A periodic write to `logs/heartbeat.log` showing the monitor process is alive. The `(heartbeat Nm ago)` chip on the Account card shows freshness.
- **Macro strip** — The VIX / T10Y2Y / DXY line at the top of the dashboard.
- **TV tunnel** — A Cloudflare quick tunnel that lets TradingView alerts reach your local webhook. Only needed if you set up TV alerts as a signal source (not required for the auto-trader's own signals).

### 22.9 Money / risk terms

- **Buying power** — Cash × margin multiplier (usually 2× for Reg-T accounts). $100k cash = $200k buying power. Doesn't change your risk exposure ceiling — you can still over-leverage yourself.
- **Equity** — Total account value = cash + market value of open positions. Same as portfolio in our paper context.
- **Notional** — The dollar value of a position regardless of how much cash you put down. 10 shares at $100 = $1000 notional.
- **Position size** — How big each entry is in dollars (notional) or share count. We cap at `max_position_usd` (default $1000) per entry.
- **PDT (Pattern Day Trader)** — SEC rule: accounts under $25k can only execute 3 day-trades in a 5-business-day window. Our PDT guard (Phase 5) checks this before submitting intraday round-trips.
- **Round-trip cost** — Total cost of a trade = entry slippage + spread + exit slippage. Typically 10-30 bps (0.1-0.3%) on liquid ETFs.
