# Profit Generation — Phase 5.5 Plan (DRAFT) — Trend Strategy Scanner

> **⚠️ DRAFT.** Rename to `PHASE5_5_PLAN CURRENT.md` before running
> `/next-milestone` against it. Phase 5.5 is a focused insertion between
> Phase 5 (intraday trading, complete) and Phase 6 (ATR / Kelly / etc.).
>
> **Activation gate:** the validator from 2026-05-19 (donchian-breakout-20
> on a 25-symbol universe over 5 years) must show PASS or PASS_WITH_NUANCE
> on at least 60% of symbols. Otherwise the trend edge isn't broad enough
> to justify the scanner infrastructure — drop back to Phase 6.

Same conventions as Phase 2 / 3 / 4 / 5:
- Python interpreter: `py -3.13` for unit tests / scripts. Conda env
  `trading` (Python 3.11) for anything that imports yfinance / alpaca-py.
- Test command: `py -3.13 -m pytest tests/<file>.py`
- Commit style: conventional commits with the standard `Co-Authored-By`
  footer.
- Branch: push directly to `main`.
- Never modify `config/credentials.json`, `data/*.db`, `logs/`.

**Phase 5.5 theme — Trend-only universal scanner.** The current trend
strategies (donchian_breakout_20, ma_cross_20_50, new_high_volume) scan
3 symbols each (SPY/QQQ/IWM). Trend math demands a wide net — the edge
comes from catching the rare parabolic winners, and you need many
attempts to catch them. Phase 5.5 extends ONLY the trend strategies to
~500 symbols (S&P 500 + Nasdaq-100 + high-volume ETFs), keeping
mean-reversion on its tightly-validated subset.

**Why scoped to trend:**
- Trend win rate 30-40% + avg winner 5-10× avg loser = wide net wins
- Mean-reversion is 70%+ win rate with small per-trade edges. Wider
  universe just adds uncalibrated bets there.
- Trailing stops + pyramiding naturally cap risk per signal — the
  mechanics are designed for high-attempt-count scanning.

**Existing assets we leverage:**
- `monitoring/intraday_fires.py` (Phase 5.1.2) — iteration pattern we copy
- `monitoring/auto_trader.py` `process_signals(bar_interval=...)` — already
  supports the wider universe via existing eligibility gates
- `monitoring/trailing_stops.py` (Phase 4.6.1) — engine already does the work
- `monitoring/pyramiding.py` (Phase 4.6.2) — engine already does the work
- Liquidity check via `data/snapshots` historical volume

**Out of scope:**
- Mean-reversion stays on its validated `active_on` subset (KRE, XBI, XHB, etc.)
- Crypto stays separate (own adapter, own constraints)
- No options scanning (Phase 7+)
- No sub-5m intraday scanning (~Phase 7+ if at all)

---

## 5.5.1 Universe loader + refresh

- [x] **5.5.1.1 Static symbol universe**
  - **Deliverable:** `data/universes/sp500.csv`, `data/universes/nasdaq100.csv`, `data/universes/etfs.csv` (manually curated, ~30 high-volume ETFs); `monitoring/universe.py` loader function
  - **Acceptance:** `load_trend_universe()` returns deduped list of ~600 symbols. Source files are in-repo (no API dependency for the universe itself). Tests: dedup correctness, loader handles missing file gracefully (logs warning, continues with what's available).
  - **Notes:** S&P 500 and Nasdaq-100 constituents change quarterly. Initial CSVs are snapshots — add manual quarterly refresh to RUNBOOK rather than auto-fetching (avoids dependency on external API).
  - **Completed:** 2026-05-19 by milestone-builder · commit b0749b2 · 553 deduped symbols (501 S&P + 101 NDX + 36 ETFs; 85 overlaps removed)

- [x] **5.5.1.2 Universe-refresh helper script**
  - **Deliverable:** `scripts/refresh_universe.py`
  - **Acceptance:** scrapes current S&P 500 + Nasdaq-100 constituents (Wikipedia is the canonical free source) and writes the CSV files. Idempotent. Telegram alerts on adds/removes since last run. Tests: parsing logic, diff detection.
  - **Notes:** Run quarterly via manual invocation (or Phase 6+ schtask). Not auto-scheduled at first — let Ross see the diff before applying.
  - **Completed:** 2026-05-19 by milestone-builder · commit 8e71075

---

## 5.5.2 Liquidity filter

- [x] **5.5.2.1 Dollar-volume filter**
  - **Deliverable:** `monitoring/liquidity.py` + `data/db.py` snapshot helper
  - **Acceptance:** `filter_by_dollar_volume(symbols, min_usd=50_000_000)` returns subset where 20-day avg (close × volume) ≥ threshold. Pulls from `snapshots` table (already populated daily). Tests: math correctness, missing-snapshot fallback (exclude conservatively).
  - **Notes:** $50M/day is a reasonable default — keeps liquid mid-caps and above, knocks out micro-caps where fills slip badly. Configurable per-strategy via `liquidity_floor_usd` on the declaration. **Implementation note:** new `liquidity_snapshots` table (purpose-built, instead of extending generic `snapshots` which has no volume column).
  - **Completed:** 2026-05-19 by milestone-builder · commit 74900b3

- [x] **5.5.2.2 Spread estimate filter (optional second guard)**
  - **Deliverable:** extension to liquidity.py
  - **Acceptance:** for symbols with no recent paper trades, fetch a live bid/ask via Alpaca and skip symbols where (ask-bid)/midpoint > 0.5%. Cached for 1 hour to avoid hammering the API. Tests: spread math, cache hit.
  - **Notes:** Belt-and-suspenders. Dollar-volume already correlates with tight spreads, but a few low-volume names slip through.
  - **Completed:** 2026-05-19 by milestone-builder · commit 1ffae12

---

## 5.5.3 Trend scanner module

- [x] **5.5.3.1 Wide-universe trend scanner**
  - **Deliverable:** `monitoring/trend_scanner.py` + `tests/test_trend_scanner.py`
  - **Acceptance:** for each strategy in TRACKED_STRATEGIES where `strategy_class == "trend"`:
    - Load the trend universe via 5.5.1
    - Apply liquidity filter via 5.5.2
    - Fetch 100 daily bars per symbol (batched, ~10 symbols per Alpaca call)
    - Run each strategy's compute_fn on each symbol
    - Record fires into `signals` table with `bar_interval='1d'`
    Idempotent on (strategy_id, symbol, bar_ts, bar_interval). Tests: full pipeline against fixture data, idempotency, liquidity filter integration.
  - **Notes:** Crucially: this BYPASSES the `active_on` field. Trend strategies in `TRACKED_STRATEGIES` keep their narrow active_on for the regular EOD fire-check (which still runs as backup). The wide scan is a separate path.
  - **Completed:** 2026-05-19 by milestone-builder · commit d273a47

- [x] **5.5.3.2 Bar-fetch batching + caching**
  - **Deliverable:** `monitoring/intraday_bars.py` extended (or new `monitoring/wide_bars.py`)
  - **Acceptance:** fetching 600 symbols × 100 bars must complete in < 5 min on a normal EOD run. Batched Alpaca calls (10-20 symbols per request), per-symbol cache scoped to today's bar close. Tests: batch sizing, cache hit when called twice in a 30-min window.
  - **Completed:** 2026-05-19 by milestone-builder · commit 789d0ee · 50 symbols per Alpaca request, 36h TTL keyed to bar-close date

---

## 5.5.4 Signal ranking + capacity allocation

When the scanner fires 30+ signals on a trending day, we can only hold ~10 concurrent positions. Need to pick the best.

- [x] **5.5.4.1 Signal ranker**
  - **Deliverable:** `monitoring/signal_ranker.py`
  - **Acceptance:** scores each fired signal by composite metric:
    - **Regime alignment** (×1.5 if current regime matches strategy's `active_in_regimes`)
    - **Volume confirmation** (×1.3 if today's volume > 150% of 20-day avg)
    - **Recent strategy edge** (×1.0–1.5 based on strategy's all-time sharpe-ish)
    - **Symbol liquidity** (×1.0–1.2 by dollar volume tier)
    Higher score wins when capacity is tight. Tests: scoring math, tie-breaking (lexical by symbol).
  - **Completed:** 2026-05-19 by milestone-builder · commit 2010738

- [x] **5.5.4.2 Capacity-aware order submission**
  - **Deliverable:** `monitoring/auto_trader.py` extended with `max_new_entries_per_day` and per-strategy caps
  - **Acceptance:** when ranked signals > available capacity (10 - currently_open), only top-N by score get submitted. Lower-ranked signals get logged with `SKIP_CAPACITY` reason for transparency. Configurable via `auto_trade.max_new_entries_per_day` (default 5). Tests: capacity gate triggers correctly, top-N selection, skip-reason logged.
  - **Notes:** A scanner that fires 50 signals/day but submits all 50 will overwhelm the account. The capacity cap is what keeps the system disciplined.
  - **Completed:** 2026-05-19 by milestone-builder · commit 75b7286 · signals reordered by ranker score before the loop; counter increments on BUY/DRY_BUY only.

---

## 5.5.5 Scheduler wiring

- [x] **5.5.5.1 EOD scanner trigger**
  - **Deliverable:** `schedulers/run_daily.bat` extended; new schtask step
  - **Acceptance:** the existing 14:30 PT DailyReport schtask, AFTER `monitoring.daily_report` runs (which handles the normal narrow universe), invokes `monitoring.trend_scanner` to scan the wide universe. New trend signals get processed by `auto_trader` in the same pipeline run. Tests: full sequence, exit-code propagation, failure isolation (scanner failure doesn't poison daily report).
  - **Notes:** The scanner is conditional on `auto_trade.trend_scanner_enabled` (default `false`). Same observe-only-first pattern as intraday — code is built, master flag flipped after first-day playbook walkthrough.
  - **Completed:** 2026-05-19 by milestone-builder · commit 89ac532 · wired inside daily_report.main() so run_daily.bat needs no change; maybe_run_trend_scanner() is the test seam.

---

## 5.5.6 Dashboard surface

- [x] **5.5.6.1 Scanner activity card**
  - **Deliverable:** new card on `/research` page + `/api/state` extension
  - **Acceptance:** "Scanner activity" card showing today's wide-universe fires per trend strategy. Each row: strategy, symbol, score, action taken (SUBMITTED / SKIP_CAPACITY / SKIP_INELIGIBLE / SKIP_LIQUIDITY). Auto-refresh 30s. Tests: API shape, render against fixture.
  - **Notes:** On Research not Monitor because it's strategy-level analytics. Monitor still shows the actual paper orders (which include the submitted scanner fires).
  - **Completed:** 2026-05-19 by milestone-builder · commit b605607 · derives action labels at query time (SUBMITTED / SKIP_INELIGIBLE / SKIP_CAPACITY / PENDING); SKIP_LIQUIDITY excluded from rows because filter runs before signals are recorded.

- [x] **5.5.6.2 Scanner-fire indicator on paper orders**
  - **Deliverable:** Monitor `paper_trades_today` card extended with "scanner" tag
  - **Acceptance:** paper trades originating from the scanner (vs. the narrow active_on path) get a visual tag — small "🔍" or "scanner" badge. Tests: render against mixed fixture.
  - **Completed:** 2026-05-19 by milestone-builder · commit acc9b72 · `is_scanner` flag added to /api/state paper_trades_today; Monitor renderer paints a magnifying-glass badge next to the strategy cell on scanner-sourced rows.

---

## 5.5.7 Validation + first-day playbook

- [x] **5.5.7.1 End-to-end scanner smoke test**
  - **Deliverable:** `scripts/smoke_trend_scanner.py`
  - **Acceptance:** exercises the full pipeline against a synthetic universe (20 fake symbols with known bullish breakout setups). Verifies: universe load, liquidity filter applied, fires generated, ranking computed, capacity capping correct, paper orders tagged. Output: pipeline trace log + final stats. Tests: harness self-tests.
  - **Completed:** 2026-05-19 by milestone-builder · commit f2c6e93 · 20-symbol universe (12 breakouts + 6 flats + 2 illiquids); 12 fires → top-5 by ranker → 5 BUYs + 7 SKIP_CAPACITY at cap=5. Flagged: scanner persists bar_ts from df.index which under live Alpaca daily bars is an ISO datetime — auto_trader's bar_ts = asof.isoformat() join breaks. Out of scope for 5.5.7.1; needs a follow-up.

- [x] **5.5.7.2 First-day scanner playbook**
  - **Deliverable:** `docs/TREND_SCANNER_FIRST_DAY.md`
  - **Acceptance:** the procedure for first flip of `trend_scanner_enabled=true`. Includes: pre-flight checks, what to watch in the first hour, signs of trouble (too many fires, illiquid fills, regime mismatch), rollback steps. ≤ 10 procedures, each ≤ 5 steps.
  - **Completed:** 2026-05-19 by milestone-builder · commit b21259b · 10 procedures × 5 steps with structure-check test enforcing the contract.

---

## Notes for Phase 6 candidates

Phase 6 already drafted at `docs/PHASE6_PLAN DRAFT.md`. Phase 5.5 doesn't displace it — runs before it because building the scanner depends on the existing trend infrastructure but doesn't depend on ATR / Kelly / breakout-retest. Phase 6 still happens, just after 5.5.

## Activation gate (REPEATED — important)

**Phase 5.5 only ships if the 5-yr validator from 2026-05-19 shows the trend strategy has wide-universe edge.** Specifically:

- ≥ 60% of tested symbols show PASS or PASS_WITH_NUANCE → BUILD
- 40-60% → REFINE first (adjust thresholds, narrow symbol set, then retest)
- < 40% → DROP scanner, focus on Phase 6 (deepen existing edges instead)

If activated, 5.5 is ~12-15 hours of work split across the 11 milestones above.
