# Profit Generation — Phase 2 Plan

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

---

## 2.1 Mass strategy ingestion

- [x] **2.1.4 Batch codegen + validator**
  - **Deliverable:** `scripts/batch_validate.py` + `tests/test_batch_validate.py`
  - **Acceptance:** walks records.jsonl, codegens any UNTESTED record, validates across configurable universe, writes verdict back. Pre-fetches bars once. CLI flags: `--max`, `--since`, `--universe`, `--lookback-days`, `--force`, `--skip-codegen`, `--strategy-id`, `--model`.
  - **Completed:** 2026-05-15 · commit `c8970d0`

- [x] **2.1.1 TradingView Pine library scraper**
  - **Deliverable:** `scripts/scrape_tradingview.py` + `tests/test_scrape_tradingview.py`
  - **Acceptance:** scrapes 10+ public Pine scripts from TradingView's public scripts page (https://www.tradingview.com/scripts/), extracts title + author + description + Pine source code, writes UNTESTED records to records.jsonl using the existing schema. Respects rate limits (max 1 req/sec). Caches via `config.cache.cached`. Skips strategies already in records.jsonl (dedupe by source URL). Tests: HTML parsing, dedupe, rate-limit honoring, malformed-script graceful skip.
  - **Notes:** TradingView's `/scripts/` page is server-rendered HTML — `requests` + BeautifulSoup is enough. Pine source lives at `/script/<id>/`. Filter to strategies only (not indicators) via the page's tag filters.
  - **Completed:** 2026-05-15 · commit `da4b19d`

- [x] **2.1.2a LLM-as-source strategy generator** (replaces deprecated Reddit scraper — Reddit closed self-service API access 2026-05)
  - **Deliverable:** `scripts/llm_strategy_generator.py` + `tests/test_llm_strategy_generator.py`
  - **Acceptance:** asks Ollama (default `qwen2.5-coder:14b`) to generate N candidate trading strategies in a category, each as JSON `{strategy_id, title, entry_rules, exit_rules, risk_management}`. Validates JSON shape, dedupes by strategy_id against existing records.jsonl entries, writes accepted candidates as UNTESTED. CLI flags: `--category` (e.g. "mean-reversion", "breakout", "momentum"), `--count` (default 10, hard cap 50 per run), `--avoid` (comma-list of techniques to exclude — e.g. "Bollinger,RSI,MACD"), `--model`, `--temperature` (default 0.7 for variety). Prompt MUST require: concrete numeric entry/exit rules (not vague), no look-ahead bias, daily-bar oriented, JSON list output only. Tests: prompt construction, malformed-JSON graceful skip, dedupe against existing records, --avoid honored, count cap enforced.
  - **Notes:** Reuse the Ollama plumbing from `monitoring/llm_codegen.py` (`_ollama_post`). Variety is the point — temperature 0.7, NOT 0.1. The validator (existing `batch_validate` pipeline) is the quality filter; this script just emits candidates.
  - **Completed:** 2026-05-15 · commit `92ce661`

- [x] **2.1.2b GitHub repo strategy scraper**
  - **Deliverable:** `scripts/scrape_github_strategies.py` + `tests/test_scrape_github_strategies.py`
  - **Acceptance:** searches GitHub REST API for repos matching configurable queries (default: `"trading strategy"`, `"algorithmic trading"`, `"pine script strategy"`), filters to `stars >= 30` and `pushed_at >= 1y ago`. For each matching repo: fetches README + heuristically-named strategy files (matches `*strategy*.py`, `*.pine`, `strategies/*.py` up to 3 files per repo), runs the combined text through Ollama for extraction (same prompt shape as 2.1.2a), writes results to records.jsonl as UNTESTED with source_url = repo URL. CLI flags: `--query`, `--min-stars` (default 30), `--max-repos` (default 20), `--since-pushed-days` (default 365). Dedupe by repo URL across runs. Tests: GitHub API mocking, README + strategy-file extraction, LLM result shape, dedupe, rate-limit honoring.
  - **Notes:** Use `requests` against `api.github.com`. Authenticated calls allow 5000 req/hr — supports a `github` section in `credentials.json` with `token` field (Personal Access Token, public_repo scope only). Unauthenticated falls back to 60 req/hr (still enough for `--max-repos 20`). The agent should NOT add the credentials section — surface for user to add a token if missing, OR proceed in unauthenticated mode if user prefers. Polite User-Agent header required.
  - **Completed:** 2026-05-15 · commit `4fd899f`

- [x] **2.1.3 Quantitative-blog scrapers**
  - **Deliverable:** `scripts/scrape_quantocracy.py` + `scripts/scrape_quantpedia.py` + tests
  - **Acceptance:** scrape Quantocracy's RSS feed, extract linked blog posts, run each through LLM extraction, write UNTESTED records. Same for Quantpedia (curated strategy index). At least 5 strategies extracted from each source per run.
  - **Completed:** 2026-05-15 · commit `019eb99`

- [x] **2.1.5 Auto-promotion workflow**
  - **Deliverable:** `--promote` flag added to `scripts/validate_strategy.py` AND `scripts/batch_validate.py`. New: `scripts/promote_strategy.py` (helper).
  - **Acceptance:** when verdict is PASS or PASS_WITH_NUANCE, the flag triggers: (1) edit `monitoring/config.py` TRACKED_STRATEGIES list to add the strategy with `active_on=[symbols where it PASSed]` + `compute=<fn_name>`, (2) write `monitoring.intraday_monitor.COMPUTE_FN_MODULES` entry if the strategy lives outside the existing modules, (3) reseed trading.db. Idempotent — re-promoting an already-promoted strategy is a no-op. Dashboard reflects the new active strategy on next refresh. Tests: promotion is reversible via `--demote`, idempotent re-runs, dry-run flag.
  - **Completed:** 2026-05-15 · commit `223cd29`

- [x] **2.1.6 Source-attribution & dedupe**
  - **Deliverable:** `scripts/dedupe_records.py` + tests
  - **Acceptance:** uses `nomic-embed-text` (already on Ollama) to embed each record's `entry_rules + exit_rules`. Cosine-similarity above 0.92 → mark as duplicates and merge (keep the one with the longest source URL chain). Writes `extra.merged_from = [list of original strategy_ids]`. Idempotent.
  - **Completed:** 2026-05-15 · commit `e2cfac9`

---

## 2.2 Better trade insights

- [x] **2.2.1 Per-strategy equity curves on dashboard**
  - **Deliverable:** new `/api/equity_curve/<strategy_id>` endpoint + new dashboard section + tests
  - **Acceptance:** endpoint returns `[(date, cumulative_return_pct)]` for the strategy's closed outcomes from `outcomes` table. Dashboard renders as inline SVG sparkline per active strategy in the EQUITY CURVES card. Click → modal with full-size SVG + drawdown overlay. No external charting library — plain SVG. Tests: empty case (no outcomes), single trade, multiple trades cumulative math correct.
  - **Completed:** 2026-05-15 · commit `2a9bc32`

- [x] **2.2.2 Walk-forward / out-of-sample analysis**
  - **Deliverable:** `scripts/walk_forward.py` + `tests/test_walk_forward.py`
  - **Acceptance:** for a strategy + universe, splits historical bars into N rolling windows (default: 6mo train / 3mo test, step 3mo). Computes per-window verdict. Strategy gets a `walk_forward_stable: bool` flag in records.jsonl based on whether ≥ 70% of test windows match the in-sample verdict. Tests: synthetic data with known stable + unstable strategies, window math correctness.
  - **Completed:** 2026-05-15 · commit `5b8903f`

- [ ] **2.2.3 Time-of-day / day-of-week / regime conditioning**
  - **Deliverable:** new analytics module `monitoring/edge_slicer.py` + dashboard endpoint + UI section
  - **Acceptance:** for each active strategy, slice closed outcomes by day-of-week (Mon/Tue/.../Fri), by VIX quartile (need FRED VIX series), by market regime tag (from snapshot's regime field). Dashboard table: strategy × slice → (n, mean, sharpe). Surfaces "this strategy only works on Mondays" type insights. Tests: synthetic outcomes with known day-of-week bias get correctly sliced.

- [ ] **2.2.4 Correlation matrix**
  - **Deliverable:** `/api/strategy_correlation` endpoint + dashboard heatmap section + tests
  - **Acceptance:** computes pairwise correlation of daily P&L across active strategies (from outcomes table aggregated by exit_ts date). Renders as inline SVG heatmap. Hover → cell value. Above 0.7 correlation gets red border (suggests redundancy). Tests: identity matrix when N=1, perfect correlation when same strategy twice, math correctness.

- [ ] **2.2.5 Realized-vs-theoretical edge analysis**
  - **Deliverable:** `scripts/edge_diff.py` + dashboard widget
  - **Acceptance:** for each strategy with paper_trades, compare backtest expected return per signal vs actual paper-trade fills (using fill_price - entry_price). Surface "backtest says +0.97% but paper fills are giving us +0.42% — slippage is eating 56% of edge" per strategy. Writes report to `logs/edge_diff_YYYY-MM-DD.json`.

- [ ] **2.2.6 News-sentiment overlay on outcomes**
  - **Deliverable:** `scripts/news_sentiment_overlay.py` + dashboard widget
  - **Acceptance:** for each closed outcome, look up news on the symbol within ±1 day of entry_ts. Aggregate sentiment from `news.sentiment` JSON. Slice outcomes by entry-day sentiment (positive/neutral/negative) and report mean return per slice per strategy. Dashboard shows "trades into negative-sentiment days return +X% vs +Y% on neutral days" per strategy.

- [ ] **2.2.7 Forward expectations**
  - **Deliverable:** `/api/strategy_forecast/<strategy_id>` + dashboard widget
  - **Acceptance:** based on backfill stats, compute expected fires-per-month and median return. Show as "expected: ~12 fires/month, median +0.5%/trade" on the strategy edge card. Sets calibrated user expectations. Tests: synthetic strategy with known fire frequency gets forecast accurately.

---

## 2.3 Smarter execution

- [ ] **2.3.1 Time-of-day execution offset**
  - **Deliverable:** `auto_trade.entry_time_offset_min` setting + auto_trader honors it
  - **Acceptance:** auto-trader respects a "wait N minutes after open before submitting" setting. Default 0 (current behavior). When > 0, orders submitted in the EOD pipeline get `client_order_id` tagged with the desired execution time and a small follow-up scheduler in `auto_trader.py` sleeps until that time before submitting. Tests: settings override honored, default unchanged.

- [ ] **2.3.2 Limit-inside-spread orders**
  - **Deliverable:** `auto_trade.order_type = "market" | "limit_inside_spread"` setting; `auto_trader._submit_market_order` extended.
  - **Acceptance:** when set to limit_inside_spread, fetches latest bid/ask via Alpaca data API, submits limit at mid-price, with `time_in_force=DAY`. Records both `limit_price` and `fill_price` in paper_trades. Tests: order construction, mid-price math, market fallback if no quote available.

- [ ] **2.3.3 Position sizing by Kelly fraction**
  - **Deliverable:** `monitoring/sizing.py` + `auto_trade.sizing_method = "fixed" | "kelly"` setting
  - **Acceptance:** Kelly mode reads strategy's historical win_rate + avg_win + avg_loss from outcomes table, computes Kelly fraction (capped at 25% for safety), sizes the order to `min(max_position_usd, kelly_fraction * portfolio_value)`. Default still "fixed". Tests: Kelly math correctness on canned win_rate/payoff, cap honored.

- [ ] **2.3.4 ATR-based stop-loss**
  - **Deliverable:** `auto_trade.stop_loss_atr_multiple` setting + auto_trader honors it
  - **Acceptance:** when set, after entry the auto-trader submits a STOP order at `entry - N × ATR(20)` along with the market entry. Records `stop_price` in paper_trades. Background process tracks fills; when stop hits, records exit and closes the outcome. Tests: ATR math, stop placement.

---

## 2.4 Portfolio-level risk

- [ ] **2.4.1 Per-symbol concentration cap**
  - **Deliverable:** added to auto_trader's eligibility check + setting `risk.max_pct_per_symbol`
  - **Acceptance:** if 3 strategies want to buy KRE on the same day, only the highest-Sharpe gets the order — total notional in any one symbol across strategies stays ≤ `max_pct_per_symbol * portfolio_value` (default 30%). Tests: ranking + cap enforcement, single-strategy unaffected.

- [ ] **2.4.2 Daily drawdown circuit breaker**
  - **Deliverable:** auto_trader honors `risk.max_daily_loss_pct` (already in settings, currently unused)
  - **Acceptance:** auto-trader checks portfolio_value vs equity_at_open. If down ≥ max_daily_loss_pct, refuses ALL new entries (still processes exits). Resets next morning. Tests: trip / no-trip cases, exits still fire.

- [ ] **2.4.3 Strategy-level cool-down**
  - **Deliverable:** auto_trader checks recent N trades per strategy
  - **Acceptance:** if a strategy's last 3 closed outcomes were ALL losers, pause that strategy for 5 trading days. Re-arm automatically. Tests: 3-loser trigger, re-arm timing, mixed wins/losses don't trigger.

---

## 2.5 Macro / sentiment context

- [ ] **2.5.1 FRED macro overlay**
  - **Deliverable:** `monitoring/macro_fetcher.py` + dashboard header strip
  - **Acceptance:** daily fetch of T10Y2Y (recession indicator), VIXCLS (vol), DXY (dollar). Stored in new `macro` table. Dashboard header shows "VIX 18.2 · T10Y2Y +0.34 · DXY 102.1" with color-coding. Tests: parser, dedupe by date.

- [ ] **2.5.2 Earnings-week veto**
  - **Deliverable:** `monitoring/earnings_calendar.py` + auto_trader veto
  - **Acceptance:** fetches upcoming earnings dates per symbol (free source: yfinance has `Ticker.calendar`). Auto-trader skips entries on a symbol within 2 trading days of earnings. Tests: veto logic, calendar parsing.

- [ ] **2.5.3 Sentiment-based entry veto (currently only importance bumping)**
  - **Deliverable:** `auto_trade.veto_negative_sentiment` setting + auto_trader honors it
  - **Acceptance:** when set, auto-trader skips long_entry signals on symbols with ≥ 2 negative-sentiment news items in the last 24h. Tests: veto trigger, threshold honored.

---

## 2.6 Quality of life

- [ ] **2.6.1 Weekly digest to Notion**
  - **Deliverable:** `monitoring/weekly_digest.py` + schtask
  - **Acceptance:** Sunday 6pm PT, generates a markdown summary: "this week — N fires, M closed, X% return, top performer Y, biggest loser Z, new strategies added: [...]". Posts to Notion as a new page in the daily reports DB (with a different "Type" tag = "Weekly Digest"). Tests: aggregation math, markdown structure.

- [ ] **2.6.2 Strategy degradation alert**
  - **Deliverable:** `monitoring/strategy_health.py` + Telegram + dashboard flag
  - **Acceptance:** for each active strategy, compute last-30-trade Sharpe vs all-time Sharpe. If last-30 < 50% of all-time, fire a Telegram alert + flag in dashboard's strategy_edge table with a yellow warning icon. Tests: synthetic outcomes with known degradation.

- [ ] **2.6.3 Cross-validation weekly report**
  - **Deliverable:** `monitoring/cross_validation.py` + Notion page
  - **Acceptance:** weekly report comparing EOD '1d' signals vs intraday '1d-intraday' projections vs TV 'tv-webhook' signals. Identifies (strategy, symbol, date) tuples where the three sources disagree. Posts to Notion patterns DB. Tests: synthetic signals with known disagreements.

- [ ] **2.6.4 Mobile-friendly dashboard**
  - **Deliverable:** CSS pass on `dashboard/index.html`
  - **Acceptance:** dashboard renders cleanly at 375px viewport (iPhone width). Action queue + open positions + Telegram-link visible without horizontal scroll. Other sections collapse / scroll cleanly. Tests: visual regression skipped — manually verify by user.

- [ ] **2.6.5 Cloudflare tunnel auto-setup for TV webhook**
  - **Deliverable:** `schedulers/start_tv_tunnel.bat` + dashboard surface for tunnel URL
  - **Acceptance:** bat file starts cloudflared in named-tunnel mode, captures the URL, writes to `data/tunnel_url.txt`. Dashboard shows the URL in a TV WEBHOOK card with copy-to-clipboard button. Tests: skipped (external service).
