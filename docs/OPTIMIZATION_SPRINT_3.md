# Optimization Sprint 3 — Symbol Ownership, Order Integrity & Intraday Risk

Source: trading-daily-brief + trading-daily-analysis (Hermes cron, gpt-5.5) for
2026-06-05. Implements EVERY optimization + debug suggestion from those reports.

**Context / why this sprint exists:** Sprint 2's M1 added per-symbol *reservation*
but not single-owner *arbitration*. Result in production: duplicate symbol ownership
(IWM owned by 4 strategies, KRE/NVDA by 2) kept overselling past flat and colliding on
stop submits — the account drifted from −$62k to −$101k of UNINTENDED SHORTS in one
session (now covered manually) and logged Alpaca `40310000` wash-trade rejects on
AMGN/GDX/IWM/QQQ/XBI. The +4.05% "green" day was the accidental short book getting
lucky on a −3% tech tape, not edge.

**HARD LESSON (apply to every milestone):** Sprint 2's M1 PASSED ITS TESTS but DID
NOT BITE IN PRODUCTION. For every order-management milestone (M1, M2, M3, M5, M6) the
acceptance test MUST drive the REAL production call path (the actual
`auto_trader` / `close_intraday_positions` / `stops` submit functions that run live),
with multi-strategy shared-symbol fixtures (IWM, KRE, NVDA), and MUST fail on the
current code. A new module that isn't wired into the live path does NOT satisfy
acceptance. Prefer EXTENDING `monitoring/position_manager.py` (Sprint 2) over adding a
parallel system.

Execute milestones IN ORDER. For EACH: implement to conventions → run FULL non-live
suite (`py -3.13 -m pytest tests/ -m "not live"`) → if green, tick the box here →
commit + push to main. End commit messages with:
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

**OOM GUARDRAIL:** never replace a function with one that calls that same patched name
(the db.init_db self-recursion that caused a 38GB OOM). Capture the real callable
first; sanity-scan monkeypatches; abort if pytest memory climbs abnormally.
**Guardrails:** no weakening of risk.* / paper-gate / kill-switch; no new deps; NO
autonomous live order placement. HALT and report on test failure, abnormal
memory/behaviour, or any milestone needing an architectural decision you can't make
safely.

---

## [ ] M1 (P0, keystone) — single symbol-owner registry + broker-order deduper
[report opt#4, debug#1, debug#2]
One owner (or one shared parent risk bucket) per symbol across all strategies. Before
ANY sell/stop/flatten submit: query the broker's OPEN orders + position for the symbol,
verify side/qty/owner, and **replace-or-reuse** an existing protective order rather than
submitting a conflicting one (eliminates `40310000`); compute net-available and NEVER
oversell past flat (no new shorts). Extends Sprint 2 `position_manager.py` and must be
wired into every live exit/stop/flatten path.
**Acceptance (PROD PATH):** integration test driving the real submit path with IWM/KRE/
NVDA owned by multiple strategies, proving exactly one valid exit/stop order stack per
live position, zero oversell-into-short, and zero duplicate/conflicting stop submits.
Fails on current code.

## [ ] M2 (P0) — intraday exit-signal gating to owned positions
[debug#3]
Only emit/record a `long_exit` signal when the strategy has a live OWNED position for
that symbol. Kills the spam (6,587 vwap + 5,086 momentum exits today vs a handful of
real positions).
**Acceptance:** a positionless/paused strategy emits 0 exit signals; a strategy holding
a real position still emits its single exit. Drive the real signal path.

## [ ] M3 (P0) — paused-strategy position policy
[debug#8, opt#2]
Define and enforce: a paused strategy does NOT take new entries AND does not retain/
manage open positions silently — on pause it flattens its holdings (through the M1
owner registry) or explicitly marks them carried. Today paused ORB variants still held
IWM/NVDA/SPY/QQQ and kept arming stops, causing contention.
**Acceptance:** pausing a strategy with holdings flattens them via the owner registry
and it stops arming new stops; a carried flag (if used) is explicit. Prod path.

## [ ] M4 (P1) — intraday time-stop / max-loss overlay
[opt#7]
Hard per-intraday-position max-loss and max-hold-time; force-close on breach. The
`stale_intraday_flatten_missed` closes bled −4.795% avg — far too large for 1m/5m/15m.
**Acceptance:** a position breaching max-loss OR max-hold-time is force-closed with the
correct exit_reason.

## [ ] M5 (P1) — stale-flatten audit + end-of-session flat assertion
[debug#4]
Trace why 58 intraday positions reached `stale_intraday_flatten_missed` instead of
normal flatten; add an EOD assertion that every intraday-owned position is flat or
explicitly carried, failing/alerting loudly otherwise.
**Acceptance:** an unflattened intraday position at session end triggers the assertion/
alert; a clean session passes silently. Prod path (the real EOD flatten).

## [ ] M6 (P1) — post-fill stop-protection verification
[debug#7]
After every buy fill, verify a valid protective stop is attached (or a verified
equivalent existing open order); fail/alert loudly if a filled position is unprotected.
**Acceptance:** a fill that ends without a stop raises the alert; a fill with a valid
stop passes. Prod path.

## [ ] M7 (P1) — separate cleanup outcomes from performance analytics
[opt#5, debug#5]
Tag `reconciled_no_position` and `stale_intraday_flatten_missed` so they NEVER pollute
fresh-trading expectancy/win-rate stats used by `strategy_health`, the eligibility/
expectancy gate, and the daily report. Today 175 cleanup closes vs 4 fresh closes made
strategy health unreadable.
**Acceptance:** expectancy/strategy-stats computed over FRESH closes only; cleanup
closures excluded. The report shows the fresh-vs-cleanup split.

## [ ] M8 (P2) — strengthen evidence threshold for activation/size-up
[opt#6]
A strategy with < 20 recent FRESH (non-cleanup) closes cannot be auto-sized up without
walk-forward/paper-forward confirmation. Extends Sprint 2 M4 expectancy gate; depends on
M7's fresh-close tagging.
**Acceptance:** a strategy with <20 fresh closes is held at probation size; ≥20 with
positive expectancy may size up.

## [ ] M9 (P2) — trend loser cap
[opt#1]
Tighten the stop / add a per-position max-loss for the trend book (Donchian / MA-cross)
so single-name blowups are capped — today's trend losers included ENPH −16.16%,
AVGO −15.86%, ORCL −13.89%, HPQ −12.77%. Keep Donchian active; just cap the tail.
**Acceptance:** a trend position breaching the max-loss is stopped out; normal winners
are unaffected.

## [ ] M10 (P3) — rsi2-oversold controlled-size promotion
[opt#3]
Allow `rsi2-oversold` to trade at strictly capped small size (recent n=5, 80% win,
+2.098%) — promising but under-sampled. Gate size-up behind M8's evidence threshold.
**Acceptance:** rsi2-oversold trades at the capped probation size and cannot size up
until it clears the evidence gate.

## [ ] M11 (P3, observability) — correct deployed-capital + accounting in the report
[debug#6]
The −89.9% "deployed" reading came from `portfolio_value − cash`, which is meaningless
with short/margin balances. Fix `schedulers/pg_report_data.py` to compute exposure from
`long_market_value` / `short_market_value` / equity (flag net-short explicitly), and add
a loud alert if `short_market_value < 0` for this long-only system. Note in the summary
that the `~/.hermes/scripts/` copy must be refreshed (`tr -d '\r'`) — do not touch the
WSL copy yourself.
**Acceptance:** the script reports long/short exposure correctly and alerts on any
unintended short; runs against the live DB without error.
