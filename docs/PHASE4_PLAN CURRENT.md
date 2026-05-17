# Profit Generation — Phase 4 Plan (DRAFT)

> **⚠️ DRAFT.** Rename to `PHASE4_PLAN CURRENT.md` before running
> `/next-milestone` against it. The milestone-builder agent searches for
> the `CURRENT.md` suffix — keeping this file as DRAFT prevents
> accidental autonomous execution before Ross reviews and refines.

This will become the source of truth for **milestone-builder** in Phase 4.
Same conventions as Phase 2 / Phase 3:
- Python interpreter: `py -3.13` for unit tests / scripts. Conda env
  `trading` (Python 3.11) for anything that imports yfinance / alpaca-py.
- Test command: `py -3.13 -m pytest tests/<file>.py` (skip live API tests)
- Commit style: conventional commits with the standard `Co-Authored-By`
  footer.
- Branch: push directly to `main`.
- Never modify `config/credentials.json`, `data/*.db`, `logs/`.

**Phase 4 theme:** Phase 3 hardened operations and shipped the
*capability* for live equity, live crypto, and per-strategy segregation —
but Ross hasn't flipped any live switch yet. Phase 4 walks across that
bridge carefully (4.1), opens the crypto leverage question (4.2), swaps
the local-Ollama codegen path for a higher-quality Claude-API one (4.3),
and finally puts a public read-only performance page online (4.4). Small
operational followups from Phase 3's concerns sit in 4.5.

---

## 4.1 Live transition (equity + crypto)

- [x] **4.1.1 Live-promotion scorer**
  - **Deliverable:** `scripts/score_live_candidates.py` + `tests/test_score_live_candidates.py`
  - **Acceptance:** ranks every active strategy by paper-trading track record. Score = mean live return × √N × stable_sharpe. Flags any strategy with N≥50 closed paper outcomes AND Sharpe > 0.4 AND positive mean return as `READY_FOR_LIVE`. Output: a single sorted report to stdout AND Notion. Does NOT flip anything — only surfaces candidates. Tests: scoring math, threshold gating, dedupe against already-live strategies.
  - **Notes:** This is the tool Ross will use to *decide* which strategies graduate to `auto_trade.live_strategies` (3.1.5). Manual flip remains a deliberate human decision.
  - **Completed:** 2026-05-17 by milestone-builder · commit ee26ae5

- [~] **4.1.2 Live-credentials onboarding wizard**
  - **Deliverable:** `scripts/setup_live_credentials.py` + idempotent flow
  - **Acceptance:** interactive wizard that prompts Ross for Alpaca live keys, validates them against the live API, writes them into `config/credentials.json` under `alpaca_live`, and posts a confirmation to Notion. Refuses to overwrite existing live keys without `--force`. Tests: schema validation, refusal-on-existing, dry-run mode.
  - **Notes:** Agent must NOT execute this milestone end-to-end since it requires Ross's live API keys. Agent ships the wizard code + tests; Ross runs it himself.
  - **Partial:** 2026-05-17 code + 24 tests shipped (commit 845d4f4). READY FOR ROSS to run with `py -3.13 scripts/setup_live_credentials.py --dry-run` first, then without `--dry-run` once live keys are in hand.

- [x] **4.1.3 Live-equity smoke-test playbook**
  - **Deliverable:** `docs/LIVE_SMOKE_TEST.md` (NOT code)
  - **Acceptance:** documents the day-1-live procedure end-to-end: which strategy goes first (one ETF, single share size), how to monitor the first 5 fills in real-time, abort criteria, rollback to paper, sign-off checklist. Cross-references preflight, reconcile_positions, kill_switch. ≤ 10 procedures, each ≤ 5 steps.
  - **Completed:** 2026-05-17 by milestone-builder · commit f7b73fb

- [x] **4.1.4 Live-crypto smoke-test playbook**
  - **Deliverable:** `docs/CRYPTO_SMOKE_TEST.md` (NOT code)
  - **Acceptance:** mirror of 4.1.3 but for the crypto adapter built in 3.4.1. Notes BTC/USD vs ETH/USD spread differences, 24/7 monitoring expectations, minimum capital. ≤ 10 procedures.
  - **Completed:** 2026-05-17 by milestone-builder · commit ab1f86f

---

## 4.2 Crypto leverage / margin

- [ ] **4.2.1 Crypto leverage feasibility — research milestone**
  - **Deliverable:** `docs/CRYPTO_LEVERAGE_RESEARCH.md` (NOT code)
  - **Acceptance:** documents (a) which Alpaca crypto products support leverage at all, (b) maintenance margin formulas, (c) liquidation mechanics + funding rates, (d) which of our crypto strategies would actually benefit from leverage vs which would just amplify slippage burn, (e) regulatory + tax angle for leveraged crypto, (f) recommended go/no-go criteria. No code.

- [ ] **4.2.2 Leverage-aware sizing (conditional on 4.2.1 GO)**
  - **Deliverable:** `monitoring/crypto_adapter.py` extended with `leverage` parameter + `crypto.max_leverage` setting (default 1.0)
  - **Acceptance:** ships only if 4.2.1 verdict is GO. Per-symbol leverage cap, liquidation-distance check before every entry (refuse entries within 20% of liquidation price), separate `crypto_leverage` action surfaced in process_signals result. Tests: liquidation math, refusal cases, default-to-1.0 fallback when 4.2.1 not yet greenlit.

---

## 4.3 Strategy generation via Claude API

- [ ] **4.3.1 Claude-API codegen adapter**
  - **Deliverable:** `monitoring/codegen_claude.py` + integration into existing batch_validate pipeline
  - **Acceptance:** drop-in replacement for the Ollama codegen path. Same input schema (UNTESTED record from records.jsonl), same output schema (strategy implementation). Uses prompt caching for the system prompt + few-shot examples. CLI flag `--model claude` on batch_validate routes to this adapter. Tests: prompt construction, cache-key stability, response parsing.
  - **Notes:** Prompt caching is mandatory per global CLAUDE.md — system prompt + ≥5 few-shot strategy examples should be marked `cache_control`.

- [ ] **4.3.2 Codegen quality A/B**
  - **Deliverable:** `scripts/codegen_ab.py` + report
  - **Acceptance:** takes N UNTESTED records from records.jsonl, generates each twice (once via Ollama, once via Claude), runs both through the validator, computes win-rate, PASS-rate, mean Sharpe, and a per-strategy delta. Output: Notion post + JSON summary. Cost-tracked (Claude API spend logged). Tests: aggregation math, cost accounting.

- [ ] **4.3.3 Claude-API budget gate**
  - **Deliverable:** `config/api_budget.json` + budget check inside `codegen_claude.py`
  - **Acceptance:** daily budget cap (default $5/day, configurable). On exhaustion, auto-fallback to Ollama path with a Telegram alert. Tracks running spend in a new `api_spend` table keyed by date. Tests: budget exhaustion fallback, daily reset at UTC midnight.

---

## 4.4 Public read-only performance page

- [ ] **4.4.1 Sanitized performance API**
  - **Deliverable:** new `/api/public/*` endpoints on dashboard with NO auth
  - **Acceptance:** exposes a public-safe subset: equity curve (no $ amounts, % returns only), per-strategy Sharpe + win-rate, last 30-day P&L %. NEVER exposes: position sizes, open positions, credentials, Alpaca account IDs, raw fill data. Rate-limited per IP (60 req/min). Tests: sensitive fields rejected, rate-limiter, shape validation.

- [ ] **4.4.2 Static performance page**
  - **Deliverable:** new `public/` subdirectory with a single-page site (Next.js OR plain HTML + Chart.js — pick the lighter-weight option)
  - **Acceptance:** renders equity curve, per-strategy stats table, "last updated" timestamp. Mobile-responsive. Calls the 4.4.1 endpoints. No login. Lighthouse score ≥ 90. Tests: build succeeds, snapshot of HTML output.

- [ ] **4.4.3 Daily Vercel auto-deploy**
  - **Deliverable:** `schedulers/deploy_public.bat` + Vercel project config
  - **Acceptance:** daily schtask `\TradingSystem\PublicDeploy` at 23:30 rebuilds the static page with the latest performance numbers and deploys to Vercel. Notion alert on deploy success/failure. Tests: deploy command construction, failure-path Telegram alert.
  - **Notes:** Vercel project must be created manually by Ross first — agent surfaces a missing-config message rather than auto-creating.

---

## 4.5 Phase 3 operational followups

- [ ] **4.5.1 Preflight `--tunnel` check**
  - **Deliverable:** `scripts/preflight.py` extended with a dedicated `--tunnel` flag
  - **Acceptance:** replaces the inline Python one-liner that 3.5.5 RUNBOOK Procedure 6 currently uses to check tunnel_url.txt freshness. CLI: `py -3.13 scripts/preflight.py --tunnel` exits 0 if file < 1d old, non-zero otherwise. RUNBOOK gets updated to use the new flag. Tests: stale-file detection, missing-file case.

- [ ] **4.5.2 Dashboard card helper extraction (deferred)**
  - **Deliverable:** `dashboard/index.html` refactored to share JS helpers between slippage / fill latency / divergence cards
  - **Acceptance:** ONLY ship if a 4th similar card is added. Three similar cards beats a premature abstraction (Phase 3 closing note). Reopen this milestone when card #4 lands.
  - **Notes:** Keep as a placeholder — agent should skip on first encounter and tick a `(deferred)` note rather than build.

---

## 4.6 Trend-following with pyramiding and trailing stops

Currently the strategy roster is mean-reversion-heavy (RSI2 oversold, consec-bearish bounces). This section adds the opposite market mode — trend-following — so the system covers both regimes. The regime router (3.3.3) already exists to route capital between them automatically.

- [ ] **4.6.1 Trailing stop engine**
  - **Deliverable:** `monitoring/trailing_stops.py` + `tests/test_trailing_stops.py` + auto_trader integration
  - **Acceptance:** three configurable formulas selectable per strategy via `trailing_stop.method` setting:
    - `atr_trail` — stop = highest_high_since_entry − (multiplier × ATR_14)
    - `chandelier` — stop = highest_high_over_N_days − (multiplier × ATR_22)
    - `percent_trail` — stop = highest_high_since_entry × (1 − pct)
    Stop only moves UP for longs, DOWN for shorts (ratchet, never loosen). New `trailing_stops` table tracks current stop per open position; updated on every bar close. Auto-trader honors the trailing stop on exit eligibility check. Tests: monotonic-ratchet, formula correctness for each method, position-direction handling, no-update-on-flat-bar.
  - **Notes:** Initial stop (entry-time ATR stop from 2.3.4) still applies until the trailing stop crosses above it. Then trailing takes over.

- [ ] **4.6.2 Pyramiding logic in auto_trader**
  - **Deliverable:** `auto_trade.pyramiding` settings section + auto_trader add-on entry logic + `paper_trades` schema additions for tier tracking
  - **Acceptance:** when a position is already open and a new confirming signal fires from the same strategy + same direction + regime still trend-aligned, submit an *add-on* order. Add-on sizes follow a tier schedule (default `[1.0, 0.5, 0.25, 0.125]` — initial entry full size, then halving). Max-N tiers configurable (default 4). Each add-on logged with `pyramid_tier` column. The trailing stop applies to the *whole* aggregated position. Tests: tier sequencing, size math, regime-veto on add-ons, max-N cap, stop applies to aggregate.
  - **Notes:** Pyramiding is opt-in per strategy via `pyramidable: true` declaration in `TRACKED_STRATEGIES`. Mean-reversion strategies must NOT be pyramidable — there's no continuation thesis there.

- [ ] **4.6.3 Trend-following strategy implementations**
  - **Deliverable:** `strategies/trend/` module with three implementations + validator runs through existing PASS/FAIL pipeline
  - **Acceptance:** three trend strategies marked `pyramidable: true` and `active_in_regimes=["bull", "trend"]`:
    - `donchian_breakout_20` — long on close above 20-day high, exit on close below 10-day low
    - `ma_cross_20_50` — long on 20-EMA crossing above 50-EMA, exit on opposite cross
    - `new_high_volume` — long on new 52-week high accompanied by volume ≥ 150% of 50-day average
    All three respect the trailing stop engine (4.6.1) and pyramid via 4.6.2. Validator must show PASS on at least one over a 5-year backtest before any goes live. Tests: signal shape, regime gating, pyramiding declaration, validator integration.
  - **Notes:** Expected behavior: win rate 30-40%, but the avg winner is 5-10× the avg loser (classic Turtle profile). Long flat periods between trends are normal — don't pull a strategy that's underperforming for 60 days unless the divergence report (3.6.2) shows it's actually broken.

- [ ] **4.6.4 Trend / mean-reversion regime allocator**
  - **Deliverable:** `monitoring/regime_router.py` extended with capital-allocation logic
  - **Acceptance:** the existing regime router only does on/off gating per strategy. Extend it to *allocate capital* between the two modes based on current regime: clear trend → 70% trend / 30% mean-reversion; chop → reverse; mixed → 50/50. Allocation expressed as multipliers applied on top of tiered sizing from 3.2.1. Tests: allocation math, regime transitions, default-50/50 fallback when classifier confidence < 0.6.
  - **Notes:** Together with 4.6.1-3, this is the piece that makes the system actually adaptive — capital flows automatically to whichever strategy class fits current conditions, instead of every strategy fighting for the same dollars.

---

## Notes for Phase 5 candidates

- Multi-account live (>1 broker, e.g. IBKR alongside Alpaca)
- Futures (after `docs/FUTURES_RESEARCH.md` go/no-go gate)
- Options (after `docs/OPTIONS_RESEARCH.md` go/no-go gate)
- LLM-driven daily strategy review (Claude summarizes the prior trading day, flags anomalies)
- Realtime websocket fills (replace minute-cadence polling)

## Out of scope for Phase 4

- HFT / market making
- Anything requiring an LLC or formal entity
- Margin trading on equities (separate risk surface from crypto leverage)
- Strategy generation via fine-tuned models (Claude API or Ollama only)
