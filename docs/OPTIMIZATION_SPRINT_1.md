# Optimization Sprint 1 — milestone plan

Derived from `docs/OPTIMIZATION_PLAN.md` + `docs/SYSTEM_DATA_ANALYSIS.md` (2026-06-02).
Execute in order — M1 is foundational (gates measurement), M2 is low-risk config,
M3/M4 share a root cause. Build → test (`py -3.13 -m pytest`) → commit → push per
milestone. Halt and report if a milestone needs an architectural/design decision
or if any non-live test fails. Live-API tests (`-m live`) may be skipped — note it.

Conventions: match existing code style; no new deps without asking; no comments on
unchanged code; trust internal framework guarantees, validate at boundaries.

---

## [x] M0 (P0, CRITICAL — do first) — Fix sub-penny stop_price rejection

**Why:** discovered 2026-06-02 (`docs/INTRADAY_REALITY_CHECK.md`). Alpaca rejects
**100% of protective stop orders** with `{"code":42210000,"message":"invalid
stop_price ... sub-penny increment"}` — 63 submit attempts, 63 rejections, 0 on the
book. Every position (intraday AND EOD) is currently running with **no hard stop**;
only the trailing-stop ratchet and the EOD flatten provide protection. This is a live
risk-management hole, so it goes first.

**Root cause:** `stops.py:145` rounds stop_price to 4 decimals. US equities ≥ $1 must
be priced to 2-decimal ticks (sub-penny only allowed < $1). The 4dp value is rejected.

**Build:** round/quantize stop_price (and any limit price used on stop orders) to a
valid tick — 2dp for price ≥ $1.00, finer only for sub-$1 names. Apply wherever stop
orders are priced (stops.py and any stop/bracket submission in auto_trader). Confirm
with a real paper submission path if feasible (live test may be skipped — note it).

**Acceptance:** unit test proves stop_price for a ≥$1 symbol quantizes to 2dp (e.g.
123.4567 → 123.46) and a sub-$1 symbol keeps finer precision; existing stop tests
green. Suite green. After this lands, hard stops should actually rest on the book.

---

## [x] M1 (P0) — Instrument MFE/MAE/exit_reason + capture intraday outcomes

**Why:** `outcomes.mfe_pct`, `mae_pct`, `exit_reason` are 100% NULL across all 2,048
rows, and every closed outcome is `bar_interval='1d'` — zero intraday outcomes exist.
We cannot measure stop/trailing/pyramid effectiveness without this. It gates every
later tuning decision.

**Touchpoints:**
- `monitoring/outcome_tracker.py` — `close_for_exit()` hardcodes
  `exit_reason="long_exit_signal"`; `reconcile_signals()` defaults `bar_interval='1d'`.
  It already supports `bar_intervals=[...]` (PG-009). 
- `monitoring/close_intraday_positions.py:~212-222` records the closing SELL but
  **never calls `db.close_outcome`** — this is the specific reason zero intraday
  outcomes exist. The only intraday outcome-writer (`reconcile_stop_fills`) keys off
  stop fills that never happen (see M0). Wire outcome-close into the intraday EOD-close
  and the signal/trailing exit paths.
- DB `outcomes` cols already exist: `mfe_pct`, `mae_pct`, `exit_reason`, `bars_held`.
- Bars source: `monitoring/auto_trader._build_default_bars_fetcher` / intraday bars in
  `intraday_bars` table.

**Build:**
1. Compute `mfe_pct` (max favorable excursion) and `mae_pct` (max adverse excursion)
   over the bars between entry_ts and exit_ts when closing an outcome — long: 
   mfe = (max(high)−entry)/entry, mae = (min(low)−entry)/entry. Persist on close.
2. Set `exit_reason` accurately to one of: `stop`, `trailing_stop`, `eod_close`,
   `long_exit_signal` (the exit path already knows which — e.g. trailing-triggered
   exits set `trailing_triggered=True`; EOD closes carry the auto-close note; ATR
   stop exits via stop fills). Thread that reason into `close_for_exit`.
3. Ensure intraday exits are reconciled into `outcomes` — include the intraday
   intervals (`1m`,`5m`,`15m`,`1d-intraday`) in the reconcile path used after each
   intraday run (wire `bar_intervals` through, or add an intraday reconcile call in
   `monitoring/auto_trader_intraday.py`).
4. Optional backfill: extend `scripts/backfill_outcomes.py` to populate mfe/mae for
   historical rows where bars are available (best-effort; don't fail the run if bars
   are missing).

**Acceptance:** new/updated unit tests prove (a) mfe/mae computed correctly from a
synthetic bar series, (b) exit_reason is set per exit type, (c) an intraday
(`bar_interval='1m'`) entry+exit produces a closed `outcomes` row. Full non-live
suite green.

---

## [x] M2 (P0) — Quarantine the negative-edge strategies

**Why:** evidence-backed money-losers. `intraday-1m-orb` (−$56 live, 22% WR; ORB
backtest −12.9% vs SPY +22.6%), `intraday-1m-vwap-reclaim` (negative live + backtest),
`botnet101-consec-bearish` (PF 0.95, −0.06%/trade, n=168). They consume open-position
slots / capital with no demonstrated edge.

**Touchpoints:** pause mechanism already exists — `auto_trader.py:2760` checks
`sh_mod.is_paused(conn, strategy_id)` against the `paused_strategies` table. Find the
helper that writes pauses (the strategy-halt module / `paused_strategies` writer).

**Build:** quarantine the three strategy_ids via the existing pause mechanism (insert
into `paused_strategies` with a reason like "sprint1: negative edge"), NOT by deleting
them — we keep their history for the measurement work in M1. If a CLI/seed exists for
pausing, use it; otherwise add a tiny idempotent helper + a seed entry. Confirm a
paused strategy is skipped in `process_signals` (a `SKIP_PAUSED` path/test).

**Acceptance:** test proves a paused strategy_id yields no entry order. The three IDs
are paused in the DB (or a committed seed/migration script that pauses them). Suite green.

---

## [x] M3 (P1) — Fix the qty<1 veto on high-priced liquid names (the real "price cap")

**Why:** `price_too_high` fired 6,338× in a week (5,139 on `intraday-1m-momentum`) on
SPY/QQQ/NVDA/AVGO/SMH/IWM. ROOT CAUSE (verified): there is NO literal $250 share-price
cap. The skip at `auto_trader.py:597-606` fires when `_calc_qty(close, notional) < 1` —
intraday notional is sized so small (intraday `min_position_usd` floor ~\$250, median
fill \$914) that `floor(notional/price) = 0` for ~\$700+ shares. So the best, most
liquid vehicles are excluded purely because the intraday position is too small to
afford one share.

**Build (choose the cleanest; document the choice):**
- Preferred: enable **fractional shares** for intraday market DAY orders on
  fractionable symbols (Alpaca supports notional/fractional qty), so a \$250 notional
  buys 0.33 SPY instead of skipping. Guard: only when broker supports it for the symbol.
- AND/OR change the veto so it only rejects when the *strategy's real notional cap*
  (`max_position_usd`) can't afford 1 share — `floor(max_position_usd/price) < 1` —
  not the shrunken intraday notional. (This is mostly moot once M4 raises sizing.)
- Do NOT add a share-price ceiling. Do NOT remove the legitimate qty<1 guard for
  genuinely unaffordable cases.

**Acceptance:** test proves a high-priced liquid symbol (e.g. price \$760, intraday
notional \$250) is no longer vetoed (either sized as a fraction, or sized against the
real cap), while a genuinely zero-budget case still skips. Suite green. Note clearly
in the commit which approach was taken and why.

---

## [x] M4 (P1) — Deploy idle capital: scale intraday/position size to edge

**Why:** ~70% of capital idle (\$69.5k cash; median fill \$914 vs \$10k cap). Sizing
is flat across edge tiers; `kelly_quarter × fraction_of_kelly=0.25 ×
max_position_fraction=0.10` compounds three multipliers that crush size, and intraday
is stuck at grace-period half-size because `min_outcomes=30` never clears (no closed
intraday outcomes — fixed by M1). Proven EOD winners (n≥360) should get proportional
capital.

**Touchpoints:** sizing in `auto_trader.py` (`_compute_position_size`/sizing block
~L560-596), `config/settings.json` `auto_trade.tiered` / `kelly` / `intraday`
sizing, intraday `min_position_usd` floor.

**Build:**
1. Raise the intraday position floor/notional so liquid names are tradeable and
   capital is deployed (coordinate with M3 — together they should let SPY/QQQ/etc.
   actually fill at a meaningful size).
2. Scale size to edge: confirm the Kelly path actually clears for the high-n EOD
   winners (`botnet101-3-bar-low`, `4bar-momentum-reversal`); let strategies with
   PF>2.0 size up toward the cap (e.g. raise `kelly.max_position_fraction` modestly,
   or add an expectancy-tiered multiplier) WITHOUT breaching `risk.max_position_usd`,
   `max_open_positions`, `max_open_per_strategy`, or `max_daily_loss_pct`.
3. Keep all existing risk guards intact — this is sizing-up within limits, not
   removing limits.

**Acceptance:** test proves (a) a high-PF/high-n strategy sizes larger than a
thin/low-edge one for the same price, (b) sizing never exceeds the configured caps,
(c) intraday positions size to ≥1 share of liquid names. Suite green. Be conservative
— do not over-leverage on 15 days of live data; flag the assumption in the commit.

---

### Guardrails (apply to all)
- Don't overfit to ~15 days of live data; prefer changes justified by the larger
  backtest/outcome samples.
- Never weaken `risk.*` hard limits or the paper-mode / kill-switch gates.
- If any milestone's correct implementation is ambiguous (esp. M4 sizing policy),
  HALT and report with the specific decision needed rather than guessing.
