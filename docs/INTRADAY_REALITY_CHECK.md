# Intraday Reality Check

_Read-only audit — 2026-06-02. No code, config, DB, or orders were modified._

## Verdict (plain English)

We **are** actively day-trading during market hours: the every-15-min scheduler fires
intraday strategies on 1m/5m/15m bars, and the auto-trader submits **real Alpaca paper
orders across the whole session** (entries from the 9:30 ET open through the day, with
mid-session signal exits, trailing exits, and an EOD flatten). Stops, trailing, and pyramids
are all **wired and live in code**, but three things are NOT what the labels suggest. (1)
**The protective STOP order is computed and submitted on every entry but Alpaca rejects
100% of them** with `sub-penny increment` errors — so the `entry_stops='atr_initial'` tag is
on the entry row, but **there is zero stop_price on the book for any position, intraday or
EOD**. That is protective _intent_, not a protective _order_. (2) **Trailing stops genuinely
run on intraday positions** (14 of 19 live trail rows are intraday strategies) and have fired
at least one real intraday exit — this piece works. (3) **Pyramiding is dormant**: no active
intraday strategy is flagged `pyramidable`, every pyramid evaluation hits
`pyramid_not_pyramidable`, and `pyramid_tier` is 100% NULL — nothing has ever pyramided.
Separately, there is a **measurement blind spot**: intraday positions never get a closed
`outcomes` row (all 2048 outcomes are `1d`; `mfe_pct`/`mae_pct` are NULL on every row), so we
have no exit-quality record for intraday — stops/trailing/exits are _happening_ but
**un-measured**. Net realized intraday P&L over the (very small, 2-day) real sample is roughly
flat-to-slightly-negative.

---

## 1. Are we trading during market hours? — YES, across the full session

**Pipeline (confirmed end to end):**

- `schedulers/run_intraday.bat` — registered as Windows task `TradingSystem\Intraday`,
  `/sc minute /mo 15` (every 15 min). `schedulers/register_intraday.bat:21-25`. Each step
  self-checks `market_is_open`, so off-hours ticks exit immediately.
- Step (a) `monitoring.intraday_monitor --once` — `intraday_monitor.py:259-273`. This is a
  **synthesis** pass that projects EOD strategies onto today's in-progress bar and writes
  `bar_interval='1d-intraday'` signals (`intraday_monitor.py:30,211-231`). Informational; not
  the real intraday trade path.
- Step (b) `monitoring.intraday_fires` — `intraday_fires.py:95-213`. Loads 1m/5m/15m bars
  (`load_intraday_bars`), runs each intraday strategy's `compute_fn`, and records fires to
  `signals` with `bar_interval` in `{1m,5m,15m}`. Per-interval scan windows
  (`intraday_fires.py:42-50`) re-scan recent bars so a 15-min cadence doesn't miss
  sub-cadence fires; the `UNIQUE` constraint dedupes.
- Step (c) `monitoring.auto_trader_intraday` — `auto_trader_intraday.py:41-92`. Reads
  `auto_trade.intraday_enabled` (currently **true**, `config/settings.json:37`) and
  `intraday_intervals` (`["1m","5m","15m"]`, `settings.json:38`). For each interval it calls
  the **same** `auto_trader.process_signals(..., bar_interval=interval)`
  (`auto_trader_intraday.py:74-79`) the EOD path uses — same eligibility, sizing, regime,
  stops, trailing, pyramid branches.

**Data evidence — intraday orders fire all day, not just at open/close:**

Intraday `paper_trades` (joined to `signals` where `bar_interval IN (1m,5m,15m)`), by hour:

| Hour (UTC) | Hour (ET) | Orders |
|---|---|---|
| 13:00 | 09:00 (pre/open) | 35 |
| 14:00 | 10:00 | 39 |
| 15:00 | 11:00 | 8 |
| 16:00 | 12:00 | 4 |
| 17:00 | 13:00 | 3 |
| 18:00 | 14:00 | 4 |
| 19:00 | 15:00 | 1 |
| 21:00 | 16:00 (EOD flatten) | 40 |

By strategy + interval (buys/sells): `intraday-1m-momentum` (24 buy / 31 sell),
`intraday-1m-vwap-reclaim` (18 / 25), `intraday-1m-orb` (14 / 10), `intraday-orb-pivots-5m`
(4 / 1), `intraday-orbo-5m` (4 / 1), `intraday-mr-3bar-low-15m` (1 / 1).

Signal fires by day show the **2026-05-30 timezone fix** unlocking full RTH coverage: 1m
fires jumped from ~350-500/day (pre-fix) to **7,655 (06-01)** and **7,883 (06-02)**. Real
intraday order activity is concentrated on **2026-06-01 (7 buys)** and **2026-06-02 (58 buys,
69 sells)** — the first genuinely full intraday-trading days.

**Conclusion:** Intraday trading is live and genuinely spread across the session. The
"we're not trading during the day" worry is **refuted**.

---

## 2. Stop-loss: CONFIGURED + COMPUTED + SUBMITTED — but 100% REJECTED by Alpaca

**Code path (correct in design):**

- Config: `stops.atr_multiplier=2.5`, `by_class.mean_reversion.atr_multiplier=2.0`,
  per-strategy overrides, `fixed_percent_fallback=0.05` (`settings.json:51-71`).
  `auto_trader_intraday` merges this top-level `stops` block into the intraday config via
  `at.merge_config` (`auto_trader_intraday.py:33-38`, `auto_trader.py:119-133`), so intraday
  uses the same stop policy as EOD.
- `process_signals` attaches a stop after every entry: `_maybe_attach_stop`
  (`auto_trader.py:741-750`), which resolves the level via `sizing.resolve_initial_stop`
  (`auto_trader.py:2244-2258`) and submits a **standalone GTC STOP SELL** order via
  `stops.submit_atr_stop` (`auto_trader.py:2270-2274`, `stops.py:148-163`). Note: this is a
  **separate stop order, not a bracket/OCO**.
- On success it records a `paper_trades` row with `order_type='stop'` + `stop_price`
  (`auto_trader.py:2292-2304`) and labels the entry row `entry_stops='atr_initial'`
  (`auto_trader.py:752-764`).

**Data evidence (the gap):**

| metric (all 196 paper_trades) | value |
|---|---|
| rows with `stop_price` set | **0** |
| rows with `order_type LIKE '%stop%'` | **0** |
| rows with `entry_stops='atr_initial'` | **63** (58 intraday: 49×1m, 8×5m, 1×15m) |

So the protective _intent_ is recorded on 63 entries, but **not a single protective stop
order exists on the book**. The reason is in the logs — `submit_atr_stop` raises, the
`submit_failed` branch returns before writing the stop row (`auto_trader.py:2275-2280`):

```
[ERROR] stop submit failed for intraday-1m-orb/SPY:
  {"code":42210000,"message":"invalid stop_price 741.9597. sub-penny increment
   does not fulfill minimum pricing criteria"}
```

**63 stop-submit failures across logs (58 intraday + 5 daily), ZERO successes**, every one a
sub-penny rejection. The stop is rounded to 4dp (`stops.py:145`) but Alpaca requires penny
increments for stocks ≥ $1. The count (63) exactly matches the 63 `entry_stops='atr_initial'`
rows: **every** stop we tried to place was rejected.

**Conclusion:** Stop-loss is configured and the code attempts it on every entry, but it is
**protective intent, not a protective order** — there is no working stop on the book for any
intraday (or EOD) position. This is the single highest-value fix: round stop_price to the
correct tick (2dp for ≥$1) so the order is accepted.

---

## 3. Trailing stop: CONFIGURED + RUNNING ON INTRADAY — works

**Code path:**

- Config: `trailing_stop.method='atr_trail'`, `multiplier=3.0`, `atr_period=14`
  (`settings.json:78-83`); merged into intraday config the same way as stops.
- `process_signals` advances trailing stops for **every open position** (intraday included)
  before evaluating new signals: `_update_trailing_stops_for_open_positions`
  (`auto_trader.py:2539-2542`, `2539-2542` → `1109-1160`), ratcheting up only and floored at
  the entry-time stop (`auto_trader.py:1095-1100`). Exits are checked via
  `_check_trailing_exits_for_open_positions` (`auto_trader.py:3003-3010`, `1301-1381`).
- State persists in the `trailing_stops` table (`trailing_stops.py`).

**Data evidence:** The `trailing_stops` table has 19 live rows; **14 are intraday strategies**:

- `intraday-1m-orb` × 5 (AMD, KRE, QQQ, SMH, SPY)
- `intraday-orb-pivots-5m` × 4 (NVDA, QQQ, SPY, TSLA)
- `intraday-orbo-5m` × 4 (NVDA, QQQ, SPY, TSLA)
- `botnet101-3-bar-low` × 1, `trend-donchian-breakout-20` × 3, `trend-ma-cross-20-50` × 2 (EOD)

And at least **1 real intraday trailing exit** has fired (intraday SELL row tagged
`trailing`). The table only holds _currently-active_ trails (rows are cleared on close,
`auto_trader.py:1286-1291`), so 14 active intraday trails is strong live evidence.

**Conclusion:** Trailing stops are configured **and** actively ratcheting on intraday
positions, not just EOD. This piece is working as designed. (Caveat: because the initial ATR
stop never lands on the book — section 2 — the trailing engine is currently the _only_
protective mechanism actually live for intraday.)

---

## 4. Pyramiding: CONFIGURED in code, but DORMANT — never triggered

**Code path:** Full pyramid pipeline exists — `_process_pyramid_addon`
(`auto_trader.py:837-1005`) calls `pyramiding.evaluate_addon`
(`pyramiding.py:167-209`), gated on `is_pyramidable(declaration)`
(`pyramiding.py:57-63`), regime alignment, and max tiers. Add-on tier is written to
`paper_trades.pyramid_tier` (`pyramiding.py:149-160`).

**Who is flagged pyramidable:** Only **3 trend strategies**, all in
`strategies/trend/__init__.py:35,51,64` (`trend-*`), all `bar_interval='1d'` (EOD).
**None of the 6 intraday strategies are pyramidable** — every intraday declaration in
`monitoring/config.py` sets `pyramidable: False` (lines 42, 60, 75, 114, 125, 136).

**Data evidence:**

- `paper_trades.pyramid_tier`: **100% NULL** (196/196).
- `intraday_skips` gate `pyramid_not_pyramidable`: **11 rows**, every one
  `reason_detail='strategy declaration has no pyramidable=true'` (mix of botnet101 + a few
  `intraday-1m-orb` — the intraday signals were even _evaluated_ for pyramiding, then vetoed).
- Zero `paper_trades.notes` mention pyramid; zero add-on orders.

**Conclusion:** Pyramiding is **dormant, never triggered**. The machinery is live, but no
strategy in the active intraday set opts in, and even the 3 EOD pyramidable strategies have
not stacked a tier. For intraday specifically, pyramiding is a no-op by design.

---

## 5. The measurement gap: intraday exits are HAPPENING but NOT MEASURED

**Outcomes table reality (confirmed):**

| metric (2048 outcomes rows) | value |
|---|---|
| rows with `bar_interval='1d'` (via signals join) | **2048 (100%)** — 1853 closed, 195 open |
| rows with `bar_interval IN (1m,5m,15m)` | **0** |
| rows with `mfe_pct` set | **0** |
| rows with `mae_pct` set | **0** |
| closed rows with `exit_reason` set | 1853 (EOD only) |

So **no intraday position ever produces a closed `outcomes` row**, and `mfe_pct`/`mae_pct`
are NULL on **every** row (EOD included).

**Why (root cause in code):** `close_intraday_positions.py` flattens intraday positions at
EOD by submitting a market SELL and recording it in `paper_trades`
(`close_intraday_positions.py:212-222`) — but it **never calls `db.close_outcome`**. The only
place that writes a closed intraday-relevant outcome is `stops.reconcile_stop_fills`
(`stops.py:244-248`), which fires only when a STOP order fills — and per section 2 no stop
order ever lands, so that path never runs for intraday.

**But the exits genuinely happen** — intraday SELL rows by exit type:

| exit type | count |
|---|---|
| EOD flatten (`auto-close intraday EOD`) | 40 |
| mid-session signal exit (`auto-exit on bar_ts=...`) | 28 |
| trailing-stop exit | 1 |

Mid-session exit timestamps span 08:08-09:55 ET bar_ts — i.e. positions are closing
**intraday, not only at the bell**.

**Conclusion:** Distinguish clearly — intraday exits (signal, trailing, EOD) are
**happening**; they are simply **not recorded as closed `outcomes`**. Stops/trailing
effectiveness for intraday is therefore **un-measured**, not absent. To measure it we must
write `outcomes` rows (with `return_pct`, `exit_reason`, and populate `mfe_pct`/`mae_pct`) on
every intraday close.

---

## 6. Net verdict for the discussion

**(a) Are we actively day-trading with stops & trailing live?**
- Day-trading: **YES** — real paper orders across the full RTH session (section 1).
- Trailing stops: **YES, live on intraday** (section 3).
- Initial stop-loss: **NO working order on the book** — computed + attempted on every entry
  but 100% rejected by Alpaca (sub-penny). Protective intent only (section 2).

**(b) Dormant vs un-measured vs losing:**
- **Dormant:** Pyramiding — never triggered, no intraday strategy opts in (section 4).
- **Broken (intent-only):** Initial ATR stop orders — 63/63 rejected (section 2).
- **Un-measured:** Intraday exit quality — exits happen but no closed `outcomes`,
  `mfe`/`mae` NULL everywhere (section 5).
- **Losing (tiny sample):** Net realized intraday P&L over the only 2 real full days is
  roughly **flat-to-slightly-negative (~-$32 realized on matched pairs, 36 lots still open)**.
  By strategy (FIFO-matched, closed fills only):
  `intraday-1m-momentum` +$15.88 (10W/5L), `intraday-mr-3bar-low-15m` +$2.00 (1W/0L),
  `intraday-1m-vwap-reclaim` -$5.69 (6W/5L), `intraday-1m-orb` -$44.02 (1W/1L). **Sample is
  far too small to judge edge** — this is 2 days post-TZ-fix, not a verdict on the strategies.

**(c) What "reworking intraday" would change vs leave intact:**
- **Leave intact:** the scheduler/fires/auto-trader pipeline (works), trailing-stop engine
  (works on intraday), regime/sizing/eligibility gating.
- **Fix (high value, low risk):** round stop_price to a valid tick (2dp for ≥$1) so initial
  ATR stops are **accepted** — currently every position runs with no hard stop on the book,
  relying solely on trailing + EOD flatten.
- **Instrument (to measure):** write closed `outcomes` rows on intraday exits and populate
  `mfe_pct`/`mae_pct`, so we can actually judge stop/trailing/exit effectiveness and feed the
  eligibility gates.
- **Decide:** pyramiding for intraday is off by design — keep it off unless a specific
  intraday strategy has a continuation thesis worth opting into.

**Bottom line for the owner:** We _are_ day-trading with trailing live. The plan's implied
worries are mostly wrong on "not trading" and "no trailing," partly right on "no stops" (the
initial stop is broken on the broker side, trailing is the only live protection), and right
that intraday exit quality is currently invisible. The two concrete actions are: **fix the
stop tick-rounding** and **record intraday outcomes** — neither requires reworking the trading
loop itself.
