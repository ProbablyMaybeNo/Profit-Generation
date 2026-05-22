# Options-Based Synthetic Pyramiding — Feasibility Research

**Status:** Phase 6 research milestone (6.5.1). Deliverable per
`PHASE6_PLAN CURRENT.md:128-139`. No code, no implementation. This
document is the go/no-go input for whether Phase 7+ takes on options
pyramiding as a real workstream.

**Date:** 2026-05-21
**Author:** Claude Opus 4.7, drafted with Ross.
**Prior art:** `docs/OPTIONS_RESEARCH.md` (Phase 3.4.2) — narrower
question on options for long-only screening, not pyramiding-as-amplifier.

**Headline recommendation:** **NO-GO for Phase 7. Revisit when trend
strategies have ≥50 closed outcomes AND ≥5% of those exceed +50%
return.** Today's evidence does not yet support the asymmetric-payoff
case that options pyramiding requires. Specifics in §(d) and §(f).

---

## (a) Alpaca options endpoints and current API limits

Alpaca added US-listed options trading to its API in 2024 (paper +
live). The relevant endpoints are:

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /v2/options/contracts` | Discover contracts for a symbol — strikes, expirations, types, multipliers | API key |
| `GET /v1beta1/options/snapshots/{symbol}` | Bid/ask/last/Greeks/IV per contract | API key |
| `GET /v1beta1/options/quotes/latest` | Latest NBBO quote across contracts | API key |
| `GET /v1beta1/options/bars/{symbol}` | OHLCV bars (1m / 1h / 1d) on a contract | API key |
| `POST /v2/orders` (`asset_class=us_option`) | Submit option order — market / limit / stop, plus multi-leg `class=mleg` for spreads | API key |
| `DELETE /v2/orders/{order_id}` | Cancel | API key |
| `GET /v2/positions` | Includes option positions inline with stock positions (already used in `reconcile_positions.py`) | API key |

**Rate limits (paper):** 200 req/min on the trading API; 1000 req/min
on the market data API for paid tiers, 200 req/min on the free tier.
At our current activity (≤74 actions/day per `auto_trader`), neither
is a real constraint. If we crank LLM filter to 1m cadence in Phase 7,
option-chain pulls per pyramid candidate could push us toward the
1000/min ceiling — manageable with chain caching.

**Account requirements:** Alpaca options trading needs an approval
level (1-4 like the broader US options market). The paper account
auto-approves at level 2 (long calls/puts + cash-secured puts +
covered calls). Level 3 (debit/credit spreads) and Level 4 (uncovered
short options) require manual approval forms even on paper. The
candidate structures in §(c) all fit within level 2-3.

**What's missing:** no historical IV time series endpoint (we'd have
to record snapshots ourselves), no Greeks endpoint independent of the
snapshot (Greeks come bundled in the snapshot — fine in practice).

---

## (b) Data infrastructure delta vs current

Today's system models a position as `paper_trades(symbol, side, qty,
fill_price, status, …)`. An option position has more state. The delta:

| Concept | Current | Needed for options |
|---|---|---|
| Symbol resolution | Free-text equity tickers | OCC contract identifier (e.g. `AAPL250620C00200000`) — root + expiry + C/P + strike |
| Multiplier | Implicit `qty * price = notional` | Explicit ×100 (or ×10 for mini-options) — `notional = qty * price * multiplier` |
| Strike grid | n/a | Needed for bull-call-spread leg selection — chain pull at signal time |
| Expiry calendar | n/a | Track DTE per open contract; alert at 7 DTE, force-close at 1 DTE (theta cliff) |
| IV snapshot | n/a | Record per-contract IV at entry and at close — needed for vega P&L attribution |
| Greeks at entry | n/a | Delta, gamma, theta, vega per contract — for risk reporting |
| Exercise / assignment | n/a | Webhook handling for assignment events (cash-settled vs physical) |
| Tax lot tracking | LIFO is fine | Section 1256 vs 988 vs equity-option tax treatment matters — see §(e) |

**Schema additions** (sketch, not for implementation here):
- New table `option_contracts(occ_id, underlying, expiry, type, strike,
  multiplier, first_seen, last_seen)`.
- New table `option_quotes(occ_id, ts, bid, ask, last, iv, delta,
  gamma, theta, vega, open_interest, volume)` — append-only, indexed
  on `(occ_id, ts)`.
- `paper_trades` gains an `option_contract_id` foreign key.
- `outcomes` gains `iv_at_entry`, `iv_at_exit`, `realized_vega_pnl`.

**Engineering effort estimate** (gut feel, not committed): chain
ingest + schema = 2 days; snapshot writer + Greeks pipeline = 2 days;
contract lifecycle (expiry, assignment) = 3 days; tax-lot tracking
= 2 days. Call it **9 engineering days minimum**, before any
strategy actually trades an option. Multiple of that if we want
historical IV backfill (which any backtest would need).

---

## (c) Candidate structures — payoff and fit-to-purpose

The question is not "should we trade options" but "as a *pyramid
add-on tier* on a position the share-based system already owns, what
structure adds the most R per dollar of risk?" Three candidates:

### Long call (single leg)
```
P&L
 │            ╱
 │           ╱
 │──────────╱─── strike
 │          │
 │  -premium│
 └──────────┴────────── underlying price →
```
- **Risk:** capped at premium paid.
- **Upside:** linear above strike, unbounded.
- **Theta:** worst-in-class — burns ~1-2% of premium per day at 30
  DTE, more as expiry nears.
- **Use case:** strong directional conviction with a defined holding
  horizon. The pyramid-add scenario: trend strategy is mid-run, share
  position is at +20%, we want to add asymmetric exposure without
  committing more share-equivalent capital.
- **Verdict for our use:** *only* viable on trend strategies whose
  median hold > 30 days. See §(d).

### Bull call spread (debit spread)
```
P&L
 │       ┌──────────  (max profit = K2 - K1 - net debit)
 │      ╱│
 │     ╱ │
 │────╱──┼──── strike K1
 │   ╱   K2
 │  -net debit
 └───────────────────── underlying price →
```
- **Risk:** capped at net debit paid (long K1, short K2).
- **Upside:** capped at K2 − K1 − net debit.
- **Theta:** roughly half a long call — the short leg cancels much of
  the decay.
- **Use case:** moderate directional conviction with a defined target
  price. The pyramid-add scenario where we have a clear technical
  resistance (Donchian channel top, prior swing high) and don't need
  unbounded upside.
- **Verdict for our use:** *the most likely winning structure for our
  trend strategies* — capped upside aligns with the channel-exit
  exits we already use, halved theta makes the 20-40 DTE hold viable.

### Ratio call spread (1 long, 2 short)
- **Risk:** unbounded above the short strikes if uncovered (level 4
  approval required); bounded if we hold the underlying shares (which
  in the pyramid context we *do*).
- **Upside:** between K1 and K2 — caps quickly.
- **Theta:** can be neutral or positive (collect more premium than
  paid).
- **Use case:** range-bound continuation — we expect the trend to
  resume but not run past resistance R + small buffer.
- **Verdict for our use:** *no.* Adds complexity, requires level 4
  approval, and the ratio bets against our own thesis (we own shares
  because we expect a run, not a range).

**Recommended structure if we proceed:** **bull call spread, 30-45
DTE, K1 at-the-money, K2 at the strategy's natural exit target.**
Single structure for all trend pyramiding — keeps the implementation
narrow, the tests tractable, and the historical analysis clean.

---

## (d) Which 4.6 trend strategies actually benefit — empirical reality check

This is where the analysis runs into the limitation Ross would want
flagged. Today's outcomes table (queried 2026-05-21):

| Strategy | n_closed | n ≥ 5R (+5%) | n ≥ 10R (+10%) | max_return |
|---|---:|---:|---:|---:|
| trend-donchian-breakout-20 | 0 | — | — | — |
| trend-ma-cross-20-50 | 0 | — | — | — |
| trend-new-high-volume | 0 | — | — | — |
| breakout-donchian-retest-20 | 0 | — | — | — |
| breakout-donchian-retest-short-20 | 0 | — | — | — |
| botnet101-3-bar-low (MR) | 376 | 79 | 20 | +28.6% |
| botnet101-consec-below-ema (MR) | 557 | 39 | 18 | +80.2% |
| botnet101-4bar-momentum-reversal (MR) | 359 | 34 | 17 | +93.5% |
| botnet101-buy-5day-low (MR) | 242 | 10 | 0 | +8.5% |

**The reality:**

1. **The trend strategies haven't closed a single trade yet.** Paper
   deployment started 2026-05-18 — three days ago. Trend strategies
   are designed to hold for weeks. We have zero empirical data on the
   one thing this milestone needs to know: *do our trend strategies
   actually produce 5-10× R tail wins on a meaningful frequency?*
2. **The mean-reversion strategies do have tail wins** — the +80%
   and +93% maxes on consec-below-EMA and 4bar-reversal are real
   asymmetric outcomes. But mean-reversion strategies hold ≤5 bars
   typically. Options on a 5-bar hold are *pure theta decay* — even
   a +93% underlying move gets eaten by IV crush + theta on a 5-day
   option position. **Options don't help mean-reversion strategies.**
3. **Backtest data (not paper) would inform the trend analysis** —
   but no backtest output is checked into the repo, and a backtest
   designed for share P&L doesn't necessarily reproduce option P&L
   without a separate IV-aware simulation.

**Conclusion for §(d):** without trend-strategy outcome data, the
"which strategies benefit" question is unanswerable today. Best to
defer the question, not answer it speculatively.

---

## (e) Tax & regulatory delta from share pyramiding

**Tax treatment — equity options on US-listed names:**
- Short-term capital gains if held <1 year (same as shares).
- Wash sale rules *do* apply to options and *do* cross between
  shares and options of the same underlying — selling AAPL shares at
  a loss and buying AAPL calls within 30 days disallows the loss.
  Pyramiding-with-options creates wash sale exposure that
  pyramiding-with-shares does not.
- Section 1256 (60/40 long/short treatment) applies to **index**
  options (SPX, NDX, RUT) and broad-based ETF options in some
  rulings, but NOT to single-name equity options. The trend
  strategies (4.6.x) trade SPY/QQQ/IWM only — these are ETF options
  whose 1256 treatment is debated. *Don't assume 60/40; assume
  short-term until a tax professional says otherwise.*

**Regulatory:**
- PDT rule applies to options the same way it applies to stock —
  same-day open+close counts as a day trade. Our paper account is
  unlimited (>$25k), so this is currently a non-issue but worth
  remembering before any live flip.
- Options approval levels are broker-managed. Bull call spreads
  require Level 3 on most brokers; verify Alpaca's specific gate
  before assuming this is automatic.
- Pattern Day Trader resets to live limits on the live flip
  (Phase 4.1's bridge). Adding options trading raises the question
  of which day-trade counter applies — same counter or separate.

---

## (f) Recommended go/no-go criteria

**Hard prerequisites (must be true before reconsidering):**

1. **Trend-strategy outcome corpus.** At least **50 closed outcomes
   per trend strategy** (300+ total across the five trend strategies
   currently live). At paper-only rate, that's 3-6 months at current
   trade frequency. Estimate based on signals/day × close rate.
2. **Demonstrated tail wins.** Among those closed outcomes, **≥5%
   must exceed +50% return** (a proxy for the 5-10× R asymmetric tail
   that justifies the option premium). Without that, options dilute
   rather than amplify.
3. **Median hold ≥ 30 days.** Confirms the strategies hold long
   enough for theta to be tolerable rather than dominant.

**Decision criteria (if all 3 prerequisites met):**

| Question | GO threshold | NO-GO threshold |
|---|---|---|
| Hypothetical option-pyramid P&L on closed outcomes (re-simulated with bull-call-spread overlay) | ≥ +20% delta vs share-pyramid | < +5% delta or negative |
| Wash-sale frequency under option-pyramid scenario | < 10% of opportunities lost to wash-sale window | > 20% lost |
| Worst-case drawdown vs share-pyramid | < 1.5× share max-DD | > 2× share max-DD |
| Engineering cost vs estimated edge | < 30 days implementation for ≥ 20% PnL uplift | > 60 days for < 10% uplift |

**Recommended path forward:**

1. **Defer the decision.** Mark this milestone as "researched, deferred
   pending evidence." Set a calendar reminder for **2026-08-21** to
   re-query the outcomes table and check whether the 50-outcome /
   ≥5% tail-win prerequisites are met.
2. **Don't pre-build infrastructure.** The 9 engineering days listed
   in §(b) is real cost — burning it before we have evidence the edge
   exists is the kind of premature optimization that this system has
   so far deliberately avoided.
3. **Phase 7 slot freed.** With 6.5.1 deferred, the Phase 7 slot
   originally penciled for "options pyramiding implementation"
   (per Phase 6 notes) becomes available for whatever's next on the
   candidate list — likely the LLM filter overlay (`PHASE7_PLAN
   DRAFT.md:7.1`) or websocket fills (`PHASE7_PLAN DRAFT.md:7.5`).

**One scenario that flips this immediately to GO:** if a single trend
strategy demonstrates a +100% or larger winner within the next 60
days, the asymmetric-payoff case becomes hard to ignore, and a narrow
implementation (bull-call-spread overlay on just that one strategy)
becomes worth the engineering cost. Watch for the outlier rather
than the average.

---

## Cross-references

- `PHASE6_PLAN CURRENT.md:128-139` — original milestone spec.
- `docs/OPTIONS_RESEARCH.md` (3.4.2) — earlier options research scope
  (screening, not pyramiding).
- `monitoring/pyramiding.py` — the share-based pyramid implementation
  this would augment, not replace.
- `data/db.py:160` — `paper_trades` schema, the table that would gain
  the `option_contract_id` foreign key.
