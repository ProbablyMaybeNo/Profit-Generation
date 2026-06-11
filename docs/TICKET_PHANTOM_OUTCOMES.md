# TICKET: Phantom outcomes — signal-scoped rows booked as fake trades

**Created:** 2026-06-09 · **Priority:** P1 (pollutes Stage 4/8 gate data) ·
**Status:** partially fixed (see "Shipped"), remainder open

## Symptom (evidence)

One GEN position produced TWO closed outcomes on 2026-06-09:

| signal entry | exit_reason | return_pct | reality |
|---|---|---|---|
| 2026-05-20 (filled, 3 sh) | `trailing_stop` | **+2.08%** | the real trade |
| 2026-05-29 (signal only, never filled) | `reconciled_no_position` | **−0.72%** | phantom — no order ever existed |

The phantom's "loss" is fabricated: exit mark (25.62) vs a signal close that
was never an entry price. Multiply across 472 `reconciled_no_position`
Donchian rows and per-strategy win rates / expectancy are unusable.

## Mechanism

1. `monitoring/outcome_tracker.py::open_for_entry` opens an outcome per
   **long_entry signal** (close price as "entry"), with no requirement that
   the signal produced a fill. The same-symbol dedup guard helps but does not
   close the hole (and many historical rows predate it).
2. `monitoring/daily_report.py` F2 pass (`reconcile_signals(..., open_only=True)`)
   did this for every intraday signal nightly.
3. `monitoring/reconcile_positions.py::sweep_orphan_outcomes` closes ANY open
   outcome whose symbol is not held at the broker, at a last-known mark, as
   `reconciled_no_position` — converting unfilled-signal rows into fake
   closed trades.

## Shipped 2026-06-09 (commit pending)

- `reconcile_signals(require_fill=True)` — the intraday F2 pass now only
  opens outcomes for signals with a filled/partially-filled buy in
  `paper_trades`. Without this, Stage 3's signal-only candle-continuation
  fires would have generated dozens of phantom open outcomes per day,
  turning the Stage 0/4 lifecycle verifier RED on bookkeeping noise.
  Tests: `tests/test_outcome_tracker.py` (3 new), full suite 2503 green.

## Shipped 2026-06-11

- **`db.signal_has_fill` / `db.signal_has_any_fill` / `db.mark_outcome_phantom`**
  (data/db.py) — the single source of truth for "did this signal become a real
  position?" and a non-fabricating phantom close (exit_reason='phantom_no_fill',
  exit_price/return/mfe/mae all NULL).
- **Verifier hardened** (scripts/verify_intraday_lifecycle.py) — no-fill outcomes
  classify as `phantom` and are excluded from the Stage 0/4 gate entirely
  (counted, never pass/fail it). The gate now reports real vs phantom; a paused
  signal-only strategy can no longer turn it RED. Item 4-adjacent.
- **Orphan-sweep guard** (monitoring/reconcile_positions.py, item 4) —
  `sweep_orphan_outcomes` now quarantines a no-order outcome as `phantom_no_fill`
  instead of booking it at a fabricated last-known mark. A sell-only orphan (real
  position, buy unlinked) still books, via `signal_has_any_fill`.
- **`phantom_no_fill` added to `CLEANUP_EXIT_REASONS`** (strategy_health.py) so
  quarantined rows drop out of every eligibility/health/stats query.
- **One-time quarantine** (scripts/quarantine_phantom_outcomes.py, item 3) —
  dry-run by default, `--intraday-only` default / `--all` opt-in. Ran
  `--intraday-only --apply`: **256 intraday phantoms quarantined** (181 stale,
  68 reconciled, 5 long_exit, 2 trailing) — all from paused strategies, zero live
  impact. Intraday lifecycle now has 0 no-fill rows.
- DB backed up first to `data/trading.db.bak_phantom_20260611_*`.
- Tests: test_verify_intraday_lifecycle (+3), test_reconcile_positions (real-vs-
  phantom orphan split), test_quarantine_phantom_outcomes (new, 6). Full suite
  green except one pre-existing date-drift flake (test_macro_fetcher).

**1d/other quarantine — DONE 2026-06-11 (Ross's go).** Ran
`quarantine_phantom_outcomes --all --apply`: **2,553 1d/other phantoms
quarantined** (total phantom_no_fill = 2,809; 93 real filled outcomes remain as
the honest dataset). The decision was clear once measured: the 34 phantom
−7.3% rows were not just noise, they were **freezing Donchian out of trading** —
its eligibility basis was n=35/−7.3% → INELIGIBLE. After quarantine it is
n=1/+2.1% → grace (grace_period=True) → **eligible again at reduced size**. So
the quarantine *unblocked* the one live edge rather than risking it (explains the
single real Donchian fill ever + flat P&L). DB backed up to
`data/trading.db.bak_1dquar_20260611_*`. Acceptance criteria below now hold:
no unfilled signal appears as a closed trade with a non-null return; per-strategy
stats reconcile to `paper_trades` fills.

## Remaining work

1. **Decide the outcome model.** Either outcomes are position-scoped (one row
   per filled entry; recommended) or signal-scoped (hypothetical tracking) —
   but then broker reconciliation must NEVER close signal-scoped rows with
   fabricated marks. Today it is an incoherent mix of both.
2. **EOD/1d path:** `daily_report`'s 1d `reconcile_signals` pass still opens
   outcomes for unfilled 1d signals (Donchian fires ~74 entry signals/day,
   nearly all skipped by sizing/eligibility gates). Consider `require_fill=True`
   there too — check what consumes 1d outcome counts first (`min_outcomes`
   eligibility gate, strategy stats, P2 validation) and whether they expect
   signal-tracking semantics.
3. **Backfill hygiene:** tag or quarantine historical phantom rows —
   `outcomes` whose signal has no filled buy in `paper_trades` — e.g.
   `exit_reason='phantom_no_fill'`, `return_pct=NULL`, so they drop out of
   every stats query. ~472 Donchian `reconciled_no_position` rows are the
   bulk. Read-only audit first; migration script + tests after approval.
4. **`sweep_orphan_outcomes` guard:** skip outcomes whose signal has no fill
   (they are not orphan *positions*, they are phantom *rows*) instead of
   booking them at a fabricated mark.

## Acceptance criteria

- A signal that never filled can never appear as a closed trade with a
  non-null return.
- One broker position ⇒ exactly one closed outcome.
- Per-strategy clean-exit stats reconcile with `paper_trades` fills.
- Lifecycle verifier (`scripts/verify_intraday_lifecycle.py`) sessions are
  unaffected by signal-only (observe) strategies.
