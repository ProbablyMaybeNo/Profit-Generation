# Markets Expansion Research — Crypto / Futures / Forex / Asian, for a PT-based trader

**Created:** 2026-06-19 · **For:** Ross (Los Angeles / Pacific Time) · **System:** Profit Generation (Alpaca, paper)
**Inputs:** 5-thread research sweep (Sonnet) — Alpaca-crypto ops, crypto strategy edge, futures, forex+Asian, multi-asset practitioner ops. Sources inline.

> **Read this first.** Our entire risk engine (Stage 0.2 + 1.3) is built on a **protective stop that RESTS on the broker's book** + a **scheduled-task (US-RTH) runtime**. That single fact reorders the "obvious" answer below.

---

## TL;DR — the decision

1. **The counterintuitive headline:** **Alpaca crypto does NOT support stop, trailing-stop, or bracket orders — only market / limit / stop-limit** (verified vs Alpaca docs). So the market that's *easiest broker-wise* (already integrated, scaffolding exists) is the *worst architectural fit* for our stop-based engine, and it needs a 24/7 daemon (our system is scheduled-tasks) and has the worst tax. Crypto isn't "the easy win" the existing scaffolding implied.
2. **The markets that PRESERVE our architecture** (native resting/trailing stops via API) are **Forex (OANDA)** and **Futures (IBKR/Tradovate)** — both need a new broker, but both keep the engine intact.
3. **Best for *you* to watch & pattern-spot RIGHT NOW, zero build:** crypto on TradingView (24/7). Satisfies the "follow along myself" goal today without touching the system.
4. **My recommendation:** don't fragment focus while the equity edge turns on (Monday → grace-admit). Watch crypto manually now; pick the *automated* expansion after — **OANDA forex** for the cleanest engine-transfer, or **CME micro futures via IBKR** for the best tax + edge + an evening-watchable instrument (Micro Nikkei). Full reasoning below.

---

## 1. The critical constraint: resting stops + runtime model

We spent **Stage 0.2** making a protective stop actually rest on the book (0/409 → resting) and **Stage 1.3** adopting a Chandelier(22,3.0) trail floored on that stop. The engine assumes:
- a **resting stop order** the broker holds, and
- a **scheduled-task** runtime (US-RTH), with nightly reset + EOD flatten.

Any new market is graded first on whether it preserves those two things.

| Market | Resting stop via API? | Runtime fit | 
|---|---|---|
| **Alpaca crypto** | ❌ **stop-limit only** (no stop-market, no trailing, no bracket) | ❌ needs 24/7 daemon |
| **Forex (OANDA)** | ✅ **native server-side trailing stop** (`trailingStopLossOnFill`) | ⚠️ 24/5; our session model adapts |
| **Futures (IBKR/Tradovate)** | ✅ native stop / trailing / bracket via API | ✅ near-24h but EOD-flatten model fits cleanly |

---

## 2. Per-market analysis (PT-centric)

### A. Crypto — via Alpaca (already scaffolded)
- **Hours:** 24/7 — unbeatable for *watchability* on your schedule. Our `monitoring/crypto_adapter.py` already has symbol detection, BTC-USD→BTC/USD normalization, a 24/7 bar loader, sizing override, and a `register_crypto.bat` scheduler (built Phase 3.4.1, dormant).
- **Order types (the blocker):** market / limit / **stop-limit** only. **No stop-market, no trailing-stop, no bracket.** Our resting-stop design can't port directly — we'd emulate the trail in software and rest a **stop-limit** (risk: the limit leg doesn't fill in a fast move → exposure past the stop). The existing scaffolding uses *market* orders and predates this analysis — it would need a stop-limit + software-trail layer.
- **Fees:** 0.15% maker / 0.25% taker (≤$100k tier) → ~0.50% round-trip taker. Meaningful drag on small R at 0.75% risk.
- **Edge (strong for trend):** crypto trend-following is well-documented — AdaptiveTrend (150+ perps, 2021–24) Sharpe ~2.4 *on perps with long/short*; spot-only long-biased realistic Sharpe ~0.9–1.4. EMA + **Chandelier(22,3.0)** on BTC daily: PF 1.61 vs 1.28 fixed-trail. **Adaptation needed: drop ATR period 22→10–14** (crypto regimes compress); keep ~3.0×. Cross-sectional momentum is academically strong but needs 50–100 liquid coins. Mean-reversion + ORB are weak/over-fit in crypto; funding-rate basis is a real but separate (perp) edge.
- **Candle patterns:** weak standalone in crypto (one study: ~5% predictive); defensible only as a *continuation filter on top of a trend* — same as equities.
- **Tax (worst of the three):** property, 37% top, **per-wallet cost-basis tracking required (2025+)**, Form 8949 burden; wash-sale currently N/A (could change). Crypto-tax software effectively mandatory.
- **Ops reality:** 24/7 = a persistent VPS daemon, websocket heartbeat + reconnect, one-websocket-per-account limit (may need a separate sub-account from the equity stream), no nightly reset, weekend-gap risk, exchange/counterparty risk. $154B in 2025 crypto liquidations — survivors ran spot-only/low-leverage, BTC/ETH only, hard DD kill-switch.
- **Verdict:** best *watchability*, **worst fit for our stop-based engine**, worst tax, most new infra (24/7). Doable as an isolated stop-limit daemon — not the freebie the scaffolding suggested.

### B. Futures — CME micros via IBKR / Tradovate (new broker)
- **Hours (PT):** Globex ~23h, Sun 3pm – Fri 2pm PT. Index RTH **6:30am–1:15pm PT** (peak liquidity, your morning). Maintenance break 2–3pm PT. Gold RTH closes 10:30am PT, crude 12:30pm PT. EOD-flatten sidesteps overnight margin entirely — fits our model.
- **Instruments:** MES (S&P, $1.25/tick), MNQ (Nasdaq, $0.50), MYM (Dow), M2K (Russell), MGC (gold), MCL (crude). Day-trade margins ~$50–$200/contract; **no PDT rule**. Clean ATR sizing at ≥~$15–25k account (lumpy below).
- **Order types:** ✅ IBKR (`ib_async`) and Tradovate both support resting stop / trailing-stop / bracket via API. Chandelier still computed in software + cancel/replace (same as today).
- **Edge (strongest documented):** time-series momentum / trend-following on index+commodity futures is *the* canonical CTA edge (Moskowitz 2012; "Two Centuries of Trend Following"). Our Chandelier trail + EOD-flatten + RTH candle entries map directly.
- **Tax (best):** **§1256 60/40 → ~26.8% blended max** vs 37%; mark-to-market 1099-B (no per-trade bookkeeping hell); 3-yr loss carryback. On $100k gains, ~$10k federal saving vs equities/crypto.
- **Integration lift (highest):** new broker (IBKR Gateway persistent process, or Tradovate REST/WS), **point-value sizing** (not share notional), **quarterly rollover automation**, data fee (~$5–10/mo). ~3–4 week build. IBKR = deepest API (`ib_async`), Tradovate = cleanest REST/WS + cheapest ($0.09/contract) but $1k min + API add-on.
- **Verdict:** best tax + best edge + scheduled-task-compatible, but the biggest engineering lift (new broker + rollover + sizing rewrite).

### C. Forex — via OANDA (new broker, low lift)
- **Hours (PT):** 24/5. **London/NY overlap 5–9am PT** (peak liquidity, pre-day) + Asian session 5–11pm PT (thinner, JPY/AUD). Good pre-morning watch window.
- **Order types (best architectural match):** OANDA v20 REST has **native `trailingStopLossOnFill` + `stopLossOnFill` + take-profit** attached at order creation, server-managed — *closest to our current Alpaca workflow*, simpler than IBKR. Mature Python (`oandapyV20`).
- **Edge:** TS-momentum in FX majors is documented (Moskowitz; CTA backbone) but **regime-cyclical** (2023 hurt CTAs) — our evidence gate is the defense. Candle-continuation transfers best in the London/NY overlap; noisy in thin Asian hours.
- **Cost:** spread-only (~1.7 pip EUR/USD on OANDA) → on a 10-pip stop that's ~17% of risk budget. Needs wider stops / lower frequency than equities. IBKR FX spreads tighter (~0.6 all-in) but heavier API.
- **Tax:** spot FX is §988 ordinary by default (can elect §1256 for some) — middle.
- **Integration lift (moderate):** new broker SDK (OANDA REST is simple), pip-denominated ATR, currency-pair universe, 50:1 leverage cap (our vol-targeting stays well under). ~1–2 weeks.
- **Verdict:** the cleanest *engine-preserving* transfer (native trailing stops), good PT hours, moderate lift; spread drag is the main tax.

### D. Asian markets — CME Micro Nikkei (MNK) via IBKR (evening-watchable)
- **Hours (PT):** Tokyo cash ~5–11pm PT — **your evening, genuinely watchable after your day.** MNK trades on Globex nearly 23h so you can trade the Tokyo session live.
- **Instrument:** MNK — USD-denominated, $0.50 × index, ~$247 margin, **unrestricted for US retail** via IBKR. Trends well (2023 Japan bull run); ATR/Chandelier applies identically to an index.
- **Hang Seng (HSI):** US-person access is **fragile** (OFAC history; IBKR re-enabled but inconsistent) — prefer a US-listed ETF (EWH/FXI on Alpaca) over direct HKEX futures.
- **Verdict:** the standout "watch in your evening" play; rides on the same IBKR integration as futures (B), so it's a natural companion if you go the IBKR route.

---

## 3. Comparison matrix

| Dimension | Crypto (Alpaca) | Futures (IBKR/Tradovate) | Forex (OANDA) | Micro Nikkei (IBKR) |
|---|---|---|---|---|
| Watchable PT hours | **24/7** ✅✅ | RTH 6:30am–1:15pm + ~23h | overlap 5–9am, Asia eve | **Tokyo 5–11pm** ✅ |
| Resting/trailing stop via API | ❌ stop-limit only | ✅ native | ✅ **native** | ✅ native |
| Runtime fit (our scheduled model) | ❌ needs 24/7 daemon | ✅ (EOD-flatten) | ⚠️ 24/5, adapts | ✅ |
| Edge for trend + trailing | strong (decays w/ overfit) | **strongest documented** | good, cyclical | good |
| Tax (US) | worst (37%, per-wallet) | **best (§1256 ~26.8%)** | middle (§988/elect) | best (§1256) |
| Integration lift | low broker / **high stop+24-7** | **high** (broker+rollover+sizing) | **moderate** | high (shares IBKR) |
| New broker needed? | no | yes | yes | yes |

---

## 4. Recommendation & sequencing

**Guiding principle (unchanged):** the equity edge turns on this coming session (Monday validation → grace-admit `botnet101-3-bar-low`). Don't fragment focus mid-ignition. Run any new market as a **parallel, isolated track** (separate account/keys/process/P&L) — never a shared capital pool.

**Step 1 — NOW, zero build:** start watching **crypto (BTC/ETH) on TradingView, 24/7**. It directly satisfies your "follow along myself + spot patterns" goal today, with no integration, and builds intuition we can backtest. The system can already push its candle-pattern/regime read to your Telegram for side-by-side.

**Step 2 — let the equity engine prove out** (Monday → grace-admit → accumulate ≥20 fresh honest closes → M12 graduates). This is days, not weeks.

**Step 3 — pick the automated expansion** based on what you want most:
- **Cleanest engine-transfer, least build → OANDA forex.** Native server-side trailing stops = our exact workflow; ~1–2 weeks; London/NY overlap is your pre-morning.
- **Best long-term (tax + edge) + evening watching → IBKR micro futures (+ Micro Nikkei).** §1256 tax, strongest trend edge, MNK Tokyo session in your evening; ~3–4 weeks (Gateway + rollover + point-value sizing).
- **Most watchable + on our existing broker → crypto**, but only as a deliberately-built **stop-limit + 24/7 daemon** sub-system (accept the stop-limit gap risk and VPS infra). Best if you fall in love with watching it.

**My pick if forced to one:** **OANDA forex** as the system's first new automated market (preserves everything we built, moderate lift, good PT hours), with **crypto as your manual watch-and-learn track now**. Revisit IBKR micro futures as the "serious" expansion once one new market is running — its tax+edge edge is real and worth the lift later.

---

## 5. Key caveats
- Alpaca-crypto order-type limits verified vs docs but re-confirm before building (Alpaca ships features).
- All trend-following edges are **regime-cyclical** — our evidence gate (M12) + drawdown ladder are the defense; expect 30–50% live-vs-backtest haircut, larger in crypto.
- Futures sizing is lumpy below ~$15–25k; micros mitigate but don't eliminate.
- Hang Seng direct access is politically fragile — prefer US-listed ETFs.
- Tax notes are directional, not advice — confirm §1256/§988 treatment with a professional.

## 6. Sources
See the inline URLs throughout (Alpaca docs on order types/fees/regions; SSRN/arXiv/JFQA on crypto trend + cross-sectional momentum; CME/IBKR/Tradovate/OANDA broker + order-type docs; §1256 / crypto-tax references; FTI/AInvest on 2025 crypto liquidations). Full link set in the 5 research threads behind this synthesis.
