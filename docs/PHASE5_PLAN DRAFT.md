# Profit Generation — Phase 5 Plan (DRAFT) — Intraday Trading

> **⚠️ DRAFT.** Rename to `PHASE5_PLAN CURRENT.md` before running
> `/next-milestone` against it. The milestone-builder agent searches for
> the `CURRENT.md` suffix — keeping this file as DRAFT prevents
> accidental autonomous execution before Ross reviews and refines.

Same conventions as Phase 2 / 3 / 4:
- Python interpreter: `py -3.13` for unit tests / scripts. Conda env
  `trading` (Python 3.11) for anything that imports yfinance / alpaca-py.
- Test command: `py -3.13 -m pytest tests/<file>.py`
- Commit style: conventional commits with the standard `Co-Authored-By`
  footer.
- Branch: push directly to `main`.
- Never modify `config/credentials.json`, `data/*.db`, `logs/`.

**Phase 5 theme — Intraday Trading.** Phase 4 closed the daily-bar
end-to-end loop: signals fire after market close, auto-trader submits
paper orders at EOD, fills happen the next morning. That's slow. Phase 5
extends the system to **fire and act on intraday signals during market
hours** — every 15 min the system scans intraday bars (5m / 15m / 1h)
for tracked intraday strategies, records the signals, and the auto-trader
submits paper orders immediately. Same risk gates, same kill-switch, but
trades happen *during* the day instead of only at EOD.

**Why now:** Phase 4 confirmed the EOD lifecycle works (6 real paper
orders fired 2026-05-18 with full safety stack — kill switch, drawdown
throttle, grace period, FK + resolver bugs caught and fixed). The
infrastructure is proven. Adding intraday is a focused extension, not a
greenfield build.

**Key constraint — PDT rule:** Pattern Day Trader regulations restrict
accounts under $25k from making >3 round-trip day trades in a 5-day
window. Alpaca paper is unrestricted, but Phase 5 must build the guard
correctly so live mode doesn't blow it. See 5.4.

**Existing assets we can promote:**
- `strategies/intraday/mean_reversion_intraday.py` (shipped 3.3.2)
- `strategies/orb/orbo.py` (opening-range breakout)
- `strategies/orb/orb_pivots.py` (ORB with pivot levels)

---

## 5.1 Intraday signal pipeline

The existing Intraday schtask (every 15 min) runs `monitoring.intraday_monitor --once` which only *projects* EOD signals. Phase 5.1 extends that scanner to actually fire intraday strategies on intraday bars.

- [x] **5.1.1 Intraday bar loader**
  - **Deliverable:** `backtest/data.py` extended (or new `monitoring/intraday_bars.py`) with `load_intraday_bars(symbols, interval, lookback_bars)`
  - **Acceptance:** loads N most recent bars at the given interval (5m, 15m, 1h) from Alpaca's bars API (paper credentials are fine for data). Caches per (symbol, interval, bar_close_ts) so repeated 15-min schtask fires don't re-fetch the same bar. Tests: cache hit/miss, 5m/15m/1h shape correctness, no-data fallback.
  - **Notes:** yfinance has unreliable intraday data — go through alpaca-py's `StockBarsRequest` for daily AND intraday. Crypto can use existing `crypto_adapter`.
  - **Completed:** 2026-05-18 by milestone-builder · commit 76e1ae2

- [x] **5.1.2 Intraday strategy fire-check**
  - **Deliverable:** `monitoring/intraday_fires.py` mirroring `strategy_fires.check_fires()` but for intraday bar intervals
  - **Acceptance:** iterates over TRACKED_STRATEGIES entries that declare `bar_interval != "1d"`, fetches their bars via 5.1.1, runs each compute_fn, records any fires into `signals` table with the correct `bar_interval`. Idempotent on (strategy_id, symbol, bar_ts, bar_interval) — re-running the same scan doesn't dupe signals. Tests: per-interval iteration, idempotency, fire shape.
  - **Notes:** The existing `monitoring.intraday_monitor` script is for synthesis only. This is a new path that *commits* signals.
  - **Completed:** 2026-05-18 by milestone-builder · commit 553075c

- [x] **5.1.3 Intraday scheduler wiring**
  - **Deliverable:** `schedulers/run_intraday.bat` extended to call the new intraday fire-check after the existing synthesis pass
  - **Acceptance:** during market hours only, the every-15-min schtask now: (a) runs the existing `intraday_monitor --once` synthesis (informational), (b) runs `intraday_fires` to commit intraday signals, (c) immediately invokes the auto-trader for those new signals (next milestone). Tests: full sequence, no-op outside market hours, exit-code propagation.
  - **Completed:** 2026-05-18 by milestone-builder · commit c306d1b

---

## 5.2 Auto-trader intraday processing

Currently `auto_trader.process_signals()` hardcodes `bar_interval='1d'` in its signal SELECT. To act on intraday signals, the path needs to widen.

- [x] **5.2.1 Widen auto_trader signal SELECT**
  - **Deliverable:** `monitoring/auto_trader.py` `process_signals()` accepts a `bar_interval` parameter (default `"1d"` for back-compat), filters the signal SELECT accordingly
  - **Acceptance:** existing daily flow unchanged (callers passing no arg get `bar_interval='1d'`). New callers can pass `bar_interval="5m"`, `"15m"`, `"1h"`. All eligibility/sizing/regime/pyramid logic applies identically. Tests: existing daily tests still pass, new test for 15m signal processing.
  - **Completed:** 2026-05-18 by milestone-builder · commit fc1a4e9

- [x] **5.2.2 Intraday auto-trader trigger**
  - **Deliverable:** new `monitoring/auto_trader_intraday.py` (or extend trigger) called by `run_intraday.bat` after `intraday_fires`
  - **Acceptance:** when invoked, walks today's unprocessed intraday signals (filtered by `auto_trade.intraday_intervals` setting, default `["15m"]`) and processes each through `process_signals(bar_interval=...)`. Records paper trades the same way as the EOD path. Tests: end-to-end synthetic intraday signal → paper order, intraday-disabled config blocks processing.
  - **Completed:** 2026-05-18 by milestone-builder · commit 6c23aaa

- [x] **5.2.3 Flip `skip_intraday_signals` to false (per setting)**
  - **Deliverable:** `config/settings.json` `auto_trade` block extended with `intraday_enabled: false` (default off — safety) and `intraday_intervals: ["15m"]`
  - **Acceptance:** auto_trader_intraday only fires when `intraday_enabled=true`. When false (default), the intraday scan still records signals but no paper orders are submitted — same observe-only pattern as Phase 2 had. Tests: enabled / disabled gating.
  - **Notes:** This is the master kill-switch for intraday trading at the config level. Ross flips it on once the rest of Phase 5 is shipped and validated.
  - **Completed:** 2026-05-19 by milestone-builder · commit 7288f7c

---

## 5.3 Intraday strategy roster

- [x] **5.3.1 Promote `mean_reversion_intraday` to TRACKED_STRATEGIES**
  - **Deliverable:** `monitoring/config.py` extended with the intraday MR strategy declaration; INSERT into `strategies` table
  - **Acceptance:** declaration uses `bar_interval: "15m"`, `active_on: ["SPY", "QQQ", "IWM"]`, `grace_period: true`, `pyramidable: false` (MR shouldn't pyramid). Strategy fires via 5.1.2 on 15-min bars during market hours. Tests: declaration shape, signal generation against a known intraday bar fixture.
  - **Completed:** 2026-05-19 by milestone-builder · commit e8805b1

- [x] **5.3.2 Promote `orbo` (Opening-Range Breakout) to TRACKED_STRATEGIES**
  - **Deliverable:** `monitoring/config.py` + INSERT
  - **Acceptance:** ORBO declares `bar_interval: "5m"`, `active_in_window: ["09:35-10:30 ET"]` (only fires during the opening hour), `active_on: ["SPY", "QQQ", "IWM", "NVDA", "TSLA"]`, `grace_period: true`. New `active_in_window` filter in `intraday_fires` skips signals outside the window. Tests: time-window filter, ORB high/low computation correctness.
  - **Completed:** 2026-05-19 by milestone-builder · commit 3efb112

- [x] **5.3.3 Promote `orb_pivots` to TRACKED_STRATEGIES**
  - **Deliverable:** declaration + INSERT
  - **Acceptance:** same shape as 5.3.2 but with pivot-level confirmation. Tests as above.
  - **Completed:** 2026-05-19 by milestone-builder · commit fb77859

---

## 5.4 PDT (pattern day trader) guard

Critical safety: live capital accounts under $25k can only execute 3 round-trip day trades per rolling 5-day window. Alpaca paper has no limit, but if any intraday strategy goes live we need the guard built and tested first.

- [x] **5.4.1 PDT counter + guard**
  - **Deliverable:** `monitoring/pdt_guard.py` + `auto_trader.py` integration
  - **Acceptance:** tracks closed round-trips per trading day from `paper_trades` table. On any new intraday entry, checks `count_round_trips_last_5_days(account_value)` — if account < $25k AND count >= 3, refuses the trade with `SKIP_PDT_GUARD`. Paper accounts (>= $25k always since 100k start) effectively never trigger but the guard is still computed. Tests: counter math, threshold gating, paper-bypass case, edge case of account exactly at $25k.
  - **Completed:** 2026-05-19 by milestone-builder · commit f7a39a8

- [x] **5.4.2 PDT dashboard surface**
  - **Deliverable:** dashboard Monitor card extended with PDT day-trade counter
  - **Acceptance:** shows "Day trades today: N/3" and "5-day rolling: N/3" (where the "3" is the live restriction, paper shows the same numbers but with a "paper unlimited" subtitle). Tests: counter render.
  - **Completed:** 2026-05-19 by milestone-builder · commit 3a303ca

---

## 5.5 Intraday-specific risk controls

- [x] **5.5.1 Intraday sizing tier**
  - **Deliverable:** `monitoring/sizing.py` extended with `intraday_size_multiplier` (default 0.5 of equivalent EOD size)
  - **Acceptance:** intraday entries are sized at half the EOD-equivalent. Higher turnover means more slippage exposure; the multiplier compensates. Configurable per-strategy via `intraday_size_multiplier` override on the declaration. Tests: math, override path.
  - **Completed:** 2026-05-19 by milestone-builder · commit ea1c893

- [x] **5.5.2 Same-day round-trip cap per symbol**
  - **Deliverable:** `auto_trader.py` extended with `max_intraday_round_trips_per_symbol` setting (default 2)
  - **Acceptance:** when a symbol has already been round-tripped N times today on intraday signals, additional intraday entries on that symbol skip with `SKIP_INTRADAY_SYMBOL_CAP`. EOD signals on the same symbol are unaffected. Tests: per-symbol counter, daily reset, EOD-not-affected.
  - **Completed:** 2026-05-19 by milestone-builder · commit afea761

- [x] **5.5.3 Intraday EOD close-out**
  - **Deliverable:** new task `monitoring.close_intraday_positions` invoked from `run_daily.bat` at 16:00 ET (15:00 PT)
  - **Acceptance:** any open intraday-strategy positions are closed at market close (Alpaca MOC order or fallback to market). EOD strategies' positions stay open. Tracks closed positions and PnL. Tests: identification of intraday-only positions, MOC fallback.
  - **Notes:** Intraday strategies should not hold overnight — that's a different risk profile (gap risk). This forces the discipline.
  - **Completed:** 2026-05-19 by milestone-builder · commit 49ab1e4

---

## 5.6 Dashboard updates

- [x] **5.6.1 Intraday signals card on Monitor**
  - **Deliverable:** `dashboard/index.html` + `/api/state` extension
  - **Acceptance:** new card "intraday signals today" showing the last 20 intraday signals fired today (5m, 15m, 1h) with strategy, symbol, bar_ts, signal_type. Auto-refresh 30s shows new fires as they happen. Tests: API shape, render against fixture.
  - **Completed:** 2026-05-19 by milestone-builder · commit 044d748

- [x] **5.6.2 Intraday paper trades stream**
  - **Deliverable:** dashboard `paper_trades_today` card extended to highlight intraday vs EOD orders
  - **Acceptance:** order type column shows "INTRADAY (15m)" / "EOD (1d)" tag. Sortable. Tests: render against mixed fixture.
  - **Completed:** 2026-05-19 by milestone-builder · commit 044d748

- [x] **5.6.3 Intraday status badge on auto-trader control card**
  - **Deliverable:** mode badge extended
  - **Acceptance:** when `intraday_enabled=true`, badge shows "ACTIVE + INTRADAY". When false, shows existing "ACTIVE". Visually distinguishes the two operating modes so Ross can tell at a glance whether intraday is firing. Tests: render against both states.
  - **Completed:** 2026-05-19 by milestone-builder · commit 08d59a7

---

## 5.7 End-to-end smoke test + first live-paper day

- [ ] **5.7.1 Intraday lifecycle smoke test**
  - **Deliverable:** `scripts/smoke_intraday_lifecycle.py`
  - **Acceptance:** runs a single intraday strategy (mean_reversion_intraday) against a synthetic intraday bar series, exercising: fire-check → signal record → auto_trader entry → MOC close-out at 16:00. Outputs trade-by-trade log + final stats. NOT a unit test — proves the wiring end-to-end. Tests: harness self-tests.

- [ ] **5.7.2 First-day live-paper monitor checklist**
  - **Deliverable:** `docs/INTRADAY_FIRST_DAY.md`
  - **Acceptance:** documents the procedure for flipping `intraday_enabled=true` for the first time — pre-flight checks, what to watch in the first hour, abort criteria, rollback. ≤ 10 procedures, each ≤ 5 steps.

---

## Notes for Phase 6 candidates

(Pushed from the old Phase 5 draft — see `docs/PHASE6_PLAN DRAFT.md`)

- ATR stops generalized across all strategies
- Fractional Kelly sizing
- Breakout-and-retest strategy class
- SAR exit overlay
- Options pyramiding research

Plus:

- Multi-account live (>1 broker)
- LLM-driven daily strategy review
- Realtime websocket fills (replace 15-min polling)
- Futures (after research go/no-go)

## Out of scope for Phase 5

- HFT (sub-minute strategies)
- True realtime tick streaming (15-min polling is the granularity)
- Options intraday strategies (need full options chain infra first — Phase 6)
- Sub-5-min bars (data quality + slippage make this unreliable for retail)
- Margin trading on equity intraday (separate risk surface)
