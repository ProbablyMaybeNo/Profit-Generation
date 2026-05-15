# TradingView — Practical Operating Guide

Operating manual for what you'll actually do day-to-day. No fluff.

---

## 1. The screen — where everything lives

When you log in you land on the chart view. Five regions:

| Region | What it does |
|---|---|
| **Top toolbar** | Symbol search, timeframe selector (1m/5m/1h/D/W), chart-type dropdown, indicator button (`ƒx`), alerts (clock+`+`), compare-symbols, settings |
| **Left sidebar** | Drawing tools — trend lines, Fibonacci, rectangles, text |
| **Main chart** | The price action |
| **Right sidebar** | Watchlists. Pin yours here. |
| **Bottom tray** | Trading Panel (orders), Stock Screener, Pine Editor, Strategy Tester, Alerts list, Notes |

Bottom tray is collapsed by default — click the small "Trading Panel" tab at the very bottom edge to expand it.

---

## 2. Chart basics — reading what you see

**A candle (OHLC bar)** = one bar of the timeframe you selected. Body = open → close. **Green** body = close > open. **Red** body = close < open. Wicks (lines) = high and low extremes during the bar.

**Timeframe selector** (top toolbar): "1" = 1-minute bars, "5" = 5-min, "1h" = hourly, "D" = daily, "W" = weekly. Changing it RE-AGGREGATES the chart on the same symbol.

**Symbol search:** type a ticker. TradingView prefixes with the exchange — `NASDAQ:AAPL`, `AMEX:GDX`, `BINANCE:BTCUSDT`. Mostly you can ignore the prefix.

**Chart-type dropdown:** candles is default. Other useful ones — bars (HLC sticks), line (close-only), Heikin-Ashi (smoothed candles, easier trend-reading).

**Compare button** (top toolbar, looks like `+`): overlay another symbol normalised to %. Useful for "GDX vs SPY" relative-strength views.

**Crosshair** (top-right of main chart, hover icon): click to lock on; shows price + time at any point you hover.

### Changing timeframe — fastest ways

- Click the timeframe selector top-left, pick from dropdown
- Keyboard: type number+unit and Enter
  - `1` `Enter` → 1-minute
  - `5` `Enter` → 5-minute
  - `1` `H` `Enter` → 1-hour
  - `D` `Enter` → daily
  - `W` `Enter` → weekly
- Right-click the chart → "Change interval"
- ⭐ in the timeframe dropdown → pin favourites to the toolbar's bottom row

---

## 3. Indicators — adding studies to a chart

Click `ƒx` (top toolbar) → search by name → click → it overlays.

| Indicator | Search term | What it shows |
|---|---|---|
| Simple Moving Average | "Moving Average" → Simple | Average close over N bars. 200-SMA = classic trend filter |
| Exponential MA | "Moving Average Exponential" | Like SMA but recent bars weighted more |
| RSI | "Relative Strength Index" | Momentum oscillator, 0-100. <30 oversold, >70 overbought |
| Bollinger Bands | "Bollinger Bands" | Mean ± 2 std-dev bands. Squeeze = low vol |
| MACD | "MACD" | Two-line momentum + histogram |
| Volume | "Volume" | Volume bars below price |
| ATR | "Average True Range" | Average bar range, used for stop sizing |
| VWAP | "VWAP" | Volume-weighted average price for the session |

To remove: hover the indicator's name in the top-left chart legend → click the eye icon (hide) or X (delete). To configure: double-click the indicator's name → settings panel.

---

## 4. The trading panel — connecting an account

Bottom tray → **Trading Panel** tab. First time you open it, TradingView asks which broker:

- **Paper Trading** (TradingView's built-in sandbox — what you're using). $100k virtual cash by default. No fees, no slippage simulation.
- **Real broker** (Alpaca, Tradier, OANDA, IB, etc.) — connects via OAuth. Don't pick this until you're ready to trade real money.
- **None / Not connected** — just charting, can't place orders.

Tabs: **Positions** (open trades), **Orders** (working orders not filled yet), **History** (filled + cancelled), **Account Summary** (cash, equity, buying power). Top right of the panel: account dropdown to switch between connected accounts.

---

## 5. Placing a trade — the buy/sell ticket

Two ways to open the order ticket:
1. **Bottom-right shortcuts:** click the green "Buy" or red "Sell" buttons at the very bottom-right of the chart (those big bars showing the current bid/ask). Opens a pre-filled ticket.
2. **Right-click on the chart** → "Trade" → opens ticket at the price you clicked.

The order ticket has these fields:

| Field | What to set |
|---|---|
| **Symbol** | Pre-filled from the chart |
| **Side** | Buy (long) or Sell (short — see §7) |
| **Quantity** | Number of shares/contracts |
| **Order type** | Market / Limit / Stop / Stop Limit (see §6) |
| **Limit price** | Required for Limit + Stop Limit |
| **Stop price** | Required for Stop + Stop Limit |
| **Time in force** | DAY (cancels at close), GTC (good till cancelled), IOC, FOK |
| **Take profit** | Optional — auto-create a sell limit at this price after fill |
| **Stop loss** | Optional — auto-create a sell stop at this price after fill |

Click **Buy** (green) or **Sell** (red) at the bottom of the ticket. Paper market orders fill instantly at the current ask (buy) or bid (sell).

---

## 6. Order types — what each one means

| Type | Behavior | Use it when |
|---|---|---|
| **Market** | Fills immediately at best price | You want IN now, don't care about a few cents of slippage |
| **Limit** | Sits in book; fills only at your price or better | You want to buy CHEAPER than current (or sell HIGHER) |
| **Stop** | Becomes a market order when price crosses the stop | Sell if price drops to X (stop loss), or buy a breakout above X |
| **Stop Limit** | Becomes a LIMIT order when price crosses the stop | Same as stop but you control the worst price you'll accept |
| **OCO** (one-cancels-other) | Two orders linked; one filling cancels the other | Bracket: take-profit limit + stop-loss stop, both protecting one position |
| **Bracket** | "Take profit" + "Stop loss" fields on the order ticket → automatic OCO around the entry | Standard "set and forget" risk-managed trade |

---

## 7. Going short — selling something you don't own

In the order ticket, if you have **no position** in a symbol and click **Sell** with quantity > 0, you've opened a SHORT position. You profit if the price falls.

Caveats:
- TradingView's built-in paper account allows shorts on most US equities. Some real brokers don't, or require margin approval.
- Closing a short: **buy back the same quantity**. The Positions tab's "Close" button does this with one click.

Our Profit Generation system's strategies are **all long-only mean reversion**. We never short.

---

## 8. Stop loss / take profit — your "cut offs"

Three ways to attach a stop to a position:

1. **At entry, via bracket:** in the order ticket, fill the "Stop loss" field with a price or distance. After the entry fills, TV places a SELL STOP at that price.
2. **After entry, via the Positions tab:** click the position → "Add stop loss" → fill price → confirm.
3. **By dragging on the chart:** the position's price line is visible. Right-click → "Add stop loss" → drag the new line to the price you want.

Take profit works identically — creates a SELL LIMIT at your target.

**Trailing stops:** order ticket has a "Trail" option for stop orders. Set a distance (e.g., $0.50 or 1%) and the stop follows price up at that distance, never moving down. Locks in gains on a runner.

To **modify**: click the order in Orders tab, edit price, click Update. Or drag the line on the chart. To **cancel**: Orders tab → right-click → Cancel.

---

## 9. Closing a position

Three ways:
1. **Positions tab → click the X** next to the row. Submits a market order in the opposite direction for the full size.
2. **Reverse:** Positions tab → "Reverse" button. Closes the existing position and opens an equal-size opposite-side position in one transaction.
3. **Manual order:** open the ticket, set side opposite to your position, set quantity = position size, market order.

---

## 10. Watchlists

Right sidebar → click `+` → create a new watchlist or add to an existing one. Each row shows last price + day change + volume. Click a row → loads that symbol on the main chart.

For our system: SPY, QQQ, IWM, XLE, XOP, XBI, KRE, XME, GDX, XHB, BTC-USD, ETH-USD, SOL-USD.

---

## 11. Alerts — the only API TradingView gives you

Top toolbar → **alarm-clock-with-+** icon. Or right-click the chart → "Add Alert".

Conditions: "Crossing", "Crossing Up", "Crossing Down", "Greater Than", "Less Than", or for indicators: condition on the indicator's value (e.g., RSI < 30).

Notification channels:
- In-app popup
- Email
- SMS (Premium plan only)
- Mobile push (TV mobile app)
- **Webhook URL** — POSTs JSON to any URL when triggered. *This is what our `tv_webhook.py` receives.*

For Pine strategies (custom code): the script can call `alert()` with a message. Configure the alert to use that message as the webhook payload.

Alert limits per plan:
| Plan | Active alerts |
|---|---|
| Free | 1 |
| Essential (~$15/mo) | 20 |
| Plus (~$30/mo) | 100 |
| Premium (~$60/mo) | 400 |

---

## 12. Pine Script — the customization language

Bottom tray → **Pine Editor**. Two artifact types:
- **Indicator** (`indicator()`) — plots stuff on the chart, doesn't trade
- **Strategy** (`strategy()`) — simulates trades; backtest in **Strategy Tester** tab

For our purposes you don't need to write Pine — `monitoring/llm_codegen.py` generates Python equivalents. You might publish a strategy in Pine to set up an alert that POSTs to our webhook for cross-validation.

---

## 13. Mobile app

Same UI compressed to phone. Trading panel at the bottom. Alerts as push notifications. Useful for monitoring during the day; not great for placing complex bracket orders.

---

## 14. Cost levels at a glance

| Plan | $/mo | Why upgrade |
|---|---|---|
| Free | $0 | 1 alert. 1 chart per tab. Ads. Some intraday data on certain symbols paywalled. Fine for learning. |
| Essential | ~15 | 20 alerts (covers our 13 tracked symbols). 2 charts per tab. No ads. Real-time on more exchanges. |
| Plus | ~30 | 100 alerts. 4 charts per tab. More indicators per chart. |
| Premium | ~60 | 400 alerts. 8 charts. Custom timeframes (Renko, Range, sub-minute). |

For our system you want Essential at minimum (need ~13 alerts to mirror the tracked universe).

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **Ask** | Lowest price someone is willing to SELL at right now. You buy at the ask. |
| **ATR** | Average True Range — average bar size, used to size stops |
| **Backtest** | Replay a strategy against historical data to estimate performance |
| **Bid** | Highest price someone is willing to BUY at right now. You sell at the bid. |
| **Bid-ask spread** | Ask minus bid. Wider = costlier to trade. |
| **Bracket order** | Entry + stop loss + take profit, all linked |
| **Breakout** | Price moves above resistance (or below support) |
| **Candlestick** | OHLC bar with body + wicks |
| **Day order (DAY)** | Cancels if not filled by close of regular session |
| **Drawdown** | Peak-to-trough decline of equity. Max drawdown = worst ever. |
| **EOD** | End of day |
| **EMA** | Exponential moving average (recent bars weighted more) |
| **Equity curve** | Line of cumulative P&L over time |
| **Fill** | When your order actually executes |
| **Going long** | Buying with the expectation price will rise |
| **Going short** | Selling something you don't own (borrowed), expecting to buy back lower |
| **GTC** | Good Till Cancelled — order stays active across days until you cancel |
| **HOD / LOD** | High of Day / Low of Day |
| **IOC** | Immediate Or Cancel — fill what you can right now, cancel the rest |
| **Limit order** | "Buy/sell at price X or better" |
| **Long** | Position that profits when price goes up |
| **Margin** | Borrowed money used to amplify positions. Comes with interest. |
| **Market order** | "Fill now at any price" |
| **Mean reversion** | Strategy: price tends to return to its average after extremes (our active strategies) |
| **Momentum** | Strategy: price that's moving will keep moving |
| **OCO** | One Cancels Other — two orders linked |
| **OHLC** | Open / High / Low / Close — the four prices that define a bar |
| **Open interest** | (Options/futures) number of contracts currently held |
| **Order book** | List of pending buy/sell orders by price level |
| **Overbought** | Conventional view: RSI > 70, stretched up, prone to mean revert |
| **Oversold** | RSI < 30, stretched down, prone to bounce |
| **P&L** | Profit and Loss |
| **Pip** | Smallest price move in FX (typically 0.0001) |
| **Position** | Currently held trade (long or short, with size) |
| **Position size** | Quantity of shares/contracts you hold |
| **R / R-multiple** | Risk unit. "+2R" = profit equal to 2× your initial stop distance |
| **Resistance** | Price level above current where selling tends to appear |
| **RSI** | Relative Strength Index, momentum oscillator 0-100 |
| **Sharpe ratio** | Mean return divided by standard deviation. Higher = more consistent edge |
| **Short** | Position that profits when price goes down |
| **Slippage** | Difference between expected fill price and actual fill price |
| **SMA** | Simple Moving Average |
| **Spread** | See bid-ask spread |
| **Stop loss** | An order that closes your position if price moves against you to a set level |
| **Stop order** | Becomes a market order when price reaches your stop |
| **Stop-limit order** | Becomes a limit order when price reaches your stop |
| **Support** | Price level below current where buying tends to appear |
| **Take profit** | An order that closes your position when price reaches your target |
| **Tick** | The minimum price increment for a symbol (e.g., $0.01 for most US stocks) |
| **TIF** | Time In Force (DAY, GTC, IOC, FOK) |
| **Trailing stop** | Stop that follows price as it moves favourably, never giving back beyond a set distance |
| **VWAP** | Volume-Weighted Average Price for the session. Institutional benchmark. |
| **Win rate** | Percentage of trades that closed profitable |
