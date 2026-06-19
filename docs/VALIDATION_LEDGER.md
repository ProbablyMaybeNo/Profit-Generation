# Validation Ledger — INDEPENDENTLY VERIFIED

> **Verification stamp (Claude, main loop, 2026-06-19):** the master-blocker facts below were
> re-run by me against the live `data/trading.db` / `config/settings.json` — NOT taken on the
> audit's word. Confirmed: **0 of 418 `paper_trades` are `order_type='stop'`** (all `market`,
> 0 `stop_price`); **0 of 3,057 `outcomes` have `r_multiple`**; **all 19 `trailing_stops` are
> `atr_trail`, 0 `chandelier`**; **1 `regime_scores` row (2026-06-18)**; 130 buys stamped
> `entry_stops` with no backing stop. The audit is accurate. We are in a **NO-GO** state for
> unpausing any strategy or expanding to any new market until the §3 confirmation protocol passes
> on a real session. A failed confirmation is a HALT, not a patch.

---

# Validation Ledger & Doom-Loop Audit — Session of 2026-06-19

**Auditor:** Lead reliability auditor
**Scope:** Stages 0, 1, 2, 3.1, 3.2 — all merged to `main`, live-on-paper
**Mandate:** Quadruple-check we are NOT repeating the doom bug loop (patch-and-move-on without certainty of WHAT / HOW / EXPECTED / CONFIRMED proof).
**Ground truth:** All structural claims below were re-verified directly against `data/trading.db` and `config/settings.json` during this audit — not taken on the forensics' word.

---

## 1. Direct Verdict — Are we in the doom loop?

**PARTIAL-RISK. We are on the edge, not over it — but the disciplined-process bar Ross set has been met on ZERO changes.**

The doom loop is defined by *advancing the system without confirmed proof of the outcome before moving on*. Measured against that definition:

- **13 of 13 behavior-changing changes are merged LIVE on `main` with confirmation status TESTED-ONLY.** Not one is PROD-CONFIRMED on a real session.
- **The "green tests" are systematically non-representative.** Every protection test injects a MagicMock / OwnerBroker that returns a hardcoded `accepted` order. The exact failure class these changes exist to fix — broker rejection / fill-settlement race — is the one thing the tests structurally cannot exercise.
- **Four changes are not merely unconfirmed — they are INERT in production right now, with no alarm.** Verified against the live DB this session:

| Claim | Live DB reality (verified 2026-06-19) |
|---|---|
| Stops rest on the book | `0` of `418` paper_trades are `order_type='stop'` — all `market`. 0 stop rows ever. |
| R-multiple metric (1.6) | `0` of `3057` outcomes have `r_multiple` populated. |
| Chandelier trail flipped (1.3) | `0` chandelier rows; `19` stale `atr_trail` rows (method-lock = flip is a no-op). |
| Regime score live (2.1/2.2) | `1` regime_scores row ever, `score_date=2026-06-18`, already lagging macro. |
| Phantom cleanup (0.1) | `phantom_no_fill=0`, `open=2` — the under-fix side is genuinely clean. |
| Event-quarantine confirm query | DB has **no `orders` table** — the shipped confirmation query was never runnable. |

A change that produces zero output and has no failure alarm is **indistinguishable from a working one on a quiet day.** That is the precise mechanism by which phantom outcomes accumulated unnoticed before the reset.

**Why this is PARTIAL-RISK and not a full YES:**
- The changes are net-*conservative* — they size down, cap risk, and add alerts. The blast radius of an inert risk-reducer is "no protection added," not "new losses."
- Nothing has been unpaused; grace_admit is operator-triggered and has fired zero times.
- The system is on paper. Real money is not exposed.

**Why this is not a clean NO:**
- "Shipped and dormant" is exactly what the Donchian-only reset was meant to stop. Calling these changes "done" because the unit suite is green is the doom-loop reflex in its purest form.
- The single most important safety property of the entire stack — *a hard stop actually resting on the Alpaca book* — has **never once been observed in production** and is assumed-true across six separate changes.

**Honest bottom line:** We have not yet patched-and-moved-on, because we are pausing here to audit. If we accept these 13 changes as confirmed on the strength of the unit suite and proceed to unpause, we *enter* the doom loop at that moment. The work to exit is the Monday confirmation protocol in §3.

---

## 2. The Validation Ledger

Status legend: **TESTED-ONLY** = green on unit/MagicMock evidence only, never observed on a real broker session. **INERT** = TESTED-ONLY *and* verified to produce zero output in production today.

| ID | What | Live impact | Expected outcome | PRODUCTION confirmation signal (exact observable) | Failure signature | Rollback | Status | Doom-loop risk |
|---|---|---|---|---|---|---|---|---|
| **0.1** | require_fill gate on outcome-open (phantom fix) | EOD report only opens outcomes for genuinely filled 1d buys | Phantom rows = 0 (under-fix) AND no real 1d fill skipped (over-fix) | Under-fix: `phantom_no_fill=0` ✅ already confirmed 06-18/06-19. Over-fix inverse query (filled 1d buy with NO outcome) must return **0** on a session with a *real 1d fill* | A genuine 1d fill silently gets no outcome row; `opened` count < filled-buy count | git revert | TESTED-ONLY (under-fix confirmed; over-fix path never exercised by a real 1d fill — 06-18's 3 fills were all 15m) | LOW |
| **0.2** (6b26efb) | Attach SELL STOP after daily/swing entry | Daily entries should arm a resting stop sized to fill qty | Resting OPEN SELL STOP, qty == settled fill qty, stop_price >= 0.01 @2dp | **Alpaca `get_orders(status='open')`** shows the stop; `paper_trades` row `order_type='stop'`. Verified: **0 daily entries since merge; 0 stop rows ever** | Alpaca rejects stop (422 / wash / sub-penny); or arms wrong qty on partial fill; M7 verifier still reports protected | git revert | TESTED-ONLY (MagicMock only — broker accept is faked) | MEDIUM |
| **0.3/0.4/0.6** (f3c1507, d332bed, ffe4592) | Stamp stop status; protection_metrics; NAKED LONGS alert | Surfaces unprotected fills; counts protected legs | Alert fires only on genuinely naked fills; `protected>0` when stops rest | Persisted `entries_naked` == protection_metrics SQL; **Alpaca `get_orders(status='open')` cross-check** of order_type='stop' status (existence-of-row is NOT proof — a rejected stop row counts as protected) | Stop-attach broken → alert fires every report → gets muted = doom loop. Latent: end-of-pass sync lacks reconcile-first ordering → orphan outcomes once stops exist | git revert / remove `_maybe_alert_naked` | TESTED-ONLY; alert is a **true positive today** (correctly screaming naked) | **HIGH** |
| **1.1** | ATR-risk position sizing (0.75% risk/trade) | Sizes ALL entries (incl. live intraday) via shared `_process_entry` | `sizing_method='atr_risk'`, fallback=False, dollars_at_risk ~0.75% equity | **MUST ADD** persisted sizing payload to paper_trades for ALL bar_intervals; then confirm from DB. **The shipped 1d-only / trailing_stops-join query returns NULL for the live intraday book — do not use it** | Silent tiered fallback (rps=None when stop missing/stale) invisible; qty=1 floor on high-priced names risks >0.75% silently | git revert | TESTED-ONLY (8 tests hand-feed risk_per_share as a literal; production derivation untested) | **HIGH** |
| **1.2a** | Portfolio heat cap (6% total open risk) | Blocks new entry when aggregate open-risk > 6% equity | Non-binding read today; correct block when book carries ~$6k open risk | (A) read: heat computes without error each run. (B) bind: never observed — 0 heat skips in 200k+ skips | Reads phantom 'open' rows → overstates heat (false blocks); or rejected-stop row charged as protected → understates heat (book silently >6%); portfolio_value 0/None → cap silently disabled | settings flag | TESTED-ONLY; arithmetic unit-proven, real-fill fidelity unproven | LOW |
| **1.3** (272011a) | Chandelier trail adopted (opt-in, method-locked) | New longs on fresh (strategy,symbol) get chandelier trail | >=1 `method='chandelier'` row; stop_price == extreme - 3·ATR22, floored at entry stop | `SELECT method,COUNT(*) FROM trailing_stops` shows chandelier row on a **genuinely fresh pair**; floor binds; resting ATR stop OPEN on Alpaca | **SILENT**: compute returns None → trail never advances, no alert. Verified: **0 chandelier rows, 19 stale atr_trail** (flip is a no-op for all tracked pairs). Floor=None if no live stop row (sub-penny history) → synthetic stop has no real protection beneath | git revert + manual `UPDATE trailing_stops SET method='atr_trail'` (method-lock footgun) | INERT | MEDIUM |
| **1.4** (9e31a6e) | Drawdown kill-switch ladder (halve@15%/quarter@20%/halt@25%) | Throttles size and halts on peak drawdown | 0.5x sizing actually shrinks qty; 25% trip writes kill_switch.json + same-run KILL skips | Forced drill: log shows "→ halved"; next order's throttle_multiplier=0.5 AND Alpaca qty materially lower; forced kill writes `config/kill_switch.json live_trading_halted=true` + KILL_SWITCH_HALT skips | Sizing-bite rounds to same share count (no real protection); failed atomic_write swallowed → halt doesn't persist to next run; docstring promises hysteresis that does NOT exist (flapping 0.5x↔1.0x). Daily-breaker WIDENED 2%→3% = strictly less protective | git revert (NOT delete block — falls back to MORE aggressive module defaults) | TESTED-ONLY (band math only; chain 3497→3538→3634 never exercised) | MEDIUM |
| **1.5/1.6** (63a087d) | R-multiple per outcome + expectancy_metrics | Each close records r_multiple; dashboard prints expectancy | `r_multiple IS NOT NULL` count goes 0 → >0 | `SELECT COUNT(*) FROM outcomes WHERE r_multiple IS NOT NULL` > 0 on a real closed session. Verified today: **0 of 3057** | close_outcome reads `paper_trades WHERE order_type='stop'` which has **0 rows ever** → r_multiple structurally always NULL; expectancy line never prints (n=0 short-circuit); no alarm | git revert | INERT — **WEAK-REWORK** (source must be re-pointed to a value the live path writes) | **HIGH** |
| **2.1** | Daily regime score (VIX-200dMA + ADX) | Computes pre-market regime label | Fresh score each weekday; gate available to sizing/eligibility | `regime_scores` MAX(score_date) advances each trading day. Verified: **1 row ever (06-18)** | SILENT: empty/stale/errored read → transitional 0.5x with no log → half-sizes whole book; ADX-null → VIX-only degrade unflagged; VIX boundary whipsaw (live 18.44 vs MA 18.59) flips book 4x with no hysteresis | settings flag | TESTED-ONLY (monkeypatched) | MEDIUM |
| **2.2** | Wire regime into eligibility + sizing | risk_off blocks directional class; 0.25x/0.5x sizing | SKIP_RISK_REGIME fires on risk_off; non-risk_on entries sized down | `intraday_skips gate='risk_regime_off'` rows on a risk_off session; sizing detail risk_pct==0.0075·risk_scale. **Eligibility half is PROVABLY INERT**: active strategy 'trend-donchian-breakout-20' NOT in TRACKED_STRATEGIES → SKIP_RISK_REGIME can never fire for it | No freshness check → stale row drives sizing forever; run_macro.bat discards monitoring.regime exit code → persist failure reports exit 0; never-exercised against Alpaca | settings `risk.regime_gate.enabled=false` (safer state) | TESTED-ONLY; eligibility half INERT | **HIGH** |
| **2.3** | Event quarantine (CPI/FOMC desize + intraday skip) | Skips intraday & desizes 1d on listed macro dates | On 2026-07-14: intraday skipped, 1d desized to 0.25x | Intraday: `intraday_skips WHERE gate='market_event'` ✅ valid. De-size: **shipped query is broken** (no `orders` table; bar_interval not on paper_trades). Correct: `SELECT pt.symbol,pt.qty FROM paper_trades pt JOIN signals s ON s.id=pt.signal_id WHERE pt.side='buy' AND date(pt.submitted_at)='2026-07-14' AND s.bar_interval='1d'` | Hardcoded dates unverified vs BLS/FOMC calendar; `asof=date.today()` PDT/TZ bug class (bitten before); de-size leaves NO audit row; upcoming_events() has zero non-test callers (dead pre-warning) | settings flag | TESTED-ONLY; confirm query never ran on real schema | MEDIUM |
| **3.1** | Strategy reintroduction framework (evidence/correlation gates) | Provides evaluate_candidate machinery | Fail-closed on thin/no evidence | Correlation/evidence gate refuses on thin sample (verified rsi2 REFUSED at evidence stage) | Evidence engine depends on r_multiple/outcomes which are sparse/inert post-cleanup → admit on thin sample untested | git revert | TESTED-ONLY | MEDIUM |
| **3.2** | grace_admit cold-start (operator-triggered, 0.5x) | Operator can re-admit a paused strategy at half size | Grace-admit fires >=1 buy at ~0.5·max_position_usd with a resting stop | First run after a real grace_admit: buy exists (not silent no-op); notional ~0.5·max_position_usd (**LIVE multiplier is 0.5, NOT the documented 0.25**); resting Alpaca SELL STOP for the symbol | botnet101-* (grace_period=False) → fires NOTHING at n==0 = silent dead deadlock; doc/code drift on multiplier; hard-stop on real broker unproven (sub-penny class) | not running the CLI (no config flag); manual | TESTED-ONLY (OwnerBroker fake) | MEDIUM |

---

## 3. Rigorous Confirmation Protocol — Live session of Monday 2026-06-22

Run ALL checks **after** the session closes and the EOD report has persisted. Each check has an explicit PASS/FAIL threshold. **A FAIL is a halt, not a patch.**

### Pre-flight (before market open, Monday AM)
- **P0 — Confirm the scheduled task ran THIS code:** `git rev-parse HEAD` matches the deployed `\TradingSystem\` task's working tree. Capture the auto_trader start banner / commit hash into a log line. PASS = hash matches `a176a55` (or current main). FAIL = stale process is running → fix deployment first; nothing else below is trustworthy.
- **P1 — Add the two missing observables BEFORE the session (these are prerequisites, not optional):**
  1. Persist sizing payload (`sizing_method`, `fallback`, `risk_per_share`, `risk_pct`, `dollars_at_risk`) into `paper_trades` at submit for ALL bar_intervals (change 1.1 is unconfirmable without this).
  2. Add a desize audit row for change 2.3 (e.g. `gate='market_event_desize'`).
  3. Add reconcile_stop_fills-BEFORE-order_sync to the end-of-pass block (latent corruption guard — must land before any stop ever rests).

### Post-session checks (run against `data/trading.db` + Alpaca API)

**CHECK A — Stop actually rests on the broker (MASTER GATE for 0.2/0.3/1.2a/1.3/1.5/1.6)**
- DB: `SELECT COUNT(*) FROM paper_trades WHERE order_type='stop' AND date(submitted_at)=date('now');`
- Broker: `alpaca.get_orders(status='open')` filtered to SELL STOP legs; for each, assert qty == settled entry fill qty and stop_price >= 0.01 at 2dp.
- **PASS:** >=1 stop row AND >=1 corresponding OPEN stop on the Alpaca book with matching qty.
- **FAIL:** 0 stop rows OR a stop row exists with no matching OPEN broker order (rejected/cancelled). → **HALT. Do not unpause anything. The entire protection stack is unproven.**

**CHECK B — Over-fix / phantom both clean (0.1)**
- `SELECT COUNT(*) FROM outcomes WHERE status='phantom_no_fill';` → PASS = 0.
- Over-fix (only if a real 1d fill occurred): filled 1d buy with NO outcome row → PASS = 0. If no 1d fill occurred, mark **NOT-EXERCISED** (not PASS).

**CHECK C — R-multiple populated (1.5/1.6) — depends on CHECK A passing**
- `SELECT COUNT(*) FROM outcomes WHERE r_multiple IS NOT NULL;`
- **PASS:** > 0 on a session with at least one stop-protected close.
- **FAIL:** stays 0 while CHECK A passed → close_outcome source is mis-pointed. **Rework 1.6, do not mark done.**

**CHECK D — ATR-risk sizing fired (1.1) — requires P1.1 observable**
- `SELECT sizing_method, AVG(dollars_at_risk*1.0/(SELECT equity...)), SUM(fallback) FROM paper_trades WHERE date(submitted_at)=date('now');`
- **PASS:** bulk of entries `sizing_method='atr_risk'`, `fallback=0`, dollars_at_risk clusters 0.6–0.9% of equity.
- **FAIL:** majority fallback=1 OR risk_pct wildly off 0.75% → silent tiered fallback is firing. Halt sizing trust.

**CHECK E — Regime freshness + non-inert (2.1/2.2)**
- `SELECT MAX(score_date) FROM regime_scores;` → PASS = equals last trading day (advances).
- `intraday_skips` for `gate='risk_regime_off'` on a risk_off label → if label was risk_off and active strategy is in TRACKED_STRATEGIES, expect skips. **Today the active strategy is NOT in TRACKED_STRATEGIES — the eligibility gate is INERT; this must be fixed before claiming the gate bites.**
- **FAIL:** score_date stale OR scheduler reported exit 0 on a regime-persist failure → freshness guard + exit-code propagation must land before trusting the gate.

**CHECK F — Heat cap honesty (1.2a)**
- Reconcile: `COUNT(outcomes WHERE status='open')` vs `len(Alpaca positions)`; db_heat vs broker_heat (Σ(entry−stop)·qty from real positions) within tolerance.
- **PASS:** counts and heat agree within tolerance. **FAIL:** divergence → phantom/rejected-stop corruption of the heat table → halt heat-cap trust.

**CHECK G — Naked-longs alert is a true positive, not noise (0.3/0.4/0.6)**
- If CHECK A passed, the NAKED LONGS alert should STOP firing for protected entries. If it still fires for an entry that has a confirmed OPEN broker stop → false positive in protection_metrics (counting row existence not broker status). **Do NOT mute — fix the metric to cross-check broker status.**

### The halt rule (non-negotiable)
> **If any of CHECK A, C, D, E, F FAILs: HALT. Roll back that change to its documented safe state (see Ledger rollback column). Do NOT patch-on-top and continue the session. Do NOT mark the change confirmed. Record the FAIL, revert, and re-plan.** A failed confirmation is data, not an obstacle — treating it as something to quickly patch past IS the doom loop.

### Anti-silence requirement
Every INERT/silent-failure change (1.3, 1.5/1.6, 2.1/2.2) must get a standing assertion that **alerts to Telegram** when its expected output count stalls (e.g. "expected chandelier rows but got 0", "r_multiple still 0 after closed session", "regime score_date did not advance"). Until a human-free alarm exists, these remain doom-loop carriers regardless of any single green session.

---

## 4. Go / No-Go for proceeding (unpause / market expansion)

**Current state: NO-GO on both unpause and market expansion.**

Gate to GO is the four PROD-CONFIRMED conditions (full text in the go_no_go field):
1. **Stop rests on the real Alpaca book** (CHECK A) — master blocker; invalidates six changes if unmet.
2. **r_multiple > 0** (CHECK C) — the reintroduction evidence engine has no data until this is non-zero; promoting on it is the doom loop by definition.
3. **ATR-risk sizing observable & dominant** (CHECK D).
4. **Regime freshness guarded + scheduler exit-code propagated** (CHECK E).

Additional, specific to the *first* grace_admit:
- Grace-admit a **grace_period:True** strategy first (rsi2/rsi14/bollinger/trend/breakout) — **never** a botnet101-* strategy (grace_period=False → silent dead deadlock).
- Confirm the live multiplier in code/settings is the intended value (currently **0.5**, while docstrings say 0.25 — resolve this drift before relying on grace exposure math).
- Confirm a resting stop appears on the Alpaca book for the grace-admitted symbol (CHECK A applied to the new strategy).

**Market expansion (crypto/futures/forex/Asian):** NO-GO until all four gates pass on the *existing* equities book for at least one clean confirmed session, AND the standing silent-failure alarms (§3 anti-silence) are live. Expanding markets on top of an unproven stop-attach path and an inert evidence engine would multiply the unconfirmed surface area — the maximal doom-loop move.