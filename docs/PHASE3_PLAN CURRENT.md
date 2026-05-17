# Profit Generation — Phase 3 Plan

This is the source of truth for the **milestone-builder** agent. Each
milestone is a checkbox: `- [ ]` = open, `- [x]` = done. The agent picks
the first open item (or one named via `/next-milestone <id>`), executes
it end-to-end, runs the full test suite, commits, pushes — then ticks
the box.

**Conventions** (also encoded in `~/.claude/CLAUDE.md`):
- Python interpreter: `py -3.13` for unit tests / scripts. Conda env
  `trading` (Python 3.11) for anything that imports yfinance / alpaca-py.
- Test command: `py -3.13 -m pytest tests/<file>.py` (skip live API tests)
- Commit style: conventional commits (`feat:`, `fix:`, `chore:`, etc.)
  with `Co-Authored-By: Claude Opus 4.7 (1M context)` footer
- Branch: push directly to `main` (single-user repo)
- Never modify `config/credentials.json`, `data/*.db`, `logs/`

**Phase 3 theme:** Phase 2 made the system feature-complete and pushed
real paper orders to Alpaca starting 2026-05-18. Phase 3 hardens
operations, scales the strategy roster, builds the path from paper to
live, and adds asset classes beyond US equity ETFs. Order milestones so
that the operational scaffolding (3.1, 3.5) lands before live capital
is exposed (3.2.4 onward).

---

## 3.1 Paper → live transition scaffolding

- [x] **3.1.1 Live-trading kill switch**
  - **Deliverable:** `config/kill_switch.json` (single `{"live_trading_halted": bool, "reason": str, "set_at": iso}`) + auto_trader honors it BEFORE any eligibility check + dashboard banner + Telegram `/halt` and `/resume` commands.
  - **Acceptance:** when `live_trading_halted=true`, auto_trader refuses ALL new entries (still processes exits) and logs `KILL_SWITCH_HALT` once per run. Dashboard shows a red banner across the top with reason and set_at. Tests: kill switch honored on entries, exits unaffected, idempotent.
  - **Notes:** Telegram cmd implementation can defer to 3.1.2 if the bot doesn't already accept commands; the file-based switch is the minimum.
  - **Completed:** 2026-05-16 by milestone-builder · commit 92a9e81 (file switch + dashboard banner shipped; Telegram /halt /resume deferred to 3.1.2)

- [x] **3.1.2 Telegram bot command listener**
  - **Deliverable:** `monitoring/telegram_listener.py` (long-poll worker) + new schtask `\TradingSystem\TelegramListener` running it.
  - **Acceptance:** listens for `/halt <reason>`, `/resume`, `/status`, `/positions`, `/pnl` from the configured `chat_id`. Ignores messages from other chats. `/halt` writes kill_switch.json; `/status` returns one-line system health; `/positions` lists open paper positions from Alpaca; `/pnl` shows today's realized P&L. Tests: command parsing, auth check (wrong chat_id rejected).
  - **Completed:** 2026-05-16 by milestone-builder · commit c2616c4 (offset-persisting long-poll loop + schedulers/register_telegram_listener.bat at /sc onstart)

- [ ] **3.1.3 Pre-flight checklist script**
  - **Deliverable:** `scripts/preflight.py` + `tests/test_preflight.py`
  - **Acceptance:** prints PASS/FAIL on each check, exits non-zero on any FAIL. Checks: Alpaca account ACTIVE + unblocked, credentials.json all keys present and non-empty, settings.json schema valid, trading.db schema matches latest migration, last 3 daily reports posted to Notion, last intraday scan within 30 minutes (if market open), Cloudflare tunnel URL file fresh (< 1d). Used as a manual sanity check before any config flip.

- [ ] **3.1.4 Position reconciliation job**
  - **Deliverable:** `monitoring/reconcile_positions.py` + nightly schtask + Telegram alert on drift
  - **Acceptance:** queries Alpaca `list_positions()`, compares against `paper_trades` table (open positions = entries without matching exit). Reports any drift (symbol in Alpaca but not in our DB, or vice versa, or qty mismatch). Posts to Notion daily report as a section. Fires Telegram alert on any non-zero drift. Tests: synthetic drift cases, no-drift case.

- [ ] **3.1.5 Per-strategy live/paper segregation**
  - **Deliverable:** `auto_trade.live_strategies` setting (list of strategy_ids) + auto_trader routes orders accordingly
  - **Acceptance:** strategies in `live_strategies` submit to a live Alpaca account; all others go to paper. Defaults to empty list (everything paper). Requires `config/credentials.json` to have separate `alpaca_live` section (only added by user; agent surfaces missing-key message). Tests: routing logic, missing live creds → graceful refusal to enter live mode.
  - **Notes:** Do NOT add live credentials in this milestone. The setting + routing logic ships paper-only. User flips on per-strategy basis when ready.

---

## 3.2 Capital scaling & risk hardening

- [ ] **3.2.1 Tiered position sizing**
  - **Deliverable:** `monitoring/sizing.py` extended with `sizing_method = "tiered"` + new settings under `auto_trade.tiered`
  - **Acceptance:** per-strategy size scales with track record. Tier 0 (<5 closed outcomes) = $200, Tier 1 (5-19) = $500, Tier 2 (20-49) = $1000, Tier 3 (50+ with Sharpe > 0.3) = $2000. Caps configurable. Tests: tier boundary math, fallback when stats unavailable.

- [ ] **3.2.2 Portfolio drawdown auto-throttle**
  - **Deliverable:** auto_trader inspects portfolio equity vs trailing 30-day peak
  - **Acceptance:** when current equity ≤ 95% of trailing peak, halve all position sizes globally. When ≤ 90%, quarter them. When ≤ 85%, trip the kill switch (writes kill_switch.json with reason). Recovers automatically when equity recovers above 97% of peak. Tests: synthetic equity curves through each threshold.

- [ ] **3.2.3 Concurrent open-position cap by strategy**
  - **Deliverable:** `risk.max_open_per_strategy` setting (default 3) + auto_trader honors it
  - **Acceptance:** if a strategy already has 3 open positions, refuse new entries from that strategy regardless of edge. Exits unaffected. Tests: cap enforcement, mixed-strategy unaffected.

- [ ] **3.2.4 Multi-account capital allocation**
  - **Deliverable:** `config/accounts.json` (one entry per account: paper/live + capital_pct) + auto_trader splits orders proportionally
  - **Acceptance:** default config = single Alpaca paper account at 100%. Schema supports adding live accounts with `capital_pct` and `live_strategies` overrides. Tests: split math, defaults, missing-key handling.

---

## 3.3 Strategy roster expansion

- [ ] **3.3.1 Promote top-N validated strategies**
  - **Deliverable:** `scripts/promote_top_strategies.py` (auto-mode) + report
  - **Acceptance:** scans records.jsonl for PASS verdicts not yet in `TRACKED_STRATEGIES`, ranks by walk-forward-stable Sharpe × universe coverage, promotes top 10 via existing `--promote` machinery, prints a report. Idempotent. Dry-run flag. Tests: ranking math, dedupe against already-active list.
  - **Notes:** Manual promotion is fine until we have ≥ 50 PASS records; this milestone is for when the validator backlog grows.

- [ ] **3.3.2 Intraday-bar strategy variants**
  - **Deliverable:** new `strategies/intraday/` module + 2-3 representative strategies running on 5-min and 15-min bars
  - **Acceptance:** existing mean-reversion logic ports to intraday bars where Polygon data is available. Validator runs the same PASS/FAIL pipeline on intraday outcomes. Signals tagged with `bar_interval="5m"` or `"15m"` in the signals table. Tests: bar-loading on intraday TF, signal-shape parity with EOD path.

- [ ] **3.3.3 Regime-aware strategy rotation**
  - **Deliverable:** `monitoring/regime_router.py` + auto_trader consults it
  - **Acceptance:** reads current regime from latest snapshots row (existing `regime` field from `classify_market_regime`). Each strategy in `TRACKED_STRATEGIES` declares `active_in_regimes=["bull","chop"]` etc. Auto-trader skips entries on strategies whose declared regimes don't include current. Tests: regime mismatch skips, missing-declaration defaults to all-regimes-active.

- [ ] **3.3.4 Strategy auto-deactivation on live divergence**
  - **Deliverable:** `monitoring/strategy_health.py` extended with auto-pause logic
  - **Acceptance:** if a strategy's last 20 LIVE outcomes have mean return < 30% of its backtest mean, auto-write a `paused_strategies` entry. Auto-trader respects it. Re-arm after 30 days OR manual `--unpause`. Telegram alert on every pause/unpause. Tests: synthetic outcomes, pause/unpause cycle.

---

## 3.4 Additional asset classes

- [ ] **3.4.1 Crypto support via Alpaca Crypto API**
  - **Deliverable:** `monitoring/crypto_adapter.py` + symbols list in settings + auto_trader recognizes crypto symbols
  - **Acceptance:** initial universe = BTC/USD, ETH/USD, SOL/USD. 24/7 scheduling (separate schtask `\TradingSystem\Crypto`). Same mean-reversion logic adapted for 24/7 bar data. Position sizing respects a separate `crypto.max_position_usd` (default $500) since spreads are wider. Tests: symbol routing, order construction.

- [ ] **3.4.2 Options screening (long-only) — research milestone**
  - **Deliverable:** `docs/OPTIONS_RESEARCH.md` (NOT code)
  - **Acceptance:** documents (a) which Alpaca options endpoints we'd need, (b) data sources for IV / Greeks, (c) which existing strategies could translate to long calls/puts vs which need restructuring, (d) regulatory + tax implications (1256 vs short-term gains), (e) recommended go/no-go criteria for an implementation milestone. No code shipped.

- [ ] **3.4.3 Futures evaluation — research milestone**
  - **Deliverable:** `docs/FUTURES_RESEARCH.md` (NOT code)
  - **Acceptance:** documents (a) broker options (Alpaca doesn't carry futures; candidates: Tradovate, IBKR, NinjaTrader), (b) data cost (~$120-150/mo minimum), (c) PDT / margin / pattern rules, (d) which strategies plausibly translate to /ES, /NQ, /CL, /GC, (e) go/no-go criteria. No code shipped.

---

## 3.5 Operational maturity

- [ ] **3.5.1 DEBUG REPORT lower-severity cleanup (PG-003 to PG-016)**
  - **Deliverable:** address each non-Critical issue from `DEBUG REPORT.md` in a single sweep + delete DEBUG REPORT.md when done
  - **Acceptance:** each PG-XXX gets either a code fix OR a note in the commit message explaining why it's WONTFIX. Tests pass before and after. Final commit deletes DEBUG REPORT.md.
  - **Notes:** Heavy hitters: PG-009 (intraday/TV signals never reconciled to outcomes — touches 3.1.4), PG-011 (dashboard + TV webhook auth), PG-013 (implicit shorts).

- [ ] **3.5.2 Backup & restore script**
  - **Deliverable:** `scripts/backup.py` + nightly schtask
  - **Acceptance:** copies `data/trading.db`, `data/records.jsonl`, `config/settings.json` to `D:\Backups\profit-generation\YYYY-MM-DD\`. Keeps last 30 days, prunes older. Restore script reverses it. Tests: backup file integrity, prune logic.

- [ ] **3.5.3 Health endpoint on dashboard**
  - **Deliverable:** new `/api/health` returning JSON with all subsystem status
  - **Acceptance:** returns {alpaca: ok/blocked, db: ok, intraday_age_min: N, daily_report_age_h: N, tunnel_age_h: N, kill_switch: bool, open_positions: N}. UptimeRobot-style polling target. Tests: endpoint shape, stale-data flags.

- [ ] **3.5.4 PnL tax export**
  - **Deliverable:** `scripts/export_tax_8949.py`
  - **Acceptance:** generates Form 8949 CSV (one row per closed trade: description, date_acquired, date_sold, proceeds, cost_basis, gain_loss). Joins `paper_trades` entries to exits, computes accurate fills. Splits short-term vs long-term. CLI: `--year 2026`. Tests: roundtrip math, short/long-term split.
  - **Notes:** Useful once we go live; harmless to build now and run against paper for shape validation.

- [ ] **3.5.5 Disaster-recovery runbook**
  - **Deliverable:** `docs/RUNBOOK.md`
  - **Acceptance:** documents recovery procedures for: machine reboot, Alpaca outage, Polygon outage, accidental kill_switch trip, corrupted trading.db, Cloudflare tunnel expired, accidental `dry_run: true` re-flip mid-session. Each procedure ≤ 5 steps.

---

## 3.6 Live-vs-backtest feedback loop

- [ ] **3.6.1 Slippage / fill-quality dashboard widget**
  - **Deliverable:** extends `edge_diff.py` (built in 2.2.5) with a dashboard card
  - **Acceptance:** per-strategy widget: "expected: +0.97%/trade · actual: +0.42%/trade · slippage burn: 56%". Sorted by burn-rate desc. Tests: synthetic fills with known slippage, math correctness.

- [ ] **3.6.2 Live-vs-backtest weekly divergence report**
  - **Deliverable:** `monitoring/live_vs_backtest.py` + Notion weekly post
  - **Acceptance:** every Sunday, computes per-strategy mean return for the LIVE outcomes of the past week vs the same strategy's backtest mean. Flags any strategy where live < 50% of backtest as ⚠️. Posts to Notion. Tests: aggregation math, edge case (strategy with no live trades this week).

- [ ] **3.6.3 Fill-time / latency tracking**
  - **Deliverable:** auto_trader records `submitted_at` and `filled_at` in `paper_trades` (columns exist) and dashboard surfaces the delta
  - **Acceptance:** dashboard "FILL LATENCY" card shows median fill-time delta per strategy. Outliers (> 5min) get flagged. Tests: synthetic timestamps, median math.

---

## Notes for future phase planning

- **Phase 4 candidates** (do not start until 3.1–3.5 are done):
  - Multi-asset live (after 3.1.5, 3.4.1)
  - Crypto leverage / margin
  - Strategy generation via Claude API (replacing the local Ollama path for higher-quality candidates)
  - Public-facing performance page (read-only Vercel deploy)
- **Out of scope for Phase 3:** any live equity trading switch (3.1.5 ships the *capability*, but the flip itself is a deliberate, manual decision by Ross — never an agent milestone), HFT, market making, leverage, anything requiring an LLC.
