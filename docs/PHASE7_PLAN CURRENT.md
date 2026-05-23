# Profit Generation — Phase 7 Plan (DRAFT)

> **⚠️ DRAFT.** Rename to `PHASE7_PLAN CURRENT.md` before running
> `/next-milestone` against it. The milestone-builder agent searches for
> the `CURRENT.md` suffix — keeping this file as DRAFT prevents
> accidental autonomous execution before Ross reviews and refines.

Same conventions as Phase 6:
- Python interpreter: `py -3.13` for unit tests / scripts. Conda env
  `trading` (Python 3.11) for anything that imports yfinance / alpaca-py.
- Test command: `py -3.13 -m pytest tests/<file>.py` (skip live API tests)
- Commit style: conventional commits with the standard `Co-Authored-By`
  footer.
- Branch: push directly to `main`.
- Never modify `config/credentials.json`, `data/*.db`, `logs/`.

**Phase 7 theme:** Phase 6 added the *risk-and-entry-surface* upgrades
(ATR / Kelly / breakout-retest / SAR overlay) and queued options research.
Phase 7 takes the gating decisions sitting at the end of Phase 6 — does
SAR overlay graduate to live, does options pyramiding get built — and
adds the first LLM-in-the-loop component. The new shape: rule-based
strategies stay the trade-pickers, but an LLM becomes the contextual
filter on every signal, in shadow mode first. If after 30 days the LLM
filter demonstrably improves outcomes, it earns its place in the live
decision path. Same pattern as 6.4.2 SAR overlay — shadow-record first,
graduate on evidence.

---

## 7.1 Intraday LLM filter overlay — shadow mode

The rule-based strategies generate signals. Most of those signals fire
into a context that makes them better or worse than the rule alone can
see — Fed minutes, halt news, sector-wide correlated fires, an earnings
print hours away that earnings-veto missed because the data source
was stale. An LLM with structured input on every fire can flag the
ones that should be skipped or downsized. This milestone wires that
in as a parallel shadow recorder — same architecture as 6.4.2 — so we
get 30 days of "LLM said skip / strategy fired anyway" data before the
filter touches live PnL.

- [x] **7.1.1 LLM filter call + shadow table**
  - **Completed:** 2026-05-22 by milestone-builder
  - **Deliverable:** `monitoring/llm_filter.py` + `paper_trades_llm_filter`
    parallel table + Anthropic SDK wiring through `config.credentials`.
  - **Acceptance:** for every fire that auto_trader receives (EOD and
    intraday), `monitoring/llm_filter.py:assess_signal()` is called with
    a structured prompt containing: (i) the signal — strategy_id, symbol,
    side, close, bar_ts, signal_type; (ii) market context — current
    regime classification, macro strip, today's notable movers; (iii)
    recent news for the symbol (last 24h, top 5 by published_utc); (iv)
    earnings calendar within ±5 days; (v) prior 5 closed outcomes for
    this strategy. Returns a structured JSON with `verdict`
    ("allow" | "skip" | "downsize"), `confidence` (0-1), `rationale`
    (one sentence), and `factors` (list of up to 3 short tags like
    "fed_minutes_today", "halted_intraday", "earnings_in_2d"). Result
    is written to `paper_trades_llm_filter` keyed by
    `(strategy_id, symbol, bar_ts, signal_type)`. **Auto_trader DOES NOT
    consume the verdict in this milestone** — strict shadow mode. Tests:
    no-impact-on-live-PnL invariant (mirrors 6.4.2's
    `test_shadow_does_not_affect_paper_trades_when_sar_flips`),
    structured-output schema validation, prompt-context shape, network
    failure isolation (LLM unreachable → log warning, fire proceeds
    unchanged — **fail-open** is the chosen behavior, see Decisions log).
  - **Cost guardrails:** `claude-sonnet-4-6` at ~5K-token context per
    call (chosen for the shadow phase — see Decisions log); cap calls
    at 200/day via a daily counter, **fail-open on cap exceeded**
    (the strategy fires unchanged when we hit the cap). Use prompt
    caching on the static prefix (system instructions + schema) —
    90% cost reduction on cached portion.
  - **Surfacing:** verdicts + rationales render on a new **dashboard
    card only** during shadow phase. No Telegram alerts on `skip` —
    actionability is zero while the filter doesn't consume verdicts.
  - **Notes:** Use prompt caching for the static parts of the prompt
    (system instructions, schema definition) — that's a 90% cost
    reduction on the cached prefix. See `/skill claude-api` for the
    canonical patterns; this milestone should include caching from
    day one, not as a follow-up.

- [ ] **7.1.2 LLM filter A/B aggregation**
  - **Deliverable:** `monitoring/llm_filter_ab.py` aggregation helper
    + dashboard card mirroring the Kelly card pattern (6.2.3).
  - **Acceptance:** computes the same A/B shape as the SAR overlay
    aggregator — what would PnL have looked like if every "skip"
    verdict had been honored. Filter-by-strategy variant. Min sample
    size 50 closed outcomes before the card displays a verdict. Tests:
    A/B math on hand-computed fixture, sample-size gate honored,
    strategy-filter pass-through.

- [ ] **7.1.3 LLM filter graduation to live**
  - **Deliverable:** settings flag + auto_trader integration.
  - **Acceptance:** after 30 days, if `llm_filter_ab.summary()` shows
    a positive PnL delta with min sample 200 outcomes AND a Sharpe
    delta ≥ 0.2, a new setting `auto_trade.llm_filter_live: true`
    becomes meaningful — when set, auto_trader honors `verdict=skip`
    and downsizes `qty` by 50% on `verdict=downsize`. Default is
    `false` even after the gate is unlocked; flipping it on is a
    manual decision Ross makes. Tests: settings-gated behavior,
    downsize math, allow-passthrough math.
  - **Notes:** Decision to flip the flag is gated on the 30-day A/B
    *and* Ross's manual sign-off. The milestone ships the capability,
    not the activation.

---

## 7.2 SAR overlay → live graduation — gated on 6.4.2 A/B result

Phase 6.4.2 shipped the shadow-mode SAR overlay on the three trend
strategies. After 30 days (≈2026-06-21), the aggregator returns a
verdict. If positive, this milestone flips overlay to live.

- [ ] **7.2.1 SAR overlay live flip**
  - **Gate:** `paper_trades_sar_overlay` aggregator shows positive PnL
    delta with min sample 100 outcomes per strategy.
  - **Deliverable:** flip `sar_overlay: "shadow"` → `sar_overlay: true`
    on whichever of the three trend strategies cleared the gate.
    Per-strategy decision, not all-or-nothing.

---

## 7.3 Options synthetic pyramiding — gated on 6.5.1 verdict

If `docs/OPTIONS_PYRAMIDING_RESEARCH.md` (6.5.1) returns GO, this
section gets expanded into milestones for chain data ingestion, IV
storage, Greeks pipeline, and contract lifecycle handling. If NO-GO,
this section gets removed and the slot fills with whatever's next on
the Phase 7 candidate list.

- [ ] **7.3.x** — placeholder, to be expanded post-6.5.1.

---

## 7.4 ½ Kelly graduation flow

Phase 6.2.2 capped Kelly at ½ for all strategies. The next move is a
per-strategy graduation flow — strategies that demonstrate stability
over 200+ closed outcomes can promote from fractional to ½ Kelly
proper, with a manual sign-off gate.

- [ ] **7.4.1 Kelly tier promotion machinery**
  - **Deliverable:** `monitoring/kelly_promotion.py` + new
    `kelly_tier` column on the strategies table.
  - **Acceptance:** background helper recomputes the current Kelly
    tier per strategy nightly. When a strategy crosses the
    promotion threshold (n_closed ≥ 200, win_rate within ±5% of
    backtest expectation, max_drawdown_pct ≤ 1.5× backtest max_dd),
    a Telegram alert fires asking Ross to confirm the promotion.
    Without confirmation, the tier stays put. Tests: threshold
    math, stability gate, alert-only-once-per-strategy.

---

## 7.5 Realtime websocket fills (replaces polling)

Current intraday loop polls Alpaca every 15m. For LLM filter at 1m
cadence (or even just for fill-latency reduction on existing
strategies), we need Alpaca's bar websocket subscription.

- [ ] **7.5.1 Websocket bar subscription**
  - **Deliverable:** `monitoring/ws_bars.py` + scheduler change.
  - **Acceptance:** drop-in replacement for the 15m poll loop —
    same fire-detection logic, but evaluated bar-by-bar as Alpaca's
    websocket emits them. Reconnect logic, no-bars-for-N-seconds
    heartbeat alarm. Tests: reconnect on dropped connection,
    fire-detection equivalence vs polling on a recorded session,
    heartbeat alarm fires at threshold.

---

## Notes for Phase 8 candidates

- Crank LLM filter cadence to 1m (depends on 7.5 websocket + 7.1 baseline)
- Multi-LLM ensemble (Opus + Sonnet votes) for high-stakes fires
- Options pyramiding implementation (if 6.5.1 → GO)
- Per-symbol regime tagging (LLM classifies the *symbol's* regime, not
  just the market's)

## Out of scope for Phase 7

- Auto-promotion of LLM-proposed novel strategies into TRACKED_STRATEGIES
  (gated on demonstrated LLM value as a *filter* first — earn it).
- Live trading flip (still paper-only; the no-go-live posture is the
  whole point of the shadow-A/B-then-graduate pattern).
- Multi-account / sub-account support.

---

## Decisions log

**2026-05-21** — Ross signed off on the §7.1.1 spec questions:

1. **Fail mode** → **fail-open (allow)**. Malformed JSON, timeouts,
   API errors, and daily cap exceedance all default to `verdict=allow`
   so the strategy fires unchanged. The bias matches the system's
   existing "try the strategy unless something explicitly blocks it"
   philosophy. Costs us a few mis-trades when the LLM is broken;
   never costs us missed opportunities.
2. **Model** → **`claude-sonnet-4-6`** for the shadow phase. Haiku
   was the cheaper alternative but would miss the context-heavy edge
   cases that are exactly what makes the filter valuable. Re-evaluate
   the Haiku downgrade post-A/B if the calls turn out to be mostly
   formulaic.
3. **Surfacing** → **dashboard card only**. No Telegram on `skip`
   verdicts during shadow phase — actionability is zero while
   auto_trader doesn't consume the verdicts. Telegram surfacing
   becomes a question again at §7.1.3 graduation.
