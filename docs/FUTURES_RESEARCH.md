# Futures Research

**Status:** research milestone (Phase 3.4.3). **No code shipped.** This
document is the go/no-go gate for an eventual futures-implementation
milestone. Re-read this before opening that ticket.

**Scope:** evaluating whether the system should add futures (US equity
index, energy, metals) alongside the existing equities + crypto stack.
Futures are appealing for tax treatment (1256), 24-hour liquidity, and
no PDT rule — but the broker landscape is fragmented and the data costs
materially exceed the rest of the stack.

---

## 1. Broker options

Alpaca **does not carry futures** as of 2026-05. Forced to evaluate
third-party brokers with their own APIs:

| Broker            | API quality                           | Commissions         | Funding minimum     | Notes |
|-------------------|---------------------------------------|---------------------|---------------------|-------|
| **Tradovate**     | Modern REST + WebSocket; well-documented | $0.79 / side / contract (Lifetime plan + $25/mo data) | $400 (CME micro)     | Best dev experience. Owned by NinjaTrader Group. Free demo + clean OAuth flow. |
| **NinjaTrader**   | Native API only — no REST. Requires NinjaTrader 8 client running locally for order routing. | $0.59 / side (Lifetime $1500) | $400               | Powerful platform but the API binding is heavyweight; not a great fit for a headless Python service. |
| **IBKR (Interactive Brokers)** | Two APIs — TWS Gateway (well-documented) + Client Portal Web API. Both require an always-on local gateway process. | ~$1.25 / side (commission tiered, depends on monthly volume) | $25,000 (recommended for portfolio margin) | Most comprehensive (futures + options + global). Heavy onboarding; gateway-process model is annoying to ops. |
| **TopstepX / Apex Trader Funding** | Funded-trader programs; not real-account brokers. Skip for now. | n/a                | n/a                 | Different model; out of scope. |

**Recommendation:** **Tradovate first** for the dev experience + reasonable
$25/mo data + micro contract sizing. Migrate to IBKR only if we outgrow
Tradovate's instrument coverage (it's CME-only — no LME / DCE / overseas
exchanges).

---

## 2. Data cost (the painful part)

Futures market data is exchange-fee gated. Even paper trading requires
paid data on most brokers because the exchanges (CME, NYMEX, COMEX,
ICE) charge per data subscriber:

| Data tier                                      | Monthly cost  |
|------------------------------------------------|---------------|
| CME real-time (top-of-book) for non-pro use    | $5–$10        |
| CME L1 + Globex full L1 (e.g. Tradovate Lifetime data plan) | $25           |
| CME L2 (depth-of-market, needed for any kind of microstructure work) | $90           |
| NYMEX/COMEX add-on (CL, GC, SI, NG)            | +$25          |
| ICE (BRN brent oil, ICE softs)                 | +$80          |
| Bundled (CME + NYMEX + COMEX, real-time)       | ~$120–$150    |

**Bottom line:** $120–$150/mo MINIMUM to trade /ES, /NQ, /CL, /GC with
real-time data. That's a 4–5× increase over current Polygon spend
($29/mo). Has to be justified by clear edge.

---

## 3. PDT / margin / pattern rules

The pleasant side of futures:

- **No PDT rule.** Unlimited intraday round-trips regardless of
  account size. This is the single biggest operational unlock for our
  intraday strategies that currently can't run on equity sub-$25k.
- **Higher implicit leverage.** A /MES (E-mini S&P micro) is $5 ×
  index price, so at SPX 5300 the contract notional is ~$26.5k but
  the day-trade margin is ~$50. Position sizing has to be MUCH more
  conservative.
- **No T+1 / T+2 settlement.** Cash settles same-session.
- **Mark-to-market daily** + variation margin called intraday on
  large moves. Account can be force-liquidated at the broker's
  discretion if margin requirements aren't met.
- **Halt rules** at the exchange (limit-up / limit-down). The system
  needs to recognise halt states or it'll thrash trying to enter.

---

## 4. Strategy translation

Per-strategy translation matrix for the front-month index futures
candidates:

| Underlying  | Description                       | Candidates from our roster                                |
|-------------|-----------------------------------|------------------------------------------------------------|
| **/ES (or /MES)** | E-mini / micro S&P 500       | botnet101 cluster (all 7), rsi2-oversold — these target SPY-like behavior; should port cleanly with bar-count semantics already validated in 3.3.2. |
| **/NQ (or /MNQ)** | E-mini / micro Nasdaq-100    | Same as /ES but on QQQ-equivalent. Higher vol; tighten the consec-bearish lookback. |
| **/RTY (or /M2K)** | E-mini / micro Russell 2000 | botnet101-3-bar-low + consec-bearish — IWM-equivalent. |
| **/CL** (light crude)   | Heating oil + energy macro    | botnet101-consec-below-ema (XOP-style mean reversion); needs longer lookback (10 bars) due to 24h sessions. |
| **/GC** (gold)          | Risk-off proxy                | botnet101-turn-of-month, botnet101-turn-around-tuesday — both already trade GDX successfully. |

**No translation viable for:** ORB strategies (futures session is
continuous, no opening range to define); SMC (too instrument-specific).

**Sizing translation.** Equity strategies size by USD notional. Futures
must size by contracts, where 1 contract ≠ 1 share. The notional ratio:

| Contract | Tick value | Notional at typical price |
|----------|------------|---------------------------|
| /ES      | $12.50 / tick (0.25 idx pts) | $26.5k @ 5300 |
| /MES     | $1.25 / tick                 | $2.65k @ 5300 |
| /NQ      | $5.00 / tick (0.25 pts)      | $42k @ 21000  |
| /MNQ     | $0.50 / tick                 | $4.2k @ 21000 |
| /CL      | $10 / tick (0.01 $/bbl)      | $80k @ 80     |
| /MCL     | $1 / tick                    | $8k @ 80      |

**Recommendation:** start exclusively on micro contracts (/MES, /MNQ,
/M2K, /MCL, /MGC) so even a maximally-wrong entry is bounded at
$2.5k–$8k notional with ~$50 initial margin per contract.

---

## 5. Tax treatment (1256)

Futures get **full 1256 treatment**: 60% long-term capital gain rate,
40% short-term, regardless of hold period. For a US retail trader in
the 32% short-term / 15% long-term brackets, that's an effective
~21.8% blended rate vs ~32% pure short-term — a **~30% tax savings**
on every futures profit dollar vs equity day-trading.

For a strategy generating $50k/yr in profits, that's $5k+/yr in
saved taxes alone — which is most of the data subscription cost.

**Caveat:** 1256 contracts mark-to-market at year-end whether or not
positions are closed. The auto-trader doesn't currently have to model
year-end MtM; futures path would need that for tax-export accuracy
(extends 3.5.4 — tax export — by a meaningful amount).

---

## 6. Operational + risk concerns

| Concern                          | Notes |
|----------------------------------|-------|
| Funded-account separation        | If we open Tradovate, it MUST be a separate account from Alpaca paper. Position reconciliation (3.1.4) needs a futures-broker adapter. |
| Multi-broker auto_trader routing | Adds significant complexity. Each strategy needs a `broker_id` like the existing `live_strategies` carve-out. Wire through `monitoring.accounts` (3.2.4). |
| Settlement vs cash               | Most index futures cash-settle. Energy/metals can be physically-settled if held past expiry. Force exit by 5 days before contract expiry — every contract. |
| Rollover                         | Front-month rolls quarterly. The validator's backtest path needs to handle continuous-contract construction or it'll see giant gaps at every roll date. |
| Margin call response             | Need a runtime watcher: if margin utilization > X%, trip kill switch BEFORE the broker auto-liquidates. |
| Data subscription churn          | Exchange data fees are billed per-month, no refunds. Cancelling mid-month means you keep paying until period-end. Factor into go/no-go. |

---

## 7. Recommended go / no-go criteria for the implementation milestone

Open an implementation milestone (Phase 4 candidate) only when **all**
of these hold:

1. Equity paper trading has ≥ 90 days of clean operation post-Phase 3
   ship.
2. At least 3 tracked strategies have ≥ 100 closed paper outcomes
   each AND consistent positive Sharpe (>0.20) AND mean per-trade
   return ≥ 1.0%.
3. Trader is comfortable losing the entire data-subscription cost
   ($120–$150/mo × 6 months = ~$900) without strategy edge to justify
   it. Treat the first 6 months as **paid R&D** — not P&L.
4. Tradovate demo account opened + reviewed; API token flow tested.
5. Micro-contract-only constraint codified in settings + auto_trader
   (no /ES, only /MES, etc.) for the first 90 days of live futures.
6. Tax export (3.5.4) extended to handle 1256 MtM at year-end.

Failure of any → NO-GO; revisit in 30 days.

**Soft requirement:** if Alpaca adds futures support before our
Phase 4 starts, default to Alpaca for unified account experience.
Watch their roadmap.

---

## 8. What this milestone explicitly DOES NOT do

- No code is written.
- No broker accounts opened.
- No data subscriptions purchased.
- No futures module scaffolded.
- No tests added.

It's a decision document. Re-read before opening the implementation ticket.

---

_Authored: 2026-05-16 by milestone-builder for Phase 3.4.3._
