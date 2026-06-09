# Profit Generation — System Audit 2026-06-03 (exit / position-accounting focus)

Read-only audit. No code, config, DB, or broker state was modified. The only
broker calls made were read-only: `get_account_summary`, `get_all_positions`,
`get_orders(OPEN)`.

Audited HEAD: `2f05e86` (main). **Critical context:** the live trading process is
running STALE code — F2, F2-SAFETY, F5-LIVE, F7 are committed on disk but the
long-running process was not restarted, so today's DB reflects PRE-fix behavior.
Each finding is tagged **[stale-code: resolved-on-restart]** vs **[NEW/unresolved]**.

This report supplements the earlier `SYSTEM_AUDIT_2026-06-03.md` (commit-verification
sweep); it targets the operator's two suspicions: entries >> exits and ~260 open
positions.

---

## 1. Executive Summary (highest priority first)

1. **OPEN-outcome ledger has diverged ~18x from broker reality.** DB shows **260
   open outcomes** across **179 symbols**; the broker holds only **14 positions**.
   **165 of 179 open-outcome symbols are not held at the broker** — phantom opens.
   (P0)

2. **Entry/exit imbalance = spurious EOD-flatten sells.** Today: 24 buys vs 80
   sells. The 58 surplus sells are duplicate EOD-flatten orders targeting intraday
   buys that were already closed intraday. Root cause: domain mismatch in
   `_open_intraday_buys` — dedup `pt.id NOT IN (... pt2.signal_id)` compares a
   paper_trades PK against a signals FK (disjoint id spaces). Broken query selects
   87 buys; correct query selects 29. (P0, NEW Bug)

3. **Plain intraday signal-exits close the broker position but never close the
   outcome.** `_handle_exit` (auto_trader.py:1348) closes the outcome ONLY when
   `trailing_triggered is not None`; a plain `long_exit_signal` is "left for the
   reconcile" — but no reconcile pass closes intraday outcomes. Strands intraday
   outcomes OPEN forever; not covered by F2/F2-SAFETY. (P0, NEW Bug)

4. **`intraday_bars` covers only 10 ETF/index symbols; the 1m universe is 20.**
   AAPL, AMD, NVDA, TSLA, META, AMZN, GOOGL, AVGO, COIN, MSFT, SMH, XLK have ZERO
   intraday bars → MFE/MAE never computable and the F2-SAFETY stale-sweep skips
   them, so their orphans can't auto-close even after restart. (P1)

5. **1d trend strategies have no exit in the outcome model.**
   donchian-breakout-20 (154 open) + ma-cross-20-50 (40 open) = 194 of 260 opens;
   153/154 open donchian outcomes have no later exit signal. Outcome ledger
   conflates "signal-open" with "position-held" and accumulates indefinitely. (P1)

6. **F7 not live: `no_open_position` skip spam still written** (15,940 today,
   203,754 total) — confirms the running process predates F7 and, by inference,
   F2/F2-SAFETY/F5-LIVE. (P2, resolved-on-restart)

7. **`skip_intraday_signals` is a dead config key** — defined (auto_trader.py:47),
   set true in settings.json, never read. The suspected `skip_intraday_signals`
   vs `intraday_enabled` contradiction is a non-issue: only `intraday_enabled`
   gates intraday. (P3)

8. **EOD sells not synced after the final flatten** — 58 sells submitted 21:04 UTC
   sit `accepted`/NULL-fill because order_sync runs before signal eval, not after
   the flatten. (P2)

9. **`reconcile_stop_fills._open_outcome_for` keys on (strategy_id, symbol) only,
   not bar_interval** — can close the wrong outcome when a strategy has both a 1d
   and intraday open outcome for one symbol. (P2, latent Bug)

10. **Idle capital** — broker cash $69k of $99.9k (~31% deployed). Revisit after
    A1–A3, since ledger noise inflates perceived exposure. (P3)

---

## 2. Findings Table

| ID | Sev | Category | Title | Evidence | Root cause | Fix | Effort | Conf |
|----|-----|----------|-------|----------|-----------|-----|--------|------|
| A1 | P0 | Bug | EOD-flatten dedup compares disjoint id spaces | close_intraday_positions.py:91-96; sim broken=87 vs correct=29; pt.id 1-707, signal_id 5453-65030 | `pt.id NOT IN (SELECT pt2.signal_id ...)` PK vs FK | `pt.id NOT IN` → `pt.signal_id NOT IN` | S | High |
| A2 | P0 | Bug | Plain intraday signal-exit never closes outcome | auto_trader.py:1296,1346-1348; intraday outcomes closed=10, open=58 | outcome close gated on `trailing_triggered is not None`; no reconcile closes intraday | close outcome in `_handle_exit` for plain exits too | M | High |
| A3 | P0 | SilentFailure | OPEN-outcome ledger drifted 18x from broker | DB open=260/179 syms; broker=14; 165 phantom symbols | outcomes track signals fired, not positions held; closes never land (A2,A5) | broker-reconcile pass closes outcomes with no matching position | M | High |
| A4 | P1 | Data/Blockage | intraday_bars 10 syms vs 20-sym universe | `DISTINCT symbol FROM intraday_bars`={GDX,IWM,KRE,QQQ,SPY,XBI,XHB,XLE,XME,XOP}; open 1m outcomes span 20 syms; orphans have bars_after=0 | persist universe ≠ strategy universe | persist intraday_bars for all 20 intraday symbols | M | High |
| A5 | P1 | Bug/Design | 1d trend strategies never exit → outcomes never close | 194/260 open are donchian+ma-cross; 153/154 donchian opens have no later exit | breakout/MA exit only on rare breakdown; no time/stop exit in model | reconcile to broker (A3) or model ATR/time stop as outcome close | M | High |
| A6 | P2 | SilentFailure | F7 not live: no_open_position spam | no_open_position max recorded_at 2026-06-03T19:58, n=203,754 | process predates F7 (5f2afbe) | restart live process | S | High |
| A7 | P2 | Bug | EOD sells not synced after final flatten | 58 accepted sells today, all NULL fill_price/filled_at, submitted 21:04 UTC | order_sync runs before eval, not after flatten | run order_sync after close_intraday_positions | S | High |
| A8 | P2 | Bug | reconcile_stop_fills outcome match ignores bar_interval | stops.py:201-212 keyed (strategy_id, symbol) | no bar_interval in WHERE | add bar_interval to match | S | Med |
| A9 | P3 | Blockage | dead config key skip_intraday_signals | auto_trader.py:47; no consumer in grep | leftover default | remove/wire; document intraday_enabled is the gate | S | High |
| A10 | P3 | Optimization | idle capital ~31% deployed | broker cash $69k / $99.9k | conservative sizing; many vetoes | revisit after A1-A3 | M | Med |

---

## 3. Per-Finding Detail (P0/P1)

### A1 (P0, NEW Bug) — EOD-flatten dedup compares disjoint id spaces
`monitoring/close_intraday_positions.py`, `_open_intraday_buys`, lines 91-96:
```sql
AND pt.id NOT IN (
     SELECT pt2.signal_id FROM paper_trades pt2
      WHERE pt2.side = 'sell' AND pt2.status NOT IN ('rejected','canceled')
        AND pt2.signal_id = pt.signal_id )
```
`pt.id` is the paper_trades PK (range 1–707); `pt2.signal_id` is a signals FK
(range 5453–65030). The domains barely overlap so the exclusion almost never
fires. The flatten re-selects intraday buys already closed by the intraday exit
scanner. Simulation: broken `pt.id` predicate → **87** buys to flatten; correct
`pt.signal_id` predicate → **29**. The 58-row gap is exactly the 58 spurious
"accepted" EOD sells today and explains 24 buys vs 80 sells. Direct evidence:
buy `c980ff0d-...` EOD-closed on both 2026-06-02 (canceled) and 2026-06-03
(accepted). Over-selling is avoided only by Alpaca rejects / post-close non-fills.

**Fix:** change `pt.id NOT IN` to `pt.signal_id NOT IN`. Add a regression test:
an intraday buy with a prior non-terminal sell on the same signal_id must be
excluded from the flatten set.

### A2 (P0, NEW Bug) — Plain intraday signal-exit closes broker but not outcome
`monitoring/auto_trader.py`, `_handle_exit`. Lines 1320-1336 submit the closing
SELL and record the paper_trade. Line 1348 closes the outcome **only** when
`trailing_triggered is not None`; the comment (1346-1347) says a plain signal exit
is "left for the reconcile." But:
- the EOD 1d reconcile (daily_report.py:378) filters `bar_interval='1d'`;
- the F2 intraday reconcile (daily_report.py:391) runs `open_only=True`, never closes;
- the EOD flatten only closes outcomes for buys it still considers open — after the
  scanner already sold, the position is flat.

So a plain intraday `long_exit_signal` closes the broker position and leaves the
outcome OPEN permanently. Evidence: AAPL intraday buy/sell pairs are fully filled
and matched at the broker (2026-06-02 and 2026-06-03), yet 3 AAPL intraday
outcomes remain OPEN. System-wide only 10 intraday outcomes ever closed (all
`eod_close`) vs 58 open across prior sessions.

**Fix:** in `_handle_exit`, when `trailing_triggered is None` and the exit is an
intraday-interval exit, close the matching open outcome with
`exit_reason='long_exit_signal'` (compute MFE/MAE via `bars_fetcher`, mirroring
the trailing branch at 1348-1366). Ensure the 1d path is not double-closed (this
branch should fire for intraday intervals only; the 1d reconcile keeps owning 1d).

### A3 (P0, SilentFailure) — OPEN-outcome ledger diverged from broker
Broker `get_all_positions()` → **14** longs
{AES,AMD,DXCM,EW,GDX,GEN,IWM,KRE,NVDA,QQQ,SMH,SPY,TSLA,XBI}, portfolio
$99,897.85, cash $69,036. DB `outcomes.status='open'` → **260** rows / **179**
symbols; **165** symbols have no broker position. The outcomes ledger is a
signal-tracking artifact, not a position ledger; with A2 and A5 the closes never
land and OPEN grows every session (entry-date histogram monotonic
2026-05-19→06-02). This silently corrupts eligibility stats, concentration caps,
and any open-position-derived risk gate.

**Fix:** extend `monitoring/reconcile_positions.py` with a pass that, for each
OPEN outcome with no matching broker position, closes it at the last known
fill/mark with `exit_reason='reconciled_no_position'`. Run EOD after the flatten.
A1+A2 stop the bleed; this pass cleans the 165-symbol backlog.

### A4 (P1, Data/Blockage) — intraday_bars universe too small
`SELECT DISTINCT symbol FROM intraday_bars` = 10 symbols
{GDX,IWM,KRE,QQQ,SPY,XBI,XHB,XLE,XME,XOP} (n=21,218 bars, 2026-05-14→06-03). The
1m strategies trade ~20 symbols; open 1m outcomes include AAPL, AMD, AMZN, AVGO,
COIN, GOOGL, META, MSFT, NVDA, SMH, TSLA, XLK — none in intraday_bars. For these,
`_intraday_bars_window` returns empty so `_close_outcome_for_eod` and
`sweep_stale_intraday_outcomes` skip the row (verified: SMH/XLK/META/AMD/TSLA/AVGO
orphans have `bars_after=0`), and MFE/MAE is permanently NULL.

**Fix:** align the intraday bar persistence symbol list with the intraday strategy
universe so all 20 symbols get bars written. Forward coverage is the fix; backfill
optional.

### A5 (P1, Design) — 1d trend strategies never exit in the outcome model
`trend-donchian-breakout-20` (154 open) + `trend-ma-cross-20-50` (40 open) = 194
of 260 opens. They emit `long_exit` only on a Donchian-lower / MA-cross-down
breakdown, rare in a trend — 153/154 open donchian outcomes have no later
`long_exit` at all. Some map to real held positions (AES, NVDA), but the model
has no ATR/time-based close, so winners and abandoned signals both sit OPEN and
inflate the ledger.

**Fix:** preferred — let the broker-reconcile pass (A3) be source-of-truth for
1d-held positions and close outcomes whose position exited via stop. Alternative:
model the ATR initial/trailing stop as an outcome-closing event for 1d trend
strategies (today only the intraday/trailing path records it).

---

## 4. Verification of Recent Fixes (code-level)

| Fix | Status | Evidence |
|-----|--------|----------|
| M0 sub-penny stop quantization | **PASS** | submit_atr_stop (stops.py:180) calls quantize_stop_price → 2dp for ≥$1; stop_price_for returns 4dp (stops.py:166) but submit path quantizes. |
| M1 MFE/MAE + intraday-outcome capture | **PARTIAL/FAIL in prod** | Code present, but intraday capture broken by A2 (plain-exit gap) + A4 (no bars for half universe). Only 10 intraday outcomes ever closed. |
| M2 quarantine skips entries | **PASS (code)** | paused_strategies holds intraday-1m-orb, intraday-1m-vwap-reclaim, botnet101-consec-bearish; paused_strategy gate fired 183x. |
| M3 qty<1 veto sizes against real cap | **PASS** | Today buy fills qty 1–42, no zero-qty stuck rows. |
| M4 size-to-edge within caps | **PASS (code)** | Tiered sizing + intraday $800 floor in config; varied fill sizes. |
| EOD cancel-resting-orders flatten | **PASS (code), UNDERMINED by A1** | _cancel_open_orders_for_symbols (close_intraday_positions.py:47) cancels before flatten, but the buy-selection query (A1) flattens the wrong set. |
| F2 (351da84) intraday open-pass | live: **NOT ACTIVE** | stale process; F7 spam still writing. |
| F2-SAFETY (62a0fc4) stale-sweep | code present, **ineffective for 10/20 syms** (A4). |
| F5/F5-LIVE stop reconcile | code present, **untested live**; A8 latent. |
| F7 stop no_open_position spam | live: **NOT ACTIVE** | 15,940 rows today. |

---

## 5. Caveats / What Couldn't Be Verified

- **Stale-code attribution:** F7 confirmed not live (today's no_open_position
  writes). By inference F2/F2-SAFETY/F5-LIVE are also not live; a restart is
  required to test them. A1, A2, A4, A5 are NEW/unresolved and persist after restart.
- **A1 over-selling at broker:** the broken dedup *submitted* spurious sells but I
  could not confirm a net-short or double-sell fill — surplus sells today are all
  `accepted`/unfilled (post-close) or `canceled`. Risk is real but not yet harmful.
- **Broker open orders = 50:** resting ATR/trailing stops + 58 unfilled EOD sells;
  not individually enumerated. 14 positions is the authoritative held figure.
- **A3 cleanup policy** (close at last mark vs drop) is the operator's call; I
  recommend explicit `reconciled_no_position` to keep stats honest.
- **Thin live sample** for intraday edge (~10 closed intraday outcomes); A4 must be
  fixed before intraday MFE/MAE is measurable.
