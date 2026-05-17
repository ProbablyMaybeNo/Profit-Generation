# Live-Crypto Smoke-Test Playbook

The day-one procedure for flipping the first crypto strategy to **live**
Alpaca Crypto API trading. Mirrors `docs/LIVE_SMOKE_TEST.md` but adapts
for the 24/7 market, wider spreads, and BTC/USD vs ETH/USD differences.

Each procedure ≤ 5 steps. **Trip the kill switch (`py -3.13 -m
monitoring.kill_switch engage "crypto smoke"`) at any sign of trouble.**

Prerequisites already in place (do not start without these):
- `3.4.1` crypto adapter is live in paper for at least N ≥ 30 closed
  outcomes per crypto strategy
- `4.1.1` live-promotion scorer flagged at least one `READY_FOR_LIVE`
  crypto strategy
- `4.1.2` wizard installed `alpaca_live` AND Alpaca Crypto trading is
  enabled on the live account (separate opt-in in the Alpaca UI)
- `scripts/preflight.py` returns all-PASS
- Equity smoke (`LIVE_SMOKE_TEST.md`) has already completed day 5+ — do
  not run two firsts simultaneously
- A weekday morning slot is scheduled. Yes, crypto is 24/7 — start
  during the equity open so the dashboard / Telegram / human reviewer
  are all live

---

## 1. Pick the candidate crypto strategy

ONE strategy. ONE pair. Minimum-size order. Anything else is over-scope.

1. Run `py -3.13 scripts/score_live_candidates.py` and filter to crypto
   strategies (those whose `active_on` includes a pair from
   `monitoring.config.TRACKED_CRYPTO`).
2. Prefer BTC/USD over ETH/USD over SOL/USD. BTC has the tightest
   spread and the deepest order book — slippage burn is smallest.
3. Cap `settings.crypto.max_position_usd` to **$50** in
   `config/settings.json`. Crypto fractional orders make a $50 BTC
   position feasible (BTC ~ $60k → 0.0008 BTC).
4. Confirm the strategy is NOT a leveraged one (4.2.2 leverage code
   path must be unused for the smoke).
5. Record the chosen strategy + pair in `data/crypto_smoke.json`.

## 2. Add to live_strategies (the actual flip)

1. Read the current value: `py -3.13 -c "from config.utils import
   load_settings; print(load_settings()['auto_trade']['live_strategies'])"`.
   If equity smoke is still active, the equity strategy will be there —
   APPEND the crypto sid, don't replace.
2. Confirm `is_crypto_symbol(symbol)` returns True for the chosen pair
   (the auto_trader uses this to route via the crypto adapter, not the
   equity market-order path).
3. Re-run `py -3.13 scripts/preflight.py` — must still return all-PASS.
4. Confirm the dashboard's Live tab now shows TWO sections: equity and
   crypto, with the crypto strategy listed.
5. Post a Notion entry titled "Crypto smoke armed — \<sid\> \<pair\>"
   tagged `Crypto-Smoke`.

## 3. Pre-armed verification (any time of day)

1. Run `py -3.13 scripts/preflight.py` — must return all-PASS.
2. Confirm Alpaca live dashboard shows crypto buying_power > $200.
3. Spot-check the live spread on BTC/USD via
   `py -3.13 -c "from monitoring.crypto_adapter import latest_quote;
   print(latest_quote('BTC/USD'))"` — abort if spread > 0.5% of mid.
4. Confirm `monitoring.kill_switch` is NOT engaged.
5. Confirm the crypto schtask (`\TradingSystem\Crypto`) heartbeat in
   `logs/crypto_scan.log` is < 15 min old.

## 4. Monitor the first crypto fill

1. Watch the dashboard's Live-Positions card. Crypto fires can occur
   outside US market hours — keep a phone near for the first 24h.
2. As soon as the order submits, paste the Alpaca order_id into
   Telegram `/status` — confirm Alpaca and our DB agree on the fill
   price. Crypto fills are typically near-instant on a market order;
   anything > 5 seconds is suspicious.
3. If the fill price differs from the strategy's signal `close` by
   **more than 1.0%** (wider than the 0.5% equity threshold because
   crypto spreads are wider), trip the kill switch.
4. If the order is UNFILLED 30 seconds after submission, cancel via
   Alpaca dashboard, trip the kill switch, and inspect Alpaca order
   rejections. Most crypto market orders fill immediately — a delay
   means a liquidity issue.
5. Log every event (fill, partial, cancel) into `data/crypto_smoke.json`
   with UTC timestamps (do NOT use local time — crypto trades cross
   midnight constantly).

## 5. Monitor the first 5 crypto fills

1. Five fills is the cutoff for day 1. Crypto strategies fire less
   frequently than equity — five fills may take 2-3 days.
2. After each fill, run `py -3.13 -m monitoring.reconcile_positions` —
   the live crypto position qty and avg cost must match Alpaca exactly.
3. After each EXIT, verify the outcome row landed in `outcomes` with
   `status='closed'` and the return_pct is within 0.2% of the calc
   (wider than equity's 0.1% because of fractional-qty rounding).
4. Watch the trailing 24h P&L vs the paper baseline. Crypto's 24/7
   nature means a single overnight gap can dwarf a day's equity move —
   abort if the crypto position drawdown exceeds 5% of position size.
5. After fill #5, trip the kill switch deliberately to pause the smoke
   for a 12-hour observation window.

## 6. Abort criteria (any of these → trip the kill switch immediately)

1. Fill slippage > 1.0% on any single order.
2. Crypto position drawdown > 5% (vs entry).
3. Reconciliation mismatch between Alpaca crypto positions and our
   `paper_trades` table.
4. Any Telegram alert flagging the crypto strategy as degraded.
5. Any unexpected ERROR-level log line in `logs/auto_trader.log` OR
   `logs/crypto_scan.log`.

## 7. End-of-window sign-off

1. After the 12-hour observation pause from § 5.5, confirm no overnight
   funding / clearing surprises (Alpaca crypto has no funding rates
   on spot, but always confirm).
2. Run `py -3.13 -m monitoring.daily_report` — verify crypto fills
   appear under a separate section from equity.
3. Compute the smoke's 5-fill P&L (same SQL as equity smoke step 7.3).
4. Post a Notion sign-off tagged `Crypto-Smoke-Day1` with the P&L plus
   the BTC/USD price range during the window.
5. If sign-off is **GO**, release the kill switch and continue. If
   **NO-GO**, remove the crypto sid from `auto_trade.live_strategies`
   and open a Phase 4 review.

## 8. Rollback to paper

If at any point during the smoke the crypto strategy must come off live:

1. Trip the kill switch.
2. Edit `config/settings.json` — remove the crypto sid from
   `auto_trade.live_strategies`. Save.
3. Run `py -3.13 -m monitoring.reconcile_positions` — any open crypto
   position must be closed manually via the Alpaca dashboard. Crypto
   positions in tiny fractional qty (e.g. 0.00083 BTC) can be sold via
   "close position" in Alpaca UI.
4. Re-run `py -3.13 scripts/preflight.py` — must show the crypto sid
   is no longer in `live_strategies`.
5. Release the kill switch.

## 9. Day-2+ ramp criteria (matches equity playbook)

1. Day 2 — same single crypto strategy + same pair, `crypto.max_position_usd=$50`.
2. Day 3-5 — bump to $100.
3. Day 6-10 — bump to $250.
4. Day 11+ — restore the $500 cap AND consider adding a second crypto
   strategy IF day-10 cumulative P&L exceeds 0.5%.
5. Never add ETH/USD as a second pair while BTC/USD smoke is still
   running — promote ETH/USD via a fresh playbook execution.

## 10. Cross-references

- `docs/LIVE_SMOKE_TEST.md` — the equity playbook this mirrors
- `docs/RUNBOOK.md` — full disaster-recovery (kill switch, outage, DB)
- `monitoring/crypto_adapter.py` — the routing layer that diverts
  signals from the equity path
- `scripts/preflight.py` — must pass before steps 2 and 3
- `4.1.1 scripts/score_live_candidates.py` — picks the candidate in § 1
- `4.1.2 scripts/setup_live_credentials.py` — installs `alpaca_live`
