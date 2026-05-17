# Crypto Leverage / Margin Research

**Status:** research milestone (Phase 4.2.1). **No code shipped.** This
document is the go/no-go gate for milestone 4.2.2 (leverage-aware
sizing). Re-read this before opening that ticket.

**Scope:** evaluating whether to extend the existing Alpaca spot-crypto
adapter (Phase 3.4.1) with leveraged / margined crypto positions. The
question is whether leverage produces enough net edge after funding,
liquidation, and tax friction to justify the additional risk surface
and operational complexity.

---

## 1. What Alpaca actually offers (the floor)

As of 2026-05 — confirm before implementation, Alpaca's crypto
offering has moved twice already:

| Product                       | Status on Alpaca |
|-------------------------------|------------------|
| **Spot crypto (cash account)**| **Live** — what 3.4.1 already trades. BTC/USD, ETH/USD, SOL/USD, no leverage, instant settlement. |
| **Crypto margin / leverage**  | **Not offered.** Alpaca's crypto product is cash-only. They explicitly do not support margined crypto positions. |
| **Crypto perpetual futures**  | **Not offered.** No perp / inverse / coin-margined futures. |
| **Crypto on Alpaca via partner brokers** | **Not offered.** No upstream broker proxy. |

**Implication:** if leverage is desired, the venue is NOT Alpaca. Any
implementation forces a second broker integration onto the codebase.
The first-order question becomes "is leverage worth a second broker?"
not "how do we add leverage on Alpaca?"

This single fact dominates the decision. The rest of this document
assumes "if we did add leverage, where would it go and what would it
cost?"

---

## 2. Venue options (if we leave Alpaca)

| Venue              | Leverage offered                 | API quality                      | US user gating                  | Funding rate cadence | Notes |
|--------------------|----------------------------------|----------------------------------|---------------------------------|----------------------|-------|
| **Coinbase (Advanced Trade)** | Up to 5× on perpetual futures (BTC, ETH, others). US users gated to **eligible jurisdictions** as of 2025-Q4 launch. | REST + WebSocket; well-documented; OAuth. Quality on par with Alpaca's. | Live in most US states post-2025 launch, but Hawaii / NY ad infinitum. Always verify per-state. | 1-hour funding. | Best US-on-shore option for leverage. CFTC-regulated subsidiary. |
| **Kraken Futures (CF Benchmarks)** | Up to 5× on multi-collateral perps; up to 50× on BTC perp on the non-US offering only. | REST + WebSocket. Good docs. | **US users routed to a separate Kraken Futures US offering with reduced leverage caps (max 5×).** No NY, WA, MI. | 8-hour funding. | Solid alternative to Coinbase. Same 5× ceiling in the US. |
| **Binance.US**     | Spot only — no futures on the US arm. | REST + WebSocket. Lower volume than .com. | Most states ex NY / HI / etc. | n/a (spot) | Doesn't help us — defeats the purpose. |
| **dYdX v4 (decentralised)** | Up to 20× perps on a Cosmos-app-chain. | gRPC + REST; complex onboarding (wallet, gas, etc.). | **US users blocked at the frontend** post-v4. Self-hosted dApp access works but is grey-area. | 1-hour funding. | Best leverage ceiling but unconscionable regulatory risk for a self-employed US sole-prop. **Skip.** |
| **GMX / Hyperliquid** | Similar to dYdX (decentralised perps). | RPC-style; even more onboarding overhead. | Same as dYdX. | Variable. | Skip for the same reason. |

**Practical pick if we proceed:** **Coinbase Advanced Trade** for the
on-shore + CFTC-regulated angle, capped at the 5× they offer. The
leverage ceiling matters less than the regulatory clarity.

---

## 3. Maintenance margin and liquidation mechanics

For Coinbase perps at 5×:

- **Initial margin:** 20% of notional (= 1/5). For a $1000 BTC perp,
  $200 initial margin is locked.
- **Maintenance margin:** 10% of notional. For the same $1000 perp,
  if the account equity falls below $100 of margin (a 50% loss on
  the locked $200), the position is liquidated.
- **Liquidation price** (long): `entry × (1 − (init_margin − maint_margin) / position_size)`
  ≈ entry × (1 − 0.10) = entry × 0.90 for 5× long. **A 10% adverse
  BTC move wipes the position.**
- **Liquidation fee:** 0.5% of notional, taken from remaining margin.
  On a $1000 perp, that's an additional $5 penalty on top of the
  90% loss already taken.

For comparison, Kraken Futures US is identical — 5× max, 10% maint
margin, ~10% adverse move = liquidation.

**Translation:** at 5× leverage, BTC's typical 1-day std-dev of
2-3% means a single bad day kills a position in ~3-4 standard
deviation territory. NOT close to safe.

---

## 4. Funding rates (the slow tax)

Perps are not free. Each funding period the long side pays the short
side (or vice versa) a small percentage based on the perp-vs-spot
basis:

- **Coinbase 1h funding:** typical magnitude 0.005% – 0.020% per
  hour. Annualized **44% – 175%**. (When BTC futures premium spikes,
  hourly funding can spike to 0.10% briefly.)
- **Kraken 8h funding:** typical magnitude 0.01% – 0.05% per 8h.
  Annualized **11% – 55%**.

For our mean-reversion strategies that hold for 3-5 days, funding
costs alone consume **3% – 15%** of the perp's notional just to
maintain the position. A 5× leveraged 3-day hold therefore needs
the underlying spot move to be > the funding spend BEFORE producing
any net edge.

Our paper-side baseline mean per-trade return is ~0.3% on spot. At
5× leverage that becomes ~1.5% gross — but funding for 3 days at
median rates eats ~1% of that. Net edge after funding: **~0.5% per
trade**. Better than spot's 0.3% — but only by 67%, with 10×
greater liquidation risk on the downside.

---

## 5. Which strategies actually benefit

The math above only works if the strategy has a positive expected
move large enough to clear the funding spend. Per-strategy analysis:

| Strategy class                  | Avg holding period | Edge per trade (spot) | Edge per trade (5× perp, post-funding) | Benefits from leverage? |
|---------------------------------|--------------------|-----------------------|----------------------------------------|------------------------|
| Mean-reversion (RSI2, consec-bearish) | 3–5 bars | +0.3% to +0.6% | +0.5% to +2.0% | **Marginal.** Funding eats ~1% over the hold. Below the 2% gross threshold, perp underperforms. |
| Trend-following (Donchian breakout, MA cross) | 10–40 bars | +1.0% to +3.0% per winner; long holding | +5% to +15% per winner; funding eats 4-15% over the hold | **Almost certainly negative.** Long holding periods are the WORST case for funding — multi-week perps bleed 10%+ to funding alone, regardless of direction. |
| Momentum scalps (intraday breakout) | < 1 day | +0.1% to +0.4% | +0.5% to +2.0% (funding negligible at < 1h hold) | **Yes** — but we don't have any of these strategies built. Would be a separate Phase 5 milestone. |
| Slippage-heavy strategies (low volume coins) | n/a | Already losing to spread | Loses 5× faster | **Strongly negative.** Slippage scales linearly with leverage. |

**Bottom line:** for the strategies we ACTUALLY trade today, leverage
either marginally helps (mean reversion) or is strictly negative
(trend following). The only clear winner — intraday scalps — is
not in our roster.

---

## 6. Regulatory and tax angle

### Regulatory
- **Coinbase US perps:** CFTC-regulated. State-by-state availability.
  No registration required for the user; KYC handled at account
  opening.
- **Kraken Futures US:** Same — CFTC. State exclusions apply.
- **Tax form:** Coinbase and Kraken both issue 1099-MISC and
  1099-B for the 2026 tax year. No special form for perps as of
  2026-05; CFTC and IRS guidance is still evolving.

### Tax
- **Spot crypto** (what we trade today): straight short-term cap
  gains on every closed position. Ross's existing 8949 export
  (3.5.4) handles this.
- **Perpetual futures:** treated as cap gains in most filings, but
  the IRS has NOT issued perp-specific guidance. Some filers treat
  perps as 1256 contracts (60/40 long/short blended), which would
  be a tax win — but doing so without explicit IRS guidance invites
  audit risk. **Assume vanilla short-term cap gains; the tax win
  is theoretical.**
- **Liquidation events:** the forced-close that follows liquidation
  is a taxable event. Liquidation fees ARE deductible as a trading
  expense.

### Operational
- A second broker integration triples the credentials-management
  surface and doubles the reconciliation script complexity.
- Position sizing math gets messier because perp notional is
  decoupled from collateral.
- `monitoring.kill_switch` must learn to flatten BOTH brokers when
  engaged.

---

## 7. Go / no-go criteria

Recommended thresholds to gate the 4.2.2 implementation:

| Criterion                                              | Required for GO |
|--------------------------------------------------------|-----------------|
| At least one paper-traded crypto strategy with N ≥ 100 closed outcomes | Yes |
| That strategy's avg holding period < 24 hours          | Yes |
| That strategy's per-trade mean spot return > 1.0%      | Yes |
| Coinbase Advanced Trade perps available in Ross's state| Yes |
| Funding-rate-adjusted expected return on perps > 1.5× spot return | Yes |
| Liquidation distance > 3× the strategy's 95th-percentile MAE | Yes |

If ANY criterion fails, the recommendation is **NO-GO** and we keep
trading spot only.

---

## 8. Recommendation

**No-go for Phase 4.** Three reasons stacked:

1. **Alpaca doesn't offer it.** A second broker integration is a
   serious commitment that can't be justified by the math.
2. **Our current strategies don't benefit.** Mean-reversion gets only
   ~67% more edge post-funding for 10× more liquidation risk. Trend
   strategies (4.6) would actively lose money to funding. The
   strategy class that does benefit (intraday scalps) doesn't exist
   in the roster.
3. **Regulatory + tax overhead is non-trivial.** Two extra 1099s, a
   liquidation-aware reconciliation path, kill_switch must flatten
   both venues, plus state-by-state CFTC perp gating to track.

Revisit Phase 5 IF (a) Alpaca adds margined crypto natively, OR (b)
we build out an intraday-scalp strategy class with N ≥ 100 closed
paper outcomes and per-trade mean > 1.0%. Until then, **4.2.2 is
gated NO-GO and ships only the default `crypto.max_leverage=1.0`
fallback**.

---

## 9. References

- Coinbase Advanced Trade perpetual futures docs (verify URL at impl time)
- Kraken Futures US (CF Benchmarks) leverage limits
- CFTC guidance on US-onshore crypto perpetual futures
- IRS Publication 550 (capital gains; closest existing perp guidance)
- `docs/FUTURES_RESEARCH.md` — equity-futures research, same template
- `monitoring/crypto_adapter.py` — the existing spot path leverage
  would extend
