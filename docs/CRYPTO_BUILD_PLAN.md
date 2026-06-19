# BUILD PLAN — "HELIOS" 24/7 Crypto Trend-Follower (FINAL, handoff-ready)

> Lead architect handoff doc. Clean-room build that inherits Profit Generation's (PG) **lessons and ledger discipline**, never its code-by-copy or its state. Author: lead architect. Date: 2026-06-19. Operator: Ross (Los Angeles / Pacific Time). Repo target: **new repo `helios`**, fully isolated from `Profit Generation`. This revision closes the six REQUIRED FIXES from skeptic review: (1) stop-fill-quality/slippage bar, (2) native-trail-vs-self-managed authority + fallback, (3) DRILLED crash-window + naked-position flatten backstop, (4) honest measurement from milestone 0, (5) pinned (non-editable) pg_core + per-module anti-silence proof, (6) explicit CI-green ≠ prod-confirmed for live-only milestones.

---

## 0. Name + Thesis + Anti-Pitfall Discipline (governs every milestone)

**Name:** HELIOS (the sun never sets → 24/7 market).

**Thesis:** A single-owner, narrow-and-deep 24/7 intraday **long-only spot trend-follower** on a small set of the most liquid crypto pairs on **Kraken**, entering only when a three-layer upward-trend gate (1h bias / 15m trend / 5m trigger) aligns, and riding a **protective stop that physically rests on Kraken's matching engine** — the thing PG never once achieved (verified in-repo: `monitoring/trailing_stops.py` is a self-managed DB formula+ratchet engine; `monitoring/crypto_adapter.py` is Alpaca **market-order only**, no native trailing; 0/418 stops ever rested live). The edge claim is unproven until measured live; the **architecture** claim is that we never again build a risk-and-strategy tower on an unverified, un-drilled foundation.

### Binding anti-pitfall rules — a milestone that violates one is REJECTED at the prod-confirm gate, not patched

1. **REAL-VENUE-ONLY EXECUTION PATH.** No `MagicMock`/fake broker on order placement, fills, or rejections — ever. Behavior is proven only by live micro-orders + executions/OpenOrders readback. *(PG: every protection test injected a mock returning "accepted"; green suite hid 0/418 resting stops.)*
2. **STOP-FIRST DEPENDENCY ORDER.** The protective stop is PROVEN to REST, TRIGGER, **and fill within a bounded slippage band** (CHECK A, Stage 0) before any strategy/sizing/trend code exists. Anything consuming a stop price is BLOCKED until CHECK A is PROD-CONFIRMED.
3. **"DONE" = PROD-CONFIRMED, NOT "TESTS PASS."** Every milestone carries a ledger row with an exact runnable production observable. Next milestone cannot start until the prior one is GREEN on the live venue. *(PG merged 13/13 behavior changes LIVE at TESTED-ONLY.)*
4. **ANTI-SILENCE ALARMS ARE MANDATORY.** Any feature proven by the *presence* of output gets a standing Telegram alarm that fires when expected-output count stalls. Silent fallbacks are banned; every fallback emits a LOUD log + alert. A venue read failure = NOT-PROTECTED (loud), never "assume OK." *(PG: chandelier=0 rows, r_multiple=0/3057, regime=1 row — all inert, no alarm.)*
5. **HONEST MEASUREMENT FROM MILESTONE 0.** The position-scoped, `require_fill=True`, **fill-or-no-row** invariant is built into the schema + write path in 0.1, BEFORE the first micro-order. Every row ever written — including Stage 0's $10 orders — is honest. Exchange balance curve is authoritative P&L; outcomes reconcile to it. Phantom rows are structurally impossible, not cleaned up later. *(PG: 95.2% of outcomes were phantom.)*
6. **SINGLE-OWNER AUTHORITY.** Exactly one position manager owns each symbol's full lifecycle. Strategies emit *intents*; they never place/cancel orders. Idempotent submission. *(PG: multi-strategy fights → −$101k accidental short → Donchian-only reset.)*
7. **ONE TRAIL AUTHORITY, NOT TWO (FIX 2).** HELIOS uses **self-managed cancel/replace as the authoritative trail** (a static native stop re-priced by HELIOS each 15m close). Kraken's native auto-trail is NOT run concurrently in v1 — running both races the anchor (double-trailing). Stage 0 explicitly measures Kraken's native auto-trail anchor cadence to decide whether native trail is even viable; the default is self-managed because PG's trail math is already trusted and HELIOS controls the recompute clock.
8. **NARROW & DEEP, EXPAND BEHIND AN EVIDENCE GATE.** Day-1 = ONE strategy, **BTC-only** first live. 2nd symbol/strategy admitted only after ≥20 fresh honest closed R-bearing outcomes of positive expectancy, correlation < 0.3, one-at-a-time, with CHECK A re-run on that symbol's first live stop. *(PG root failure: 30 strategies / 552 symbols / 4 overlays, none validated.)*
9. **HARD ISOLATION FROM PG.** Separate repo, SQLite DB, credentials (Kraken only, NEVER Alpaca), process tree, kill-switch, and capital. HELIOS never opens PG's `trading.db`, never reads PG config, never runs in PG's `\TradingSystem\` tasks. Shared math arrives only via a **pinned, version-tagged** package `pg_core` (shared by value, never shared mutable state).
10. **24/7 SAFETY WITHOUT A MARKET CLOSE.** Independent kill-switch + dead-man are core infra from day 1, **drilled on the live venue** as prod-confirm gates. The crash-between-fill-and-stop window is explicitly drilled, and an automated backstop that **flattens an open naked position** (not merely cancels orders) is proven (FIX 3).
11. **CI-GREEN ≠ PROD-CONFIRMED (FIX 6).** Live-only milestones (0.4, 0.5, 0.5b, 0.6, 0.6b, 3.2, 3.3, 5.2–5.6 drills) can ONLY be confirmed by a captured live API readback artifact. A CI guard FAILS the build if anyone flips a live-only milestone to PROD-CONFIRMED without an attached live readback artifact. Green CI must NEVER be read as prod-confirmed.

---

## 1. Architecture

### 1.1 Venue decision (locked)
**Execution venue = Kraken spot.** Only US-retail-accessible (California-eligible) exchange with native server-side trailing-stop AND stop-loss order types on spot (WS v2 `add_order`, `python-kraken-sdk`). The stop rests on Kraken's matching engine — it triggers even if our process is dead. **Rejected (do not revisit in v1):** Coinbase Advanced (no native trailing → client-side emulation = the exact PG anti-pattern); Gemini (stop-limit only); Binance.US (thin post-crisis liquidity → bad fills for a frequent trader); Bybit/Crypto.com (US-disqualified). PG's `monitoring/crypto_adapter.py` (Alpaca, market-only) is **NOT reused** — confirmed in-repo as disqualified.

**Stop order TYPE decision (FIX 1 — locked):** v1 default = **`stop-loss-limit`** (a stop that, when breached, places a LIMIT, not an unbounded market) with a documented max-slip band (limit offset = `breach_price × (1 − max_slip)`, max_slip default 0.75%). Rationale: a native trailing-**stop** fires a MARKET order into a thin crypto flush and can fill catastrophically far from the level — the same loss class as PG's non-resting stops, hidden behind a green CHECK A. The limit bounds the fill. **Escalation rule (tested in 0.5b):** if the stop-limit does not fully fill within `T_escalate` (default 90s) of trigger, HELIOS converts the residual to a market exit (loud alert) — bounding slippage in normal flushes while guaranteeing exit in a true gap. Self-managed cancel/replace re-prices both the stop trigger and the limit offset on each ratchet.

**Order-placement library:** official **`python-kraken-sdk` directly** (or raw WS v2 `add_order`) for the order critical path. CCXT for market data / secondary only (it has open Kraken trailing-stop param-mapping issues — `stopLossPrice` returning None). Never trust a library abstraction for the stop-placement verification step.

**Sandbox reality:** Kraken has **no public spot testnet**. Mitigation: (a) `validate=True` on `AddOrder` for request-STRUCTURE tests only (this proves shape, NOT behavior); (b) **tiny real micro-orders** ($10–$15 BTC/USD) on the live book for all behavior verification. A 202/accepted is NOT proof — only an `executions`/`OpenOrders` readback counts. Per FIX 6, these are MANUAL live runs, not CI-covered.

### 1.2 TradingView's role (OPTIONAL, additive, non-blocking)
TV = detection enrichment + human visualization + notification only. NEVER in the atomic entry→stop sequence (no SLA, ~5s timeout, 15-alert/3-min burst cap, silent webhook disabling). Own-code engine is ground truth and sole execution controller. Reuse `monitoring/tv_webhook.py` as a **separate HELIOS instance** (own secret, own port, IP-allowlist, writes HELIOS DB only) publishing to an internal queue as a *secondary* confidence signal; the loop never blocks on it. **Deferred until after Stage 3** (own-code-only first). Plan tier when wired: TradingView **Plus** minimum.

### 1.3 Shared core vs new build
- **`pg_core` (new, pure: no I/O, no DB handle, no keys, no broker import):** candle pattern detectors (verbatim from `candle_patterns.py`); trend `compute_fn` from `candle_continuation.py` (ET-RTH `active_windows` passed full-day for 24/7); trailing-stop **formula+ratchet** from `trailing_stops.py` (`compute_atr_trail_stop`, `compute_chandelier_stop`, `compute_percent_trail_stop`, `compute_stop`, `ratchet`, `should_exit_on_trailing_stop`) — **note: these emit `round(..., 4)`; HELIOS must re-round to Kraken's per-pair tick, not 4dp**; `compute_atr`/`compute_atr_wilder` from `stops.py`; sizing/initial-stop math from `sizing.py` refactored so DB-reading helpers become injected callbacks; portfolio-heat **formula** taking `(entry, qty, stop)` tuples; R-multiple/return close-math; `cap_sell_qty` **re-typed to float**; reintroduction pure scorers (`_pearson`, `evaluate_evidence/correlation/one_at_a_time`). pg_core depends on Protocols (`BrokerAdapter`, `StopStore`, `ReturnsReader`), not implementations.
- **pg_core distribution (FIX 5 — locked):** consumed by HELIOS **only as a pinned git TAG or a vendored frozen copy with a recorded commit hash**. **Editable installs (`pip install -e`) are BANNED for any paper/live run** — an edit to PG's working tree silently changing HELIOS behavior is the exact shared-mutable-state failure this plan forbids. The HELIOS start-banner logs the resolved `pg_core` version/commit hash; a mismatch vs the pinned manifest is a startup HALT.
- **Ported-but-inert risk (FIX 5):** the chandelier/sizing/regime modules being lifted are the SAME ones that silently produced 0 rows in PG (chandelier=0, r_multiple=0/3057, regime=1). Purity ≠ it fires. **Each ported module gets its own anti-silence alarm wired in HELIOS proving it produces rows in prod** (see 2.1, 3.3, 4.1 alarms). "Pure therefore correct" is banned reasoning.
- **PG consumes pg_core** by repointing internal imports to the same pinned tag — PG's DB/config/keys/process untouched. (Alternative per open-question: HELIOS vendors a frozen copy and PG is not touched at all.)
- **Built fresh in HELIOS:** Kraken adapter (python-kraken-sdk, fractional qty, REAL min-notional/lot/tick filters); 24/7 asyncio daemon; HELIOS persistence (own SQLite, REAL qty, crypto maker/taker fee fields, **with the require_fill / fill-or-no-row invariant baked in at schema creation per FIX 4**); single-owner position manager (float reservation ledger); crypto fee/slippage model in return_pct/R; crypto-tuned risk caps (BTC-beta correlation cluster gate); safety stack (drawdown halt, kill-switch, dead-man, naked-position flatten backstop, weekend caps, anomalous-data guard).

### 1.4 24/7 daemon (single long-lived asyncio process — NOT scheduled tasks)
PG's Windows-Scheduled-Task model cannot do 24/7 (cold-start misses signals, WS rebuilt each run, no persistent state to notice an unstopped position). HELIOS = one long-lived process, state→SQLite after every change, ~7 supervised tasks under `asyncio.gather` (each try/except-wrapped so one crash doesn't kill the process): `ws_manager` · `signal_loop` · `order_monitor` (REST-confirms fills + stop placement, never trusts WS alone) · `heartbeat_loop` (10s app ping, force reconnect if pong stale >30s) · `safety_watchdog` (drawdown / per-position / dead-man / **naked-position sweep**) · `telegram_reporter` (queued, non-blocking) · `healthcheck_server` (localhost /health for UptimeRobot).

### 1.5 Hosting & process management
- **Dev + paper:** local box (zero cost, fast iteration).
- **Live capital:** **Frankfurt/Amsterdam VPS** (Hetzner/DO/Vultr, ~$6–12/mo, static IP for key allowlisting, systemd) the day before real money goes on. Latency irrelevant for candle-close entries; stability is everything. Local box = manual-override station. **Single-region VPS is a single point of failure (FIX 3 caveat):** the naked-position backstop (5.4b) and venue-side dead-man partially mitigate, but a VPS+watchdog co-failure is an accepted, monitored v1 residual risk (multi-region is v2).
- Process manager: **systemd** (`Restart=on-failure`, `RestartSec=10`, `StartLimitBurst=5`/`StartLimitIntervalSec=300`, `OnFailure=helios-alert.service` → Telegram). Local Windows dev: NSSM.

### 1.6 Hard isolation guarantees (PG ↔ HELIOS)
| Boundary | PG | HELIOS |
|---|---|---|
| Repo | `Profit Generation` | new `helios` repo, own git history/venv |
| DB | `data/trading.db` | own SQLite, fractional qty, crypto fees, fill-or-no-row invariant |
| Keys | `config/credentials.json` (Alpaca) | own `credentials.json` (Kraken) — **never Alpaca** |
| Process | `\TradingSystem\` scheduled tasks | single systemd daemon on VPS |
| TV webhook | PG instance/secret/port | own instance/secret/port → HELIOS DB |
| Capital | Alpaca account | dedicated Kraken account, separate funds |
| Kill-switch | PG's | independent, own file path + token |
| pg_core | repointed imports @ pinned tag | pinned tag / vendored frozen copy (no `-e`) |

A halt or failure in either system cannot touch the other.

### 1.7 Data-flow diagram (text)
```
            ┌──────────────────── KRAKEN (execution + market data) ────────────────────┐
            │ WS v2: book + 5m OHLCV | add_order (market entry + stop-loss-LIMIT)        │
            │ executions channel     | OpenOrders / position REST (truth)                │
            │ CancelAllOrdersAfter (dead-man: cancels ORDERS, not positions)             │
            └───▲────────────────────────────▲────────────────────────────▲─────────────┘
                │ OHLCV/ticks                 │ orders                       │ REST sync
   (OPTIONAL)  ┌┴───────────┐  queue   ┌──────┴───────┐  intents   ┌────────┴──────────┐
  TradingView ─► tv_webhook │ ─(2ndary)─► signal_loop │ ──intent──► │ POSITION MANAGER  │
  (deferred)  │ (HELIOS inst│          │ (own-code    │ ◄fill/stop─ │ entry→stop-limit→ │
             └─────────────┘          │  3-layer)    │   confirm   │ trail→exit, idemp │
                                       └──────┬───────┘             └─────────┬─────────┘
   pg_core (PINNED tag, pure): candle_patterns · trend compute_fn · ATR/      │ writes (require_fill)
   chandelier/ratchet · sizing/Kelly · heat · R-math · reintro scorers ───────┤
                                                                              ▼
            ┌────────────── HELIOS SQLite (REAL qty, fees, fill-or-no-row) ─────────────┐
            │ signals · trades(fills) · outcomes(position-scoped, R) · trailing_stops · meta│
            └──────────────────────────────────▲────────────────────────────────────────┘
                                                │ reconcile / state
  order_monitor ─ heartbeat ─ safety_watchdog(+naked-position sweep) ─ telegram ─ /health
                                                │
  external watchdog (separate proc) ─► on 120s silence: cancel-all ORDERS + REST-FLATTEN naked POSITIONS
  UptimeRobot ─► /health (5min)      Telegram (INFO quiet · WARNING auto-heal · CRITICAL human)
```

---

## 2. Validation-Ledger Discipline (applied to EVERY milestone)

Every milestone ships a ledger row; it is **NOT done** until its PROD-CONFIRM LINE is captured GREEN on the live venue. Template:

| Field | Content |
|---|---|
| **ID / WHAT / WHY / FILES** | id, one-line change, gap it closes, where it lives |
| **HOW (build+test)** | what was built + integration test hitting the REAL venue (NO MagicMock for placement/fills/rejections) |
| **EXPECTED** | precise real-world behavior (numbers, not "works") |
| **PROD OBSERVABLE (exact)** | the EXACT runnable check: DB query + expected count, OR Kraken API readback (e.g. OpenOrders shows ordertype, qty==fill qty, tick-valid price), OR a log line incl. deployed commit hash. Runnable against the REAL schema. |
| **FAILURE SIGNATURE** | what broken looks like — **including the silent/inert mode** ("0 rows, no error") |
| **ANTI-SILENCE ALARM** | standing Telegram alert when expected-output count stalls (required for any presence-of-output feature; "N/A" only if silence is impossible) |
| **ROLLBACK** | documented safe state to revert to (+ footguns) |
| **CONFIRM CLASS (FIX 6)** | `CI-OK` (automated suite suffices) OR `LIVE-ONLY` (requires captured live API readback artifact; CI-green is NOT acceptance) |
| **STATUS** | UNBUILT → TESTED-ONLY → PROD-CONFIRMED |
| **PROD-CONFIRM LINE** | date + session + captured observable value (incl. live readback artifact path for LIVE-ONLY) proving EXPECTED on prod; this flips STATUS |

**PROD-CONFIRM GATE between every milestone:**
- **Pre-flight P0:** deployed process runs THIS commit (`git rev-parse HEAD` == deployed tree; start-banner logs commit hash **and pinned pg_core hash**). Stale process / drifted pg_core invalidates everything below.
- Next milestone may not start until prior is PROD-CONFIRMED.
- A FAIL is a **HALT**: roll back to documented safe state, record the FAIL as data, re-plan. **Never patch-on-top-and-continue** (that act IS the doom loop).
- A check that couldn't be exercised (no qualifying fill) is **NOT-EXERCISED**, never PASS.
- **CHECK A (stop rests AND triggers AND fills within slip band) is the MASTER GATE.** Until PROD-CONFIRMED, every stop-consuming change stays BLOCKED.
- **CI guard (FIX 6):** any `LIVE-ONLY` milestone flipped to PROD-CONFIRMED without an attached live readback artifact FAILS CI.

---

## 3. Staged Milestones (milestone-builder-ready)

> Each milestone is small and independently confirmable. The milestone-builder expands each into the full table before building.

### STAGE 0 — PROVE THE STOP RESTS, TRIGGERS, FILLS WELL, AND CAN'T LEAVE A NAKED POSITION (MASTER GATE)

**0.1 — Repo + isolation skeleton + HONEST SCHEMA FROM LINE ONE (FIX 4).** New `helios` repo, own venv, own SQLite. **Schema enforces the fill-or-no-row invariant now:** `outcomes` rows are position-scoped, `filled` defaults absent/NOT NULL, write path is `require_fill=True`, a CHECK/trigger forbids inserting an outcome with `filled=0`. Own `credentials.json` loader (Kraken only). Start-banner logs commit hash + pinned pg_core hash. *PROD OBS:* daemon start log shows `git rev-parse HEAD` == deployed tree AND resolved pg_core hash == pinned manifest; `SELECT count(*) FROM outcomes WHERE filled=0` returns **0** and an attempted phantom insert is rejected by the DB. *Failure:* phantom insert succeeds / pg_core hash drifts. *Alarm:* N/A (bootstrap). *Confirm class:* CI-OK. *Rollback:* delete repo.

**0.2 — Kraken read-only connectivity.** python-kraken-sdk auth; balances, server time, BTC/USD filters (tick/lot/min-notional). *PROD OBS:* log prints real balance + BTC/USD min-notional + tick size. *Failure:* auth error or silent empty filter dict. *Alarm:* "Kraken read failed N min." *Confirm class:* LIVE-ONLY. *Rollback:* none (read-only).

**0.3 — Structural order validation.** Build `add_order` for market BUY + `stop-loss-limit`; submit `validate=True`. *PROD OBS:* Kraken returns validation success for both shapes; log captures exact JSON. **Note:** this proves STRUCTURE only, never behavior. *Failure:* validation rejects (wrong field/tick). *Alarm:* N/A. *Confirm class:* CI-OK (structure only). *Rollback:* none.

**0.4 — ★ CHECK A part 1: stop RESTS on the live book.** Real tiny market BUY ($10–$15 BTC/USD), confirm fill via `executions`, then place `stop-loss-limit` (tick-valid trigger + bounded limit offset). Query `OpenOrders` + subscribe `executions`. *PROD OBS:* OpenOrders shows `ordertype='stop-loss-limit'`, `qty == settled fill qty`, tick-valid trigger & limit, `triggered=False` (resting); AND `SELECT count(*) FROM outcomes WHERE filled=0` still **0** (FIX 4 holds for the live order). A 202 is NOT proof. *Failure:* order absent, qty=0, or only a 202 (the PG signature). *Alarm:* "expected resting stop, OpenOrders shows 0." *Confirm class:* LIVE-ONLY (artifact = OpenOrders JSON). *Rollback:* cancel, sell dust, HALT.

**0.5 — ★ CHECK A part 2: stop TRIGGERS & fills.** With the tiny position + resting stop-limit, set the trigger tight enough that normal volatility crosses it; observe trigger→fill. *PROD OBS:* `executions` shows triggered→filled; position flat; outcome row written position-scoped with realized R from a value the live path wrote. *Failure:* price crossed but no trigger (server-side stop inert) — the catastrophic PG class. *Alarm:* "stop crossed price but no trigger in N min." *Confirm class:* LIVE-ONLY (artifact = executions JSON). *Rollback:* market-flatten manually, HALT.

**0.5b — ★ CHECK A part 3: FILL-QUALITY / SLIPPAGE BAR (FIX 1).** From the 0.5 trigger, capture realized trigger→fill slippage vs the trigger level, and exercise the escalation-to-market path on a deliberately un-fillable limit (set limit beyond the book so it can't fill, confirm HELIOS converts residual to market after `T_escalate`). *PROD OBS:* logged `slippage_pct = (trigger_level − avg_fill)/trigger_level` is within the documented band (default ≤ 0.75% in normal conditions); escalation fires and fully exits within `T_escalate + N`s; both captured from `executions`. *Failure:* fill 8% past level with no alarm (= PG's loss class behind a green CHECK A) / escalation never fires → naked residual. *Alarm:* "stop fill slippage > X% of trail distance" (standing) + "escalation-to-market did not complete." *Confirm class:* LIVE-ONLY. *Rollback:* widen/limit band or switch trigger type, HALT until bounded.

**0.5c — ★ NATIVE-TRAIL vs SELF-MANAGED authority decision (FIX 2).** On a tiny position, place Kraken's **native** trailing-stop and observe whether/when its server-side anchor actually ratchets up as price rises (capture the update cadence). Compare against HELIOS's intended 15m cancel/replace recompute. *PROD OBS:* documented finding: native anchor cadence (e.g. updates every tick / on a delay / not at all) recorded as an artifact; explicit written decision = **self-managed cancel/replace is authoritative; native auto-trail is OFF in v1** (default), OR native is adopted only if its cadence meets/beats 15m. Confirm the two are NEVER run together for one symbol. *Failure:* both trails active → racing anchors / double exit. *Alarm:* N/A (one-time decision drill). *Confirm class:* LIVE-ONLY (artifact = cadence log + decision note). *Rollback:* default to self-managed (the safe choice).

**0.6 — Native dead-man backstop (ORDERS).** Wire Kraken `CancelAllOrdersAfter` with a daemon-refreshed token. **Documented limitation: this cancels resting ORDERS only; it does NOT flatten an open spot POSITION** (FIX 3). *PROD OBS:* with a throwaway resting limit order, let the token lapse → Kraken cancels all resting orders after timeout. *Failure:* orders survive past timeout. *Alarm:* "dead-man token not refreshed." *Confirm class:* LIVE-ONLY. *Rollback:* manual cancel-all.

**0.6b — ★ CRASH-WINDOW DRILL + NAKED-POSITION FLATTEN BACKSTOP (FIX 3).** Build the external watchdog so that on daemon silence >120s it does BOTH: (a) REST cancel-all ORDERS, AND (b) **REST query open spot POSITIONS and market-flatten any position lacking a resting protective stop** (this is what `CancelAllOrdersAfter` and a bare cancel-all canNOT do). Then **DRILL the exact failure**: induce a crash in the millisecond after a tiny BUY fills and before the stop is placed; observe the watchdog detect the naked long and flatten it. *PROD OBS:* `executions` shows the watchdog's market-sell flattening the naked position within 120s + N; CRITICAL Telegram fired; outcome row honest. *Failure:* naked long persists unprotected (the precise gap the draft asserted but never drilled). *Alarm:* "naked position detected with no resting stop" (standing). *Confirm class:* LIVE-ONLY (artifact = executions JSON of the backstop flatten). *Rollback:* none — this IS the backstop; HALT all live work until green.

> **GATE: Stage 0 must be fully PROD-CONFIRMED. No strategy/sizing/trend code may begin until 0.4 + 0.5 + 0.5b + 0.5c + 0.6b are GREEN with captured live artifacts.**

### STAGE 1 — 24/7 MONITOR + DATA FEED (no trading)

**1.1 — WS market-data manager.** One persistent WS, 5m OHLCV for BTC (ETH/SOL added later), 10s app-ping, last_pong tracking, exp-backoff reconnect (1→2→4→8→16→30s + jitter), resubscription cache, **REST snapshot on every reconnect**. *PROD OBS:* continuous 5m bars logged 24h, zero unflagged gaps; forced `ws.close()` reconnects <60s + triggers REST sync (log line). *Alarm:* "no bar for symbol X in >7 min." *Confirm class:* CI-OK for reconnect logic; LIVE-ONLY for the 24h continuity capture. *Rollback:* single-symbol BTC feed.

**1.2 — Bar aggregation (15m, 1h from 5m).** Causal, no-lookahead. *PROD OBS:* aggregated 1h bar matches Kraken native 1h OHLCV within rounding for 10 sampled bars. *Alarm:* "aggregation drift > tolerance." *Confirm class:* CI-OK. *Rollback:* fetch native HTF from REST.

**1.3 — /health + UptimeRobot inverted heartbeat.** /health + 30-min "I AM ALIVE" Telegram (equity, open positions, last signal time). *PROD OBS:* UptimeRobot fires a Telegram alert within 5 min when /health is killed; heartbeat arrives every 30 min. *Alarm:* the inverted heartbeat IS the alarm. *Confirm class:* LIVE-ONLY. *Rollback:* none.

### STAGE 2 — UPWARD-TREND DETECTION (own-code, no execution)

**2.1 — Indicator engine (pg_core pure fns + per-module anti-silence (FIX 5)).** EMA9/21/50 (1h+15m), ADX14 (1h), ADX7 (5m), RSI14 (5m), VWAP anchored UTC 00:00 (≥4h buildup before gating), ATR10 (15m), 4-bar rolling high (5m). Each ported pg_core module wired to a standing alarm proving it fires (produces values/rows), not just imports clean. *PROD OBS:* computed BTC indicators match a TradingView readback within tolerance at 5 timestamps; each ported module logs a non-null value within the first N bars. *Failure (inert):* a ported module silently emits nothing (PG's exact disease). *Alarm:* "indicator NaN/stall" + per-module "ported module X produced 0 outputs in 24h." *Confirm class:* CI-OK + LIVE-ONLY for the live no-zero-rows proof. *Rollback:* disable affected indicator.

**2.2 — Three-layer trend state machine (detection only, places nothing).** Implements §4 rules; writes detected-but-not-executed intents to `signals`. *PROD OBS:* for a known BTC uptrend, logged `uptrend=True` windows match the chart; signals carry full gate breakdown. *Failure (silent):* always-False gate → 0 signals. *Alarm:* "0 trend signals across symbols in 24h during trending tape." *Confirm class:* LIVE-ONLY (needs real trending tape). *Rollback:* loosen ADX gate to log-only.

### STAGE 3 — THE TRAILING-STOP TRADER (single-owner execution)

**3.1 — Single-owner position manager + idempotent entry.** One owner per symbol; strategies emit intents; idempotent submission (dedupe on bar_ts+symbol). Float qty throughout; tick/lot rounded to Kraken per-pair (NOT pg_core's 4dp). *PROD OBS:* conflict-regression test (two intents, same symbol+bar) yields exactly ONE position; OpenOrders shows no double-entry. *Alarm:* "position qty != expected after entry." *Confirm class:* CI-OK for dedupe; LIVE-ONLY for the on-book single-entry proof. *Rollback:* BTC-only single owner.

**3.2 — Atomic entry→protective stop-limit (with crash-window covered by 0.6b).** On fill confirmation (REST, not WS-only), immediately place `stop-loss-limit` at `2.0×ATR10` trigger + bounded limit offset; **assert via OpenOrders the stop rests before treating the trade as protected.** The fill→stop gap is backstopped by the DRILLED 0.6b naked-position flatten, not merely asserted. *PROD OBS:* every live entry has a resting stop-limit within N s, qty==fill qty (DB: trades with a matching trailing_stops row = 100%). *Failure:* the PG signature — entries with no resting stop. *Alarm:* "filled entry with no resting stop after N s" (this also arms the 0.6b backstop). *Confirm class:* LIVE-ONLY. *Rollback:* market-flatten the unprotected position, HALT.

**3.3 — Self-managed chandelier trail-up (ratchet; authoritative per 0.5c/FIX 2).** Each 15m close, compute chandelier (`highest_close(10) − 2.5×ATR10`); **cancel+replace** the stop-limit only if the new trigger is higher (never down), re-pricing the limit offset too. Native auto-trail stays OFF. **20-order-limit guard (gap fix):** cancel+replace momentarily holds 2 orders for one symbol; enforce a per-symbol replace mutex and pause new entries if concurrent resting orders approach 15 (well under Kraken's 20). **Trail-state reconciliation (gap fix):** after each replace, read OpenOrders back and write the resting trigger into HELIOS `trailing_stops` so the DB never diverges from venue truth. *PROD OBS:* trailing_stops rows show monotonically rising triggers on a trending position; each row's `stop_price` equals the resting OpenOrders trigger (reconciled); cancel/replace confirmed on Kraken. *Failure (inert):* 0 chandelier updates ever (PG's exact inert-flip bug) / DB trigger diverges from venue. *Alarm:* "no trail advance on a position open >N bars in uptrend" + "trailing_stops DB vs OpenOrders mismatch." *Confirm class:* LIVE-ONLY. *Rollback:* fall back to the initial static stop-limit offset (loud log).

### STAGE 4 — HONEST MEASUREMENT (invariant already live since 0.1)

**4.1 — Position-scoped outcomes + R-multiple (closes the loop; schema invariant from 0.1).** One row per FILLED entry, `require_fill=True`, R = realized PnL / initial-stop risk, **fees baked into return_pct/R**. *PROD OBS:* `SELECT count(*) FROM outcomes WHERE filled=0` returns **0** (has held since 0.4); every closed outcome has non-null R from a value the live path wrote. *Failure:* phantom rows / R null (PG's 95.2% / 0-of-3057). *Alarm:* "r_multiple null after a closed position" + "phantom (no-fill) outcome detected." *Confirm class:* LIVE-ONLY. *Rollback:* disable outcome writes, investigate.

**4.2 — Equity-curve reconciliation.** Daily: outcome-derived P&L reconciles in sign+magnitude with Kraken balance delta. *PROD OBS:* daily reconcile log shows |outcome P&L − exchange P&L| within fee tolerance. *Alarm:* "P&L reconcile mismatch > tolerance." *Confirm class:* LIVE-ONLY. *Rollback:* flag the day's data untrusted.

### STAGE 5 — SAFETY STACK (drilled on the live venue)

**5.1 — Pre-send size cap + per-position risk.** Risk 0.5% equity/trade; reject pre-send if notional > cap (default 20% account notional/position). *PROD OBS:* an over-cap intent is rejected pre-send (log + Telegram), no order hits Kraken. *Alarm:* "order rejected: over cap." *Confirm class:* CI-OK (pre-send logic) + LIVE-ONLY (no order on book). *Rollback:* lower cap.

**5.2 — Global drawdown halt.** ≥5% DD → pause new entries + alert; ≥10% → flatten all + cancel all + set HALTED flag (atomic+verified persisted write), URGENT Telegram, **no auto-resume**. *PROD OBS:* set equity 10% below peak in state → daemon halts + flattens + CRITICAL within one tick; HALTED flag survives restart. *Failure:* PG's swallowed atomic_write (halt didn't persist). *Alarm:* the CRITICAL itself. *Confirm class:* LIVE-ONLY (drilled). *Rollback:* manual SSH flag reset.

**5.3 — File kill-switch (independent).** KILL_SWITCH file (via SSH/phone or Telegram `/kill`) → next tick flatten + cancel + clean exit; survives restarts. *PROD OBS:* create file → flatten observed on Kraken + clean exit; persists. *Alarm:* "kill-switch engaged." *Confirm class:* LIVE-ONLY (drilled). *Rollback:* delete file.

**5.4 — Dead-man watchdog (separate process; ORDERS).** Daemon writes `last_alive_at` every 30s; separate watchdog (systemd timer, 2-min) fires CRITICAL + REST cancel-all ORDERS on stale >120s. Layered with native CancelAllOrdersAfter (0.6). *PROD OBS:* kill daemon → watchdog fires alert + cancel-all within 2 min (drilled). *Alarm:* the CRITICAL itself. *Confirm class:* LIVE-ONLY. *Rollback:* none.

**5.4b — Naked-position flatten in the watchdog (FIX 3, built/drilled in 0.6b, formalized here).** The 5.4 watchdog ALSO REST-queries open POSITIONS and market-flattens any lacking a resting stop. *PROD OBS:* re-drill the 0.6b scenario on the VPS deployment; capture the flatten. *Failure:* watchdog cancels orders but leaves a naked long. *Alarm:* "naked position flattened by watchdog" (every fire = CRITICAL). *Confirm class:* LIVE-ONLY. *Rollback:* none — this IS the backstop.

**5.5 — Anomalous-data + state-drift guard.** Discard candles >20% from prev close; suppress entries if spread >5× normal; if REST position state disagrees with in-memory → HALT entries + alert. *PROD OBS:* inject a 25%-off candle → discarded + alerted, no signal; induce drift → entries halt. *Alarm:* "bad tick discarded" / "state drift detected." *Confirm class:* CI-OK for guards + LIVE-ONLY for the drift-halt drill. *Rollback:* widen thresholds (loud).

**5.6 — Weekend regime.** Fri 20:00 → Sun 20:00 UTC: ADX gate 22→28, size ×0.5, no new tier-2 entries. *PROD OBS:* weekend log shows tightened gate + halved size on a live weekend. *Alarm:* "weekend regime not applied on Sat/Sun." *Confirm class:* LIVE-ONLY. *Rollback:* disable (loud).

### STAGE 6 — SCALE SYMBOLS (one at a time, behind the evidence gate)

**6.1 — Reintroduction evidence gate (pure scorers from pg_core).** Wire `evaluate_evidence/correlation/one_at_a_time` to HELIOS DB + owner/health helpers. *PROD OBS:* gate REJECTS a candidate with <20 fresh closes or correlation ≥0.3 (unit + live data). *Alarm:* "gate inert (0 evaluations)." *Confirm class:* CI-OK + LIVE-ONLY for the live-data rejection proof. *Rollback:* manual approval only.

**6.2 — Add ETH, then SOL, then one alt at a time** (XRP/AVAX/LINK/ADA/DOGE), each only after ≥20 fresh honest closed positive-expectancy R-outcomes + correlation <0.3 + a probation window + **re-running CHECK A (0.4/0.5/0.5b) on that symbol's first live stop.** **Max 3 concurrent positions; correlation cluster gate (BTC / ETH / all-alts-share-one → max 1 alt open).** *PROD OBS per addition:* CHECK A green for that symbol (artifact captured); gate evidence captured. *Alarm:* per-symbol "no resting stop." *Confirm class:* LIVE-ONLY. *Rollback:* remove the symbol, revert to prior set.

---

## 4. Symbol + Strategy v1 Spec (concrete)

**Universe (v1):** First live = **BTC/USD only.** Then ETH/USD, then SOL/USD (evidence-gated). Tier-2 (one at a time post-evidence, ≥$50M Kraken 7d volume + spread <0.15%): XRP, AVAX, LINK, ADA, DOGE. Cap 8 charted symbols; pause new entries if concurrent resting orders approach 15 (Kraken limit 20; cancel/replace transiently holds 2/symbol — handled in 3.3).

**Timeframe cascade:** 1h bias · 15m trend gate · 5m entry trigger. (No session open → no ORB.)

**Layer 1 — 1h bias (ALL must hold for longs):** EMA9>EMA21>EMA50 AND close>EMA50 AND ADX14≥22. Any fail → NO-TRADE long. Reset: EMA9<EMA21 on 1h → exit at next 15m close, reset to NO-TRADE.

**Layer 2 — 15m gate (both):** EMA9>EMA21 (15m) AND price>VWAP (anchored UTC 00:00, ≥4h buildup). HH/HL 4-pivot = optional confirmatory.

**Layer 3 — 5m trigger.** Primary (pullback-continuation): on L1+L2 uptrend, wait for 5m pullback within 0.3% of EMA21, require EITHER bullish engulfing/hammer closing above EMA21 OR RSI14 re-cross above 50. **Enter on the CLOSE** of the confirming bar. Secondary (breakout): L1+L2 uptrend AND consolidation (ADX7<20 for ≥3 bars on 5m), enter on 5m close above the 4-bar rolling high with volume >1.5× the 20-bar avg. Avoid entries 23:00–00:00 UTC (thinnest liquidity).

**Stops/trail (load-bearing):** Initial protective = `entry − 2.0×ATR10` (15m) placed as a **native Kraken `stop-loss-limit`** (trigger + bounded limit offset, max-slip band per FIX 1, escalate-to-market on un-fill) immediately on fill, then **assert it rests** (CHECK A discipline) with the 0.6b naked-position backstop covering the gap. Trail = **self-managed** Chandelier on 15m: `highest_close(10) − mult×ATR10`, cancel+replace only when higher (ratchet up, never down), DB reconciled to OpenOrders. Native auto-trail OFF (FIX 2 / 0.5c). Multipliers: BTC/ETH normal 2.0–2.5×; alts 2.5–3.0×; any asset daily ATR%>7% → cap 3.0×. **ATR period = 10.** All stop prices re-rounded to Kraken per-pair tick (NOT pg_core's 4dp `round`).

**Sizing & risk:** 0.5% account risk/trade (`size = (equity×0.005)/stop_distance`); single-position cap 20% notional; if ATR>2× its 20-bar avg, halve or skip. **Max 3 concurrent positions.** Correlation cluster cap → max 1 alt open at a time in v1.

**Emergency kill rule (in-strategy):** BTC drops >5% in any 15m bar → halt all new entries 4h + tighten all trailing triggers to 1.5×ATR.

**MANDATORY first-live action:** before any strategy size, run CHECK A (0.4/0.5/0.5b) on a real $10 Kraken order — confirm `stop-loss-limit` rests, triggers, fills within the slip band, and the naked-position backstop flattens an induced crash (0.6b). That sequence is the entire lesson of 10 months of PG.

---

## 5. 24/7 Ops + Safety Stack + Graduation

**Graduation ladder (each gated by prior PROD-CONFIRMED):**
1. **Stage 0 micro-orders** ($10–$15) — CHECK A rest+trigger+**slip-bounded fill**+native-cadence decision+**naked-position backstop drill** all green with captured artifacts.
2. **Paper/shadow on real market data** (own-code detection + simulated execution with real fees/slippage) — **5 clean days**: zero phantom outcomes, hourly state-vs-REST reconcile 0 drift, fills/slippage/trail reviewed vs assumptions.
3. **Tiny-live** ($500 dedicated Kraken sub-funds, **BTC-only, 1 position**) on the **Frankfurt VPS** — all safety layers active and drilled (incl. 5.4b on the VPS). Run until ≥20 honest closed R-outcomes.
4. **Scale-1:** add ETH, 2 concurrent. Re-confirm CHECK A on ETH's first stop.
5. **Scale-2+:** SOL then one alt at a time via §6 evidence gate. Raise capital only after prior tier shows positive live expectancy + clean reconciliation.

**Safety stack build/verify order:** (1) prove stop rests+triggers+**fills within slip band** FIRST → (1b) prove the **naked-position backstop flattens** a drilled crash → (2) pre-send size cap → (3) 10% drawdown halt (manual resume) → (4) external watchdog cancel-all + naked-position flatten on 120s silence → (5) file kill-switch via phone/SSH → (6) weekend exposure ×0.5. Each **drilled on the live venue** (forced trip → observe flatten) as a prod-confirm gate.

**Monitoring taxonomy:** INFO (quiet: fills, stop-set, daily 00:00 UTC P&L) · WARNING (yellow: DD>3%, WS reconnect, REST fallback, state mismatch auto-healing, escalation-to-market fired) · CRITICAL (red: DD>10%, kill-switch/watchdog fired, **naked position detected/flattened**, **stop fill slippage > band**, unhandled exception, reconnect failed ×5, stop-placement failed). **Inverted heartbeat: silence IS the alert** — 30-min heartbeat + UptimeRobot /health (5-min). Telegram via non-blocking queue; fallback to local `alerts.log` if Telegram down >5 min.

**Exchange downtime (gap fix — venue trusted at its weakest moment is NOT assumed):** on REST 503 / 5 failed reconnects → maintenance mode: **halt new orders, poll `/Time` every 60s, and DO NOT assume resting stops are healthy.** Because a matching-engine outage is exactly when server-side stops may not execute, the watchdog continues to attempt REST position reads; if positions are readable but unprotected during the outage, escalate CRITICAL ("positions exposed during venue outage — manual decision required"). v1 does NOT auto-flatten into an outage (illiquid), but it loudly surfaces the exposure rather than silently trusting the venue. On recovery → REST position audit + stop-existence re-verify + reconcile before resuming. Subscribe Kraken status → Telegram. (Multi-exchange failover = v2.)

**Pre-launch checklist (condensed):** dedicated Kraken account; API key **Trade-only, Withdrawal DISABLED**, IP-allowlisted to VPS; keys in env/secrets never git; pinned pg_core hash matches start-banner; CHECK A (rest+trigger+slip+native-cadence-decision) green on live micro-order; 0.6b naked-position backstop drilled green; WS forced-reconnect <60s + REST sync verified; all safety drills passed (incl. 5.4b on VPS); systemd Active + crash-restart + OnFailure alert verified; UptimeRobot ≤5-min; 5 clean paper days with 0 reconcile errors; CI guard confirms no LIVE-ONLY milestone is PROD-CONFIRMED without an attached live readback artifact.

---

## 6. Explicit NON-Goals (deliberately NOT built first)

- **No leverage / perps / margin.** Spot long-only.
- **No shorting / bear-side logic.** Upward-trend-follower only.
- **No alts beyond the screened tier.** No new/illiquid coins, no meme rotation, no "add ten and see."
- **No strategy zoo.** ONE strategy until it earns expansion via the evidence gate. No overlays, regime-router, or LLM-filter in v1.
- **No client-side / emulated trailing stop.** No venue lacking native resting stops (Coinbase/Gemini out).
- **No concurrent native-auto-trail + self-managed trail.** One authority (self-managed) per FIX 2.
- **No unbounded market stop on trigger.** stop-loss-LIMIT with a max-slip band + escalation (FIX 1).
- **No reuse of PG's `crypto_adapter.py` (Alpaca) or any PG state.** Clean-room. No editable pg_core install for paper/live (FIX 5).
- **No multi-exchange failover, no multi-region VPS, no CCXT on the order critical path** (data only) — v2.
- **No cloud move during dev** — local for dev/paper; VPS only when live capital goes on.
- **No optimization/tuning of thresholds before honest live measurement exists** — can't optimize what we can't measure.
- **No assuming green CI = safe to go live** — LIVE-ONLY milestones require captured live artifacts (FIX 6).
