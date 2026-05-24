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
  - **Sequencing note (2026-05-22):** §7.1.2 is paused behind §7.5
    (Live intraday data layer). The LLM filter has nothing
    intraday-relevant to evaluate until 7.5.5 ships 1m-native
    strategies — see §7.5.6 for the filter's actual activation
    milestone. 7.1.2 reopens once 30 days of shadow data has
    accumulated post-activation.
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

## 7.5 Live intraday data layer (augments existing system)

**Approach revision (2026-05-22):** Original §7.5 framed this as
"replace 15m polling with websocket." Per Ross's redirect, the
new framing is **augment, not replace** — the existing 15m
intraday loop and daily strategies keep trading throughout, and
the new data layer runs alongside them with zero behavior change
on existing strategies. New strategies that want minute-resolution
data opt in via Workstream B. Existing strategies just gain
visibility through Workstream A's skip-reason logging.

**Free-tier discipline:** stay on Alpaca free IEX WebSocket until
the system proves it can handle minute-bar data without breaking.
Upgrade to Algo Trader Plus ($99/mo SIP feed) only when there's
concrete evidence the IEX-only view costs us fills. The whole
point of this sequencing is to surface every bug paid data
would have hidden behind better quality.

### Workstream A — Data layer (no behavior change on existing trades)

- [x] **7.5.1 Alpaca IEX WebSocket listener + minute-bar storage**
  - **Completed:** 2026-05-22 by milestone-builder (commit e85da5a)
  - **Deliverable:** `monitoring/live_stream.py` + new `intraday_bars`
    and `stream_heartbeat` tables (added to `_DDL` in `data/db.py`).
    Long-running process; either runs under a new scheduled task
    `schedulers/run_live_stream.bat` or as a service. Subscribes to
    Alpaca's free IEX WebSocket bars channel for the
    `TRACKED_STOCKS + TRACKED_SECTORS` universe (10 symbols initially).
  - **Acceptance:** connects to `wss://stream.data.alpaca.markets/v2/iex`
    using credentials from `config/credentials.json:alpaca`. Authenticates,
    subscribes to bars + trades for the 10-symbol watchlist. Each incoming
    bar is upserted into `intraday_bars(symbol, ts_utc, open, high, low,
    close, volume, source='iex')`. `stream_heartbeat(component='live_stream',
    last_ts, reconnects_today, last_error)` updates every 5 seconds.
    Reconnects automatically on socket drop with exponential backoff
    (1s, 2s, 4s, 8s, max 60s). Existing 15m polling loop is **NOT**
    touched — it keeps running as the canonical fire-detection path.
  - **Tests:** synthetic WebSocket server fixture; subscribe-confirm
    handshake; bar parsing → row upsert byte-equality on replay;
    reconnect-after-drop preserves subscription; heartbeat updates
    on schedule; ungraceful crash → next invocation resumes without
    duplicate bars.
  - **Universe note:** start with 10 symbols. IEX free tier has
    bandwidth limits and the full 503-symbol trend universe would
    saturate the connection. Expand by editing the universe in
    7.5.5 once the listener proves stable.

- [x] **7.5.2 Skip-reason logging retrofitted to the existing risk gate**
  - **Deliverable:** new `intraday_skips` table + edits to
    `monitoring/auto_trader.py` everywhere a fire is silently dropped.
    No new module — pure retrofit.
  - **Acceptance:** every gate in the existing auto_trader (kill_switch,
    dry_run, max_position_usd cap, max_open_positions, concentration_cap,
    cool_down, earnings_veto, negative_sentiment_veto, pdt_guard,
    drawdown_breaker, intraday_symbol_cap) writes a row to `intraday_skips`
    when it blocks a fire. Schema: `(id, recorded_at, strategy_id, symbol,
    bar_ts, signal_type, gate, reason_detail, source)` with
    `source IN ('daily','intraday_15m','live_stream')`. The block
    decision is unchanged — we just stop dropping fires silently.
    A new `/api/skip_reasons` route surfaces the last N rows for
    the dashboard.
  - **Tests:** each gate is exercised with a fire that would trigger
    it; the row gets written with the correct gate/reason; the live
    decision (block) is byte-identical to pre-retrofit behavior
    (no-impact invariant — mirrors 6.4.2 SAR overlay test pattern);
    `intraday_skips` table is idempotent on `init_db()` re-call.
  - **Why this milestone matters most:** the existing 15m loop drops
    fires for ~12 of every 14 entries (observed 2026-05-21). Today we
    can't tell which gate is biting hardest without spelunking the
    auto_trader code path. After 7.5.2, every drop is queryable.

- [x] **7.5.3 Dashboard cards: live feed status, intraday bars, skip reasons**
  - **Completed:** 2026-05-24 by milestone-builder
  - **Deliverable:** new dashboard panel section in `dashboard/index.html`
    + supporting routes in `dashboard/server.py`.
  - **Acceptance:** three new cards on the existing dashboard:
    (i) **Live Feed Status** — websocket state (CONNECTED / DISCONNECTED /
    RECONNECTING), feed identifier (`Alpaca IEX`), subscribed symbol count,
    seconds-since-last-message, reconnects-today; (ii) **Intraday Bars** —
    most recent 1m bar per watched symbol with timestamp freshness indicator;
    (iii) **Skip Reasons** — top 5 skip reasons over the last 24h with
    counts, plus a clickable detail view showing recent skip rows.
    Routes: `/api/live_feed_status`, `/api/intraday_bars_latest`,
    `/api/skip_reasons`. All loopback-only (PG-011 precedent).
  - **Tests:** route response-shape tests against a seeded
    `intraday_bars` / `intraday_skips` / `stream_heartbeat` fixture;
    `STALE` vs `FRESH` threshold logic for the bar freshness indicator.

### Workstream B — Strategy integration (opt-in, shadow-first)

- [ ] **7.5.4 Intraday confirmation overlay — shadow mode**
  - **Deliverable:** `monitoring/intraday_confirm.py` + a new
    `paper_trades_intraday_confirm` parallel table.
  - **Acceptance:** mirrors 6.4.2 SAR overlay pattern exactly. Any
    strategy with `intraday_confirm: "shadow"` in its declaration
    records what *would* have happened if the entry required a 1m
    close above the trigger price before order submission. The live
    entry path is unchanged. Records `(strategy_id, symbol,
    daily_signal_ts, would_have_confirmed_at, hypothetical_entry_price,
    shadow_pnl_at_close, real_pnl_at_close)` for 30 days of A/B data.
  - **Tests:** no-impact-on-live-paper-trades invariant
    (`test_shadow_does_not_affect_paper_trades`); confirmation math
    on a recorded 1m bar stream; missing-1m-data graceful degrade
    (records `would_have_confirmed=null`, no exception).
  - **Notes:** opt-in per strategy. Recommend starting with the three
    trend strategies (donchian-breakout-20, ma-cross-20-50, new-high-volume)
    — they're the slowest-firing entries and would benefit most from
    a confirmation gate.

- [ ] **7.5.5 NEW 1m-native strategies — ORB-1m + momentum + VWAP-reclaim**
  - **Deliverable:** three new strategy declarations + compute functions
    in `strategies/intraday/`. Added to `TRACKED_STRATEGIES` via a new
    `INTRADAY_1M_DECLARATIONS` list in `monitoring/config.py`.
  - **Acceptance:** runs alongside existing strategies; each new
    strategy treated as a normal `TRACKED_STRATEGIES` entry with
    `bar_interval: "1m"`, `grace_period: true`, `max_position_usd: 200`
    (capped at 20% of normal max while the strategies prove themselves).
    Routes through the existing auto_trader paper path; no special
    handling needed. Strategies:
    - `intraday-1m-orb` — 1-minute opening-range breakout (first 5 minutes
      define the range; first break thereafter triggers entry).
    - `intraday-1m-momentum` — 3 consecutive 1m bars closing above a
      rising 20-period EMA with rvol > 1.5x.
    - `intraday-1m-vwap-reclaim` — price crosses back above VWAP after
      a dip below, with volume confirmation.
  - **Tests:** compute_fn tests against fixture 1m bar arrays for each
    strategy; integration test that all three resolve through
    `_resolve_compute_fn`; smoke test of the full pipeline on a 1-hour
    recorded session.
  - **Why a starter triple, not 20:** Ross's plan called this out
    explicitly — start with 2-3, observe, expand only when those work.

- [ ] **7.5.6 EOD intraday report + LLM filter activation**
  - **Deliverable:** `monitoring/intraday_eod_report.py` + flips
    `settings.llm_filter.enabled` to true (with API key configured).
  - **Acceptance:** end-of-day report drinks the day's `intraday_bars`,
    `intraday_signals`, `intraday_skips`, and existing `paper_trades`
    to produce a markdown summary: total fires by strategy, skip
    breakdown by reason, paper P&L by strategy, top divergences
    between intraday signals and EOD outcomes. Posts to Notion via
    existing channel. **AND** at this milestone, the LLM filter shipped
    in 7.1.1 finally activates — it has real intraday signal to filter
    on now, where it didn't before. The filter still runs strict-shadow
    (no live consumption), per 7.1.1 spec.
  - **Tests:** EOD report shape on a seeded day; LLM filter activation
    only happens when the API key is present (graceful no-op otherwise);
    LLM filter no-impact invariant continues to hold post-activation.
  - **Notes:** this is the milestone where the $18/mo Anthropic budget
    starts. Don't flip `llm_filter.enabled` before 7.5.5 lands — the
    filter has nothing intraday-relevant to evaluate until then.

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
