# Profit Generation — Phase 6 Plan (DRAFT)

> **⚠️ DRAFT.** Rename to `PHASE6_PLAN CURRENT.md` before running
> `/next-milestone` against it. The milestone-builder agent searches for
> the `CURRENT.md` suffix — keeping this file as DRAFT prevents
> accidental autonomous execution before Ross reviews and refines.

Same conventions as Phase 2 / 3 / 4 / 5 / 5.5:
- Python interpreter: `py -3.13` for unit tests / scripts. Conda env
  `trading` (Python 3.11) for anything that imports yfinance / alpaca-py.
- Test command: `py -3.13 -m pytest tests/<file>.py` (skip live API tests)
- Commit style: conventional commits with the standard `Co-Authored-By`
  footer.
- Branch: push directly to `main`.
- Never modify `config/credentials.json`, `data/*.db`, `logs/`.

**Phase 6 theme:** Phase 4 added the trend-following half of the system
(4.6) and walked carefully across the paper→live bridge (4.1). Phase 5
added intraday capability. Phase 5.5 added the trend scanner. Phase 6
upgrades the *risk and entry surface* with five techniques Ross flagged:
ATR-based stops generalized beyond trend, fractional Kelly sizing, a
breakout-and-retest strategy class, a Parabolic SAR exit overlay, and a
research milestone for options-based synthetic pyramiding. The order is
deliberately ROI-weighted — ATR and Kelly first because they augment
infrastructure that already exists; breakout and SAR next as new strategy
surfaces; options last as research-only because the infra cost (chain
data, IV, Greeks, expiry rolls) doesn't yet justify the edge.

---

## 6.1 ATR stops generalized across all strategies

Phase 4.6.1 ships `atr_trail` as one of three trailing-stop methods on
trend strategies. Phase 5 generalizes that infrastructure so *every*
strategy benefits — including mean-reversion, which currently uses fixed
% stops that get whipsawed in volatile regimes.

- [x] **6.1.1 ATR-based initial stops**
  - **Deliverable:** `monitoring/sizing.py` extended with `atr_initial_stop()` + integration into auto_trader entry path
  - **Acceptance:** at entry time, stop is set at `entry_price − (k × ATR_14)` for longs (mirror for shorts). `k` configurable per strategy via new `stops.atr_multiplier` setting (default 2.5). Falls back to the existing fixed-% stop if ATR can't be computed (e.g. <14 bars of history). New `entry_stops` column on `paper_trades` records which method was used. Tests: ATR math, fallback path, per-strategy multiplier override, short-direction handling.
  - **Completed:** 2026-05-19 by milestone-builder · commit 1cffdff

- [x] **6.1.2 ATR stops as default for mean-reversion strategies**
  - **Deliverable:** strategy declarations updated + tests
  - **Acceptance:** every mean-reversion strategy in `TRACKED_STRATEGIES` (RSI2 oversold, consec-bearish bounces, etc.) flips its stop method from `fixed_percent` to `atr_initial` (6.1.1) with `k=2.0` (tighter than trend's 2.5 because MR exits are quicker). Existing live/paper segregation (3.1.5) preserved — only paper-active strategies flip first; live strategies stay on existing stops until separately validated. Tests: declaration updates, no regression in existing strategy test suite.
  - **Notes:** This is the milestone where ATR stops actually start *making money* for us — trend strategies (4.6) already had ATR from the start, but the bulk of the roster is mean-reversion.
  - **Completed:** 2026-05-19 by milestone-builder · commit c45c5f0

- [ ] **6.1.3 Regime-aware ATR multiplier**
  - **Deliverable:** `monitoring/regime_router.py` extended + sizing integration
  - **Acceptance:** `k` is no longer a fixed per-strategy constant. It scales by current regime classifier output: `k_effective = k_base × regime_multiplier` where regime_multiplier is `1.25` in high-vol regimes and `0.85` in low-vol regimes (so stops widen in chop, tighten in calm). Multiplier capped to `[0.7, 1.5]` to prevent extreme stops. Tests: multiplier math, cap enforcement, default-1.0 when classifier confidence < 0.6.

---

## 6.2 Fractional Kelly position sizing

Tiered sizing (3.2.1) is a fine starting heuristic, but it ignores each
strategy's measured edge. Kelly turns the position size into a
mathematical function of the strategy's own win-rate history. Full Kelly
is famously brutal on noisy estimates — fractional Kelly (1/4 of full)
plus a min-sample-size guard is what professional shops actually use.

- [ ] **6.2.1 Per-strategy Kelly calculator**
  - **Deliverable:** `monitoring/kelly.py` + `tests/test_kelly.py`
  - **Acceptance:** `calc_kelly_fraction(strategy_name)` queries `paper_trades` for that strategy's closed outcomes, computes `p = wins / total`, `b = mean_winner / abs(mean_loser)`, returns `f* = (p*(b+1) - 1) / b`. Returns `None` if fewer than 50 closed outcomes (sample-size guard). Returns `0` if `f*` is negative (negative-edge strategy — should not be sized at all). Caps raw `f*` at `0.25` (no single strategy claims more than a quarter of portfolio even if math says it should). Tests: math correctness, sample guard, negative-edge handling, cap enforcement.

- [ ] **6.2.2 Fractional Kelly sizing tier**
  - **Deliverable:** `monitoring/sizing.py` new sizing tier `kelly_quarter`
  - **Acceptance:** new tier slots into existing tiered-sizing chain. When a strategy has Kelly guard satisfied (5.2.1 returns non-None), size = `portfolio_value × min(0.25 × kelly_fraction, per_strategy_max)`. When guard fails, falls back to current tier (3.2.1) — Kelly is opt-in via `sizing.method = kelly_quarter` per strategy. Per-strategy max-fraction setting (default 0.05) hard-caps any single position. Tests: fallback path, math against known-good inputs, max-fraction cap, fraction-of-Kelly multiplier (default 0.25, configurable for ¼ / ½ / full).
  - **Notes:** Default to ¼ Kelly. Ross can flip to ½ Kelly per-strategy after a strategy has 200+ closed outcomes — but never full Kelly. The acceptance test enforces a hard ceiling at 0.5 (½ Kelly) in code.

- [ ] **6.2.3 Kelly dashboard card**
  - **Deliverable:** `dashboard/index.html` + dashboard API extension
  - **Acceptance:** new card on dashboard shows current Kelly fraction per strategy alongside its closed-trade count and "guard status" (✓ qualifying / ✗ need N more trades). Refreshes hourly. Tests: API shape, UI render against fixture data.

---

## 6.3 Breakout-and-retest strategy

Adds a new strategy class to the roster. Trend strategies (4.6) buy on
the breakout; this one waits for the breakout AND a retest of the
broken level, sacrificing some hit-rate for a much tighter stop and
higher R:R. Regime-gated to trending markets only via the existing
regime_router.

- [ ] **6.3.1 Donchian breakout-retest strategy**
  - **Deliverable:** `strategies/breakout/donchian_retest.py` + validator integration
  - **Acceptance:** signal logic:
    1. `level = highest_high_20`. Breakout fires on close > level.
    2. Mark a "pending retest" entry valid for the next 5 bars.
    3. If price pulls back and touches level (±0.5 × ATR_14) within the window, *enter long* with stop at `level − 0.5 × ATR_14`.
    4. If 5 bars elapse with no retest, cancel the pending entry (no chase).
    Declared `active_in_regimes=["bull", "trend"]`, `pyramidable=false` (the retest IS the entry — no add-on logic). Stop method `atr_initial` (6.1.1) with `k=1.0` (tight). Validator must PASS on a 5-year backtest before going live. Tests: signal sequencing, pending-window expiry, retest tolerance, no-chase enforcement.
  - **Notes:** Expected hit rate ~40-50% with R:R near 3:1 — the inverse profile of mean-reversion. Don't compare its win-rate directly to RSI2; compare expectancy.

- [ ] **6.3.2 Resistance-level breakout-retest (short side)**
  - **Deliverable:** `strategies/breakout/donchian_retest_short.py`
  - **Acceptance:** mirror of 5.3.1 for shorts: `level = lowest_low_20`, breakdown fires on close < level, pending retest valid 5 bars, entry on retest of level from below with stop at `level + 0.5 × ATR_14`. `active_in_regimes=["bear", "trend"]`. Validator gate same as 5.3.1. Tests: mirror of 5.3.1 short side.
  - **Notes:** Shorts have asymmetric borrow costs and unlimited-loss exposure. Default `pyramidable=false` and a max-position-size override that's 50% of the long equivalent.

---

## 6.4 Parabolic SAR exit overlay

SAR is known to whipsaw as a standalone signal. As an *overlay* — an
optional early-exit on trend strategies that fires whichever comes first
between SAR and the trailing stop — it can lock in profit when momentum
stalls before the trailing stop would have triggered. Opt-in per strategy.

- [ ] **6.4.1 SAR computation + overlay engine**
  - **Deliverable:** `monitoring/sar_overlay.py` + auto_trader integration
  - **Acceptance:** standard Parabolic SAR computation (acceleration factor starting at 0.02, increments of 0.02, max 0.2 — Wilder's defaults). For any strategy with `sar_overlay: true` in its declaration, the exit check is `should_exit = trailing_stop_hit OR sar_flip`. SAR state persisted per open position. Tests: SAR math against known-good Wilder sequence, overlay precedence (whichever fires first wins), no-overlay-when-disabled.

- [ ] **6.4.2 SAR overlay opt-in for trend strategies**
  - **Deliverable:** strategy declarations updated + 30-day A/B record
  - **Acceptance:** enables `sar_overlay: true` on the three Phase 4.6.3 trend strategies (donchian_breakout_20, ma_cross_20_50, new_high_volume) in paper mode only. Records a parallel `paper_trades_sar_overlay` table that captures hypothetical exit prices/PnL as if SAR had also been firing — so Ross can compare 30 days of "SAR overlay on" vs "off" without disturbing live PnL. Tests: parallel-record shape, no-impact-on-live-pnl invariant, A/B aggregation math.
  - **Notes:** Decision to keep SAR overlay ON live is gated on the 30-day A/B result — separate milestone in Phase 6 once data is in.

---

## 6.5 Options-based synthetic pyramiding — research milestone

Pyramiding shares (4.6.2) carries reversal risk on the added tiers.
Options provide asymmetric risk on the add-ons. But the infrastructure
cost is real — chain data, IV surface, Greeks, expiry rolls, assignment
handling. This Phase 5 milestone is **research-only** — the implementation
gates on a positive go/no-go and waits for Phase 6.

- [ ] **6.5.1 Options synthetic pyramiding feasibility**
  - **Deliverable:** `docs/OPTIONS_PYRAMIDING_RESEARCH.md` (NOT code)
  - **Acceptance:** documents (a) which Alpaca options endpoints we'd need (chain, quote, exercise) and current API limits, (b) the data infrastructure delta vs current (IV surface storage, expiry calendar, contract-multiplier handling), (c) candidate structures — long calls vs bull call spreads vs ratio spreads — with expected payoff diagrams, (d) which 4.6 trend strategies would actually benefit (one with 5-10× R winners) vs which would be diluted by theta decay (anything <30-day hold), (e) tax & regulatory delta from share pyramiding, (f) recommended go/no-go criteria for Phase 6 implementation. No code.
  - **Notes:** Cross-reference `docs/OPTIONS_RESEARCH.md` from 3.4.2 — that research was on long-only screening, not pyramiding-as-amplifier. This milestone is the narrower question: "given 4.6 trend pyramiding already works on shares, what marginal benefit do options add?"

---

## Notes for Phase 7 candidates

- Options pyramiding implementation (if 5.5.1 verdict is GO)
- SAR overlay → live flip (if 5.4.2 A/B is positive)
- ½ Kelly graduation flow (per-strategy promotion after 200+ closed outcomes)
- Realtime websocket fills (replace minute-cadence polling)
- LLM-driven daily strategy review (Claude summarizes the prior trading day, flags anomalies)

## Out of scope for Phase 6

- Full Kelly (hard-capped at ½ in 5.2.2 enforcement)
- Options implementation (research only in 5.5)
- Multi-account live (>1 broker) — defer to Phase 6
- HFT / market making
- Margin trading on equities
