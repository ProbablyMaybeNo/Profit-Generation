# Options Research (long-only)

**Status:** research milestone (Phase 3.4.2). **No code shipped.** This
document is the go/no-go gate for an eventual options-implementation
milestone. Re-read this before opening that ticket.

**Scope:** evaluating whether the system should extend beyond cash
equities and crypto into long-only options (long calls and long puts).
Short options (covered calls, credit spreads, iron condors, naked puts)
are explicitly out of scope for this round — assignment risk + margin
mechanics + unlimited-loss profiles change the operational model too
much to ride alongside the current paper-equity infrastructure.

---

## 1. Broker API options (Alpaca-first)

Alpaca added equity-options trading in 2024 (`alpaca-py` ≥ 0.20). The
endpoints we'd need:

| Need                              | Alpaca endpoint                                          |
|-----------------------------------|----------------------------------------------------------|
| Discover the option chain         | `GET /v1beta1/options/snapshots/{underlying}`            |
| Latest quote for a single contract| `GET /v1beta1/options/quotes/latest?symbols=...`         |
| Historical option bars            | `GET /v1beta1/options/bars`                              |
| Submit option order               | `POST /v2/orders` with `asset_class="us_option"`         |
| List option positions             | `GET /v2/positions` (mixes equities + options)           |
| List option contracts             | `GET /v2/options/contracts?underlying_symbols=...`       |

**Account requirements.** Alpaca options requires:
- Approval level 1 (long calls/puts) — basic agreement, no margin
  required for cash-secured longs.
- Approval level 2+ unlocks spreads / cash-secured puts / covered calls
  — out of scope until level 1 has 90+ days of clean activity.

**Symbol format.** OCC option symbols: `SPY240920C00450000` =
underlying + YYMMDD expiry + C/P + 8-digit strike-in-cents. We'd need
a builder/parser module (`monitoring/option_symbols.py`) and a
`positions` reconciler extension to recognise option symbols and not
mistake them for delisted equities.

---

## 2. Data sources for IV / Greeks

Alpaca's options endpoint returns last-trade + quote but does NOT
return Greeks or IV out of the box. Three viable routes:

1. **Compute locally from quotes** — feed bid/ask mid + risk-free rate
   into Black-Scholes. `py_vollib` does this in <100 LOC; adds zero
   external service cost. Trade-off: needs accurate dividend yield +
   risk-free-rate inputs, which drift.

2. **Polygon options snapshot** — `/v3/snapshot/options/{underlying}`
   ships IV + Greeks per contract. Already on the project's Polygon
   $29/mo plan; no incremental cost. Risk: rate limit (5 req/min on
   that plan tier) makes scanning the full chain slow.

3. **CBOE Live Data** — gold standard, ~$80/mo + per-exchange fees.
   Out of scope unless we're trading options materially.

**Recommendation:** start with Polygon snapshots cached locally;
fall back to py_vollib for off-hours / cache misses.

---

## 3. Strategy translation (long-only options)

Cataloguing each currently-tracked strategy by how cleanly it
translates to long calls/puts vs whether it needs restructuring:

| Strategy                              | Direct translation?                                     |
|---------------------------------------|---------------------------------------------------------|
| `botnet101-3-bar-low`                 | YES — long-only mean-reversion → buy ATM-to-slight-ITM call on entry, exit on signal exit. Add a hard stop at 50% premium loss. |
| `botnet101-buy-5day-low`              | YES — same shape.                                       |
| `botnet101-consec-bearish`            | YES — same shape.                                       |
| `botnet101-4bar-momentum-reversal`    | YES — same shape.                                       |
| `botnet101-consec-below-ema`          | YES — same shape.                                       |
| `botnet101-turn-around-tuesday`       | YES — but small expected move + 5-day hold = theta
                                              decay risk; needs ≥ 30 DTE selection. |
| `botnet101-turn-of-month`             | YES — same theta caveat.                                |
| `rsi2-oversold` (generated)           | YES — short-horizon entry; theta is the killer; pick
                                              weekly options with delta ≥ 0.55. |
| ORB / ORB-pivots                      | RESTRUCTURE — intraday hold; options bid/ask spread
                                              eats the edge. SKIP for v1.                |
| SMC                                   | RESTRUCTURE — complex multi-leg setups; not long-only.   |

**Rule of thumb for v1:** only port strategies whose mean hold is
≥ 3 trading days AND whose mean per-trade return ≥ 1.0% on the
underlying (so a 50-delta option's percentage move covers the
spread). This filters us down to the botnet101 cluster + rsi2-oversold.

---

## 4. Regulatory + tax implications

**Account-level:**
- Pattern-Day-Trader (PDT) rule applies to options the same as
  equities. $25k account minimum before unlimited intraday round-trips.
- Options Disclosure Document (ODD) must be signed before approval.
- Alpaca passes through OCC fees (~$0.50/contract).

**Tax (US, retail):**
- Equity options: short-term capital gains (ordinary income) if held
  < 365 days; long-term if held longer. Most strategies above will be
  short-term.
- Index options (SPX, NDX, RUT, VIX) get **1256 treatment**: 60% long-
  term + 40% short-term regardless of hold period. Materially better
  tax efficiency if we trade those instead of SPY/QQQ/IWM ETF options.
  Translation cost: build the option-symbol parser for SPX-style
  formats and respect the cash-settlement (no shares delivered).
- Wash-sale rule applies across equity ↔ option boundaries
  ("substantially identical"). The auto-trader would need a
  wash-sale guard before any tax loss is taken.
- Form 8949 export (planned 3.5.4) must split short-term, long-term,
  AND 1256 buckets. Different lines on the form.

**Recommendation:** prefer SPX/NDX/RUT index options over SPY/QQQ/IWM
ETF options when the underlying universe permits, for 1256 tax
treatment.

---

## 5. Operational considerations

| Concern                         | Notes                                                    |
|---------------------------------|----------------------------------------------------------|
| Liquidity                       | Only trade contracts with bid-ask spread < 5% of mid AND open interest > 100. Wire a pre-trade liquidity check into `_process_entry`. |
| Assignment / exercise           | Long options don't get assigned. Auto-exercise rule = ITM ≥ $0.01 at expiry → assignment. Always close 1-2 days before expiry to avoid surprise share delivery. |
| Pin risk                        | Closing exactly at expiry is dangerous (price pin at strike). Force exit at 1 DTE. |
| Slippage tracking               | The existing `edge_diff` widget (2.2.5) needs an options branch — option fills are dramatically worse vs mid than equity fills. |
| Position sizing                 | Notional ≠ risk for options. Size by max-loss = premium × 100. Add `option.max_premium_usd` setting (suggest $200 to start). |
| Earnings veto                   | Already implemented for equities (3.x). Even more important for options — IV crush post-earnings. |

---

## 6. Recommended go / no-go criteria for the implementation milestone

Open an implementation milestone (Phase 4 candidate) only when **all**
of these hold:

1. Equity paper trading has ≥ 60 days of clean operation post-Phase 3
   ship (i.e. roughly 2026-07-15 at earliest).
2. At least 3 tracked strategies have ≥ 50 closed paper outcomes each
   AND mean return ≥ 1.0% per trade (so options translation has real
   edge to amplify).
3. Account has been approved for Alpaca options level 1.
4. ODD signed; tax export (3.5.4) extended to handle 1256 split.
5. A *separate* live account is funded for options — never share the
   equity-paper account with options-live until both have track records.
6. A scripted dry-run + paper-trading mode is shipped FIRST and watched
   for 30 days before any live option order.

Failure to meet any criterion = NO-GO; revisit in 30 days.

---

## 7. What this milestone explicitly DOES NOT do

- No code is written.
- No Alpaca options approval requested.
- No tax forms filed.
- No options module scaffolded.
- No tests added.

It's a decision document. Re-read before opening the implementation ticket.

---

_Authored: 2026-05-16 by milestone-builder for Phase 3.4.2._
