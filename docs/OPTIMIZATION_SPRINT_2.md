# Optimization Sprint 2 — Intraday Order-Management + Expectancy Governance

Source: trading-daily-analysis (Hermes cron, gpt-5.5) for 2026-06-04, corroborated
by a live broker check: the account is **unintentionally net-short ~$62k across 10
mega-caps** (AAPL, AMZN, AVGO, COIN, GOOGL, META, MSFT, NFLX, XLE, XLK) in a
long-only strategy system — caused by multiple intraday strategies owning the same
symbol and each firing its own exit/flatten, overselling past flat. Same root cause
behind the Alpaca `40310000` wash-trade rejections and the "insufficient qty
(held_for_orders=N)" flatten failures.

Execute milestones IN ORDER. For EACH: implement to existing conventions → run the
FULL non-live suite (`py -3.13 -m pytest tests/ -m "not live"`) → if green, tick the
box here → commit + push to main. End commit messages with:
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

**MEMORY/OOM GUARDRAIL:** a prior test caused a 38GB OOM via a self-referential
monkeypatch (a lambda replacing `db.init_db` that then called `db.init_db`). NEVER
replace a function with one that calls that same patched name — capture the real
callable first. Sanity-scan monkeypatches before running. Abort if pytest memory
climbs abnormally.

**Guardrails:** do NOT weaken any `risk.*` limit, the paper-mode gate, or the kill
switch. No new deps. **No autonomous live order placement** — M2 builds a tool and
HALTS before executing; the operator runs the cover orders. Each fix needs a test
that fails on current code.

---

## [x] M1 (P0, Bug) — single per-symbol position/order manager

**Problem:** No single owner per symbol. Multiple strategies (e.g. NVDA under
intraday-orb-pivots-5m + intraday-orbo-5m + intraday-1m-orb) each submit exits/stops
for the same broker position → wash-trade rejects, "insufficient qty" flatten
failures, and overselling into shorts. The A1 dedup fix (`c6689ee`) helped the
flatten query but did not address concurrent multi-strategy order submission.

**Fix:** Introduce a single per-symbol order/position reservation layer used by ALL
exit/stop/flatten paths (`auto_trader`, `close_intraday_positions`, `stops`):
- Before any sell/stop/flatten submit: fetch the broker's OPEN orders + position for
  the symbol, compute `available = position_qty - held_for_orders`, and submit only
  the net-available quantity (never more).
- Reconcile/cancel-or-replace incompatible existing exit orders before submitting a
  new one (prevents the `40310000` wash-trade reject).
- Idempotent flatten: one flatten attempt per (symbol, day); subsequent calls
  monitor/cancel-replace rather than re-firing failed SELLs.
- Never allow a sell to cross through zero into a short for a long-only strategy.

**Acceptance:** unit/integration tests proving (a) when `held_for_orders` reserves
shares, the manager submits only `available` and never oversells past flat; (b) a
second strategy's exit for an already-exiting symbol does not produce a duplicate/
conflicting order; (c) a long position is never flipped short. Full non-live suite green.

---

## [x] M2 (P0, Risk cleanup) — unintended-short detection + guarded cover tool

**Problem:** 10 live short positions (~-$62k) that should not exist.

**Fix:** Build `scripts/flatten_unintended_shorts.py` (or a guarded
`monitoring.*` entrypoint): detect broker positions with qty<0 that no strategy
intends to hold (all strategies here are long-only), print them, and with an explicit
`--execute` flag submit buy-to-cover orders sized to flatten exactly. DRY-RUN by
default. Idempotent and safe to re-run. Reuse the M1 reservation layer so cover orders
don't themselves conflict.

**Acceptance:** test proving dry-run lists shorts and computes correct cover qty
without placing orders, and that `--execute` path (mocked broker) submits exact
buy-to-cover quantities and nothing for long/flat symbols. **HALT after building +
testing + committing — do NOT run `--execute` against the live account.** Report the
exact command for the operator to run at the next market open.

---

## [x] M3 (P0, Trade-logic) — pause negative-expectancy intraday strategies

**Problem (evidence, recent closed-outcome stats):** intraday-1m-momentum (−0.42%),
intraday-1m-vwap-reclaim (−0.57%), intraday-1m-orb (−0.43%), intraday-orb-pivots-5m
(−1.84%, 0% win), intraday-orbo-5m (−1.45%), rsi2-oversold (−6.5%, toxic) — all
negative expectancy with heavy churn (momentum fired 3,254 exit signals today).

**Fix:** Set these strategies to observe-only / disabled for new entries (use the
existing strategy-status / paused mechanism — do NOT delete them; they keep recording
outcomes for re-evaluation). Donchian breakout and the small MR/botnet variants stay
active. Document which flag/table controls this.

**Acceptance:** test proving the named strategies no longer generate new live entries
while still being tracked, and that trend-donchian-breakout-20 is unaffected. Full
non-live suite green.

---

## [x] M4 (P1, Trade-logic) — expectancy kill/size gate

**Fix:** Auto-pause or size-down any strategy whose recent closed-outcome
`avg_return_pct < 0` (or win_rate < configurable floor) **after a minimum-sample
guard (N ≥ 20 closed outcomes; below that, leave at probation size, never kill on
noise)**. Wire into the existing eligibility/expectancy-tiered sizing (M4/F6), scoped
per the strategy's own interval class. Do NOT weaken numeric risk limits — only gate
WHICH strategies trade and at what size. This generalizes M3 so it self-maintains.

**Acceptance:** test proving a strategy with N≥20 and avg_ret<0 is auto-paused/
size-down, a strong strategy is untouched, and a strategy with N<20 is NOT killed.

---

## [x] M5 (P1, Optimization) — exit-signal de-duplication / suppression

**Problem:** 5,868 intraday long_exit signals (momentum + vwap) — exits firing every
bar regardless of position/order state.

**Fix:** Suppress an exit signal when: no open position exists for (strategy,symbol);
an exit order is already accepted/working; or the symbol is already in a flattening
state. Keep the trading decision identical — only stop redundant signal/skip writes
and redundant order attempts.

**Acceptance:** test proving redundant exits are suppressed while a genuine first exit
still fires.

---

## [x] M6 (P1, Optimization) — intraday cost/slippage edge gate

**Fix:** Add a "don't trade if expected edge < estimated friction" gate for intraday
entries: minimum expected-move threshold + spread/slippage estimate; veto entries whose
modeled edge doesn't clear cost. Tunable; default conservative. Addresses negative
avg-return-despite-decent-win-rate (costs eating the edge).

**Acceptance:** test proving an entry with edge below the friction threshold is vetoed
and a clearly-profitable setup passes.

---

## [ ] M7 (P2, Trade-logic) — trend-ma-cross-20-50 regime/stop review

**Problem:** −2.06% today, ZS −23% worst loser; mixed recent aggregate. Catches weak
continuation / large drawdowns.

**Fix:** Add a regime/trend-strength (and/or volatility) confirmation filter to
MA-cross entries and/or tighten its stop, so it stops entering weak continuations.
Keep it active (don't pause) but gated. Validate it doesn't suppress the genuine
trend signals.

**Acceptance:** test proving a weak-regime MA-cross entry is filtered while a
strong-trend entry passes.

---

## [ ] M8 (P2, Observability) — strategy-health + KPI honesty in the daily report

**Fix:** Extend `schedulers/pg_report_data.py` (and re-install the copy to
`~/.hermes/scripts/`) to emit per-strategy: open-order count, held qty, available qty,
duplicate-symbol ownership, and tradable/paused state; PLUS a "fresh activity vs
reconciliation" split (so reconciled_no_position cleanup isn't read as trading); PLUS
an equity-snapshot-present check that flags loudly if today's snapshot is missing.

**Acceptance:** running the script prints the new sections from the live DB without
error (it snapshots trading.db to /tmp before reading — keep that). Note in the summary
that the `~/.hermes/scripts/` copy must be refreshed (`tr -d '\r'`).
